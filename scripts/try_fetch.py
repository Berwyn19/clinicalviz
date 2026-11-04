"""Manual check of plan -> API request -> fetched trials.

  python -m scripts.try_fetch                    # hand-written plan, no LLM needed
  python -m scripts.try_fetch "question" [drug]  # plan made by the LLM preprocessor
"""
import asyncio
import json
import sys

from app.ctgov.client import make_client
from app.ctgov.query_builder import build_queries
from app.schemas.common import ChartType, Dimension, Intent
from app.schemas.plan import Filters, QueryPlan

HAND_WRITTEN_PLAN = QueryPlan(
    intent=Intent.CHART,
    chart=ChartType.TIME_SERIES,
    filters=Filters(drug="Pembrolizumab"),
    group_by=Dimension.START_YEAR,
    assumptions=[],
)


async def main():
    if len(sys.argv) > 1:
        from app.planner.preprocess import preprocess
        from app.schemas.request import QueryRequest
        req = QueryRequest(query=sys.argv[1], drug_name=sys.argv[2] if len(sys.argv) > 2 else None)
        plan = await preprocess(req)
    else:
        plan = HAND_WRITTEN_PLAN
    print("PLAN:", plan.model_dump_json(exclude_none=True))

    if plan.intent == Intent.UNSUPPORTED:
        print("unsupported:", plan.reason)
        return

    async with make_client() as client:
        results = await client.fetch_all(build_queries(plan), max_trials=10000)

    for r in results:
        print(f"\n[{r.query.label or 'query'}] {r.request_url}")
        print(f"  matched={r.total_count} fetched={len(r.studies)} truncated={r.truncated} "
              f"pages={r.pages_fetched} cache_hits={r.cache_hits}")
        if r.broad_total_count is not None:
            print(f"  broad search total={r.broad_total_count} "
                  f"(left out as text-only mentions: {r.broad_total_count - r.total_count})")
        for w in r.warnings:
            print("  warning:", w)
        if r.studies:
            print("  first trial:", json.dumps(r.studies[0])[:300], "...")


asyncio.run(main())