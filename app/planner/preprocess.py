"""Preprocessor = guardrail + planner.

Always returns a QueryPlan, so the rest of the pipeline only has one input type.
An off-topic query becomes a plan with intent='unsupported'.
"""

from openai import AsyncOpenAI

from app.config import settings
from app.schemas.common import Intent
from app.schemas.plan import Filters, QueryPlan
from app.schemas.request import QueryRequest

from .guardrail import check_query
from .planner import make_plan


def _unsupported(reason: str) -> QueryPlan:
    return QueryPlan(intent=Intent.UNSUPPORTED, reason=reason, filters=Filters(), assumptions=[])


async def preprocess(req: QueryRequest, client: AsyncOpenAI | None = None) -> QueryPlan:
    client = client or AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)

    verdict = await check_query(req, client, settings.guardrail_model)
    if not verdict.relevant:
        return _unsupported(verdict.reason)

    return await make_plan(req, client, settings.planner_model, settings.planner_max_attempts)