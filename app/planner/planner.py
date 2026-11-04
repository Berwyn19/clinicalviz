import logging

from openai import APIError, AsyncOpenAI, LengthFinishReasonError
from pydantic import ValidationError

from app.schemas.plan import QueryPlan, find_plan_errors
from app.schemas.request import QueryRequest

from .prompts import PLANNER_SYSTEM, format_request

log = logging.getLogger(__name__)


class PlannerError(Exception):
    """No valid plan after all attempts, or the LLM API failed."""


TEXT_FILTERS = ("drug", "condition", "sponsor", "country")


def hard_filters(req: QueryRequest) -> dict:
    """Plan filter name -> value fixed by the structured request fields."""
    fixed = {
        "drug": req.drug_name,
        "condition": req.condition,
        "sponsor": req.sponsor,
        "country": req.country,
        "phases": req.trial_phase,
        "statuses": req.status,
        "start_year_min": req.start_year,
        "start_year_max": req.end_year,
    }
    return {k: v for k, v in fixed.items() if v is not None}


def _narrows(name: str, value, fixed) -> bool:
    """Does a compare group's value stay inside the request's fixed value?"""
    if name in TEXT_FILTERS:
        return value.strip().lower() == fixed.strip().lower()
    if name in ("phases", "statuses"):
        return set(value) <= set(fixed)            # e.g. PHASE2 vs PHASE3 inside phases [2, 3]
    if name == "start_year_min":
        return value >= fixed
    return value <= fixed                          # start_year_max


def find_compare_conflicts(plan: QueryPlan, req: QueryRequest) -> list[str]:
    """Compare groups override shared filters field by field (query_builder.merge_filters),
    so a group could silently replace a request field. Groups may only narrow those fields."""
    fixed = hard_filters(req)
    errors = []
    for i, group in enumerate(plan.compare or []):
        for name, fixed_value in fixed.items():
            value = getattr(group.filters, name)
            if value is not None and not _narrows(name, value, fixed_value):
                shown = [v.value for v in fixed_value] if isinstance(fixed_value, list) else fixed_value
                errors.append(
                    f"compare[{i}] ('{group.label}') sets {name}, but the request fixes {name}={shown!r}. "
                    f"Compare groups may only narrow it, so vary a different field "
                    f"(or use intent 'unsupported' if the question needs to vary {name}).")
    return errors


def apply_hard_filters(plan: QueryPlan, req: QueryRequest) -> QueryPlan:
    """Structured request fields always win over what the LLM read from the text.

    This sets the shared filters; compare groups that contradict them are rejected
    by find_compare_conflicts (they would otherwise override the shared filters).
    """
    overrides = hard_filters(req)
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
            errors = find_plan_errors(plan) + find_compare_conflicts(plan, req)
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