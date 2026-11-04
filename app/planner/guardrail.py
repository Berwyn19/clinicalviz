from openai import AsyncOpenAI
from pydantic import BaseModel

from app.schemas.request import QueryRequest

from .prompts import GUARDRAIL_SYSTEM, format_request


class GuardrailResult(BaseModel):
    relevant: bool
    reason: str


class GuardrailError(Exception):
    """The guardrail could not reach a decision (API error, refusal, bad output)."""


async def check_query(req: QueryRequest, client: AsyncOpenAI, model: str) -> GuardrailResult:
    """Fails closed: if we can't get a clear answer, we raise instead of letting the query through."""
    try:
        completion = await client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": GUARDRAIL_SYSTEM},
                {"role": "user", "content": format_request(req)},
            ],
            response_format=GuardrailResult,
        )
    except Exception as exc:  # network error, timeout, schema mismatch, ...
        raise GuardrailError(f"guardrail call failed: {exc}") from exc

    message = completion.choices[0].message
    if message.refusal or message.parsed is None:
        raise GuardrailError(f"guardrail gave no decision: {message.refusal or 'empty output'}")
    return message.parsed