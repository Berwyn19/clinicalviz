import logging

from openai import APIError, AsyncOpenAI, LengthFinishReasonError
from pydantic import ValidationError

from app.schemas.plan import QueryPlan, find_plan_errors
from app.schemas.request import QueryRequest

from .prompts import PLANNER_SYSTEM, format_request

log = logging.getLogger(__name__)


class PlannerError(Exception):
    """No valid plan after all attempts, or the LLM API failed."""


def apply_hard_filters(plan: QueryPlan, req: QueryRequest) -> QueryPlan:
    """Structured request fields always win over what the LLM read from the text."""
    overrides = {
        "drug": req.drug_name,
        "condition": req.condition,
        "sponsor": req.sponsor,
        "country": req.country,
        "phases": req.trial_phase,
        "statuses": req.status,
        "start_year_min": req.start_year,
        "start_year_max": req.end_year,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if not overrides:
        return plan

    changed = [k for k, v in overrides.items() if getattr(plan.filters, k) != v]
    new_filters = plan.filters.model_copy(update=overrides)
    notes = list(plan.assumptions)
    if changed:
        notes.append(f"Request fields override the question text for: {', '.join(changed)}.")
    return plan.model_copy(update={"filters": new_filters, "assumptions": notes})


async def make_plan(
    req: QueryRequest, client: AsyncOpenAI, model: str, max_attempts: int = 3
) -> QueryPlan:
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": format_request(req)},
    ]
    errors: list[str] = []

    for attempt in range(1, max_attempts + 1):
        raw_output = None
        try:
            completion = await client.chat.completions.parse(
                model=model, messages=messages, response_format=QueryPlan
            )
            message = completion.choices[0].message
            if message.refusal:
                raise PlannerError(f"model refused: {message.refusal}")
            raw_output = message.content
            plan = apply_hard_filters(message.parsed, req)
            errors = find_plan_errors(plan)
        except (ValidationError, LengthFinishReasonError) as exc:
            # Output didn't match the schema at all (rare with structured outputs).
            errors = [f"output did not match the schema: {exc}"]
        except APIError as exc:
            # The SDK already retries network/5xx errors; if we're here, give up.
            raise PlannerError(f"OpenAI API error: {exc}") from exc

        if not errors:
            log.info("plan ok on attempt %d", attempt)
            return plan

        log.warning("plan invalid on attempt %d: %s", attempt, errors)
        if raw_output:
            messages.append({"role": "assistant", "content": raw_output})
        messages.append({
            "role": "user",
            "content": "Your plan has these problems:\n- " + "\n- ".join(errors)
            + "\nReturn a corrected plan. If the question cannot be answered, use intent 'unsupported'.",
        })

    raise PlannerError(f"no valid plan after {max_attempts} attempts: {errors}")