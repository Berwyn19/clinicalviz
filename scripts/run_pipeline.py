"""Run everything built so far, end to end, and show what each stage produced.

  python -m scripts.run_pipeline "How has the number of trials for this drug changed over time?" --drug Pembrolizumab
  python -m scripts.run_pipeline "Which countries have the most recruiting trials for melanoma?"
  python -m scripts.run_pipeline "Show a network of sponsors and drugs for melanoma trials"
  python -m scripts.run_pipeline "What's the weather in Paris?"

Stages:  1 request -> 2 guardrail + planner (LLM) -> 3 API queries -> 4 fetch -> 5 normalize -> 6 aggregate
"""
import argparse
import asyncio
import json
import time

from pydantic import ValidationError

from app.ctgov.client import CTGovError, make_client
from app.ctgov.query_builder import build_queries
from app.planner.guardrail import GuardrailError
from app.planner.planner import PlannerError
from app.planner.preprocess import preprocess
from app.processing.normalizer import normalize_all
from app.aggregation.aggregator import Group, aggregate
from app.schemas.common import Intent

def stage(n: int, title: str):
    print(f"\n{'=' * 8} {n}. {title} {'=' * (60 - len(title))}")


def show_result(result):
    print(f"  kind: {result.kind}")
    if result.categories:
        print(f"  categories ({len(result.categories)}): {result.categories[:15]}")
    if result.series:
        print(f"  series: {result.series}")
    for row in result.rows[:15]:
        cite = row.citations[0] if row.citations else None
        extra = " (partial year)" if row.is_partial else ""
        print(f"    x={str(row.x)[:40]:<40} series={str(row.series or '-')[:15]:<15} y={row.y:<8}{extra}"
              + (f"  e.g. {cite.nct_id}: {cite.excerpt[:50]!r}" if cite else ""))
    if len(result.rows) > 15:
        print(f"    ... {len(result.rows) - 15} more rows")
    for node in result.nodes[:12]:
        mark = "  <- question's own filter" if node.is_query_match else ""
        print(f"    node {node.id:<45} weight={node.weight}{mark}")
    for edge in result.edges[:12]:
        print(f"    edge {edge.source} <-> {edge.target}  weight={edge.weight}")
    for row in result.table[:10]:
        print(f"    {row.nct_id}  {row.phase:<12} {str(row.status):<20} {row.start_date or '-':<10} {row.title[:50]}")
    for note in result.notes:
        print(f"  note: {note}")
    print(f"  stats: {json.dumps(result.stats, default=str)}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    for name in ("drug", "condition", "sponsor", "country"):
        parser.add_argument(f"--{name}")
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    args = parser.parse_args()

    from app.schemas.request import QueryRequest  # imported late so arg errors show fast
    t0 = time.perf_counter()

    stage(1, "Request")
    try:
        req = QueryRequest(query=args.query, drug_name=args.drug, condition=args.condition,
                           sponsor=args.sponsor, country=args.country,
                           start_year=args.start_year, end_year=args.end_year)
    except ValidationError as exc:
        print("  invalid request:", exc)
        return
    print(" ", req.model_dump_json(exclude_none=True))

    stage(2, "Guardrail + planner (LLM)")
    t = time.perf_counter()
    try:
        plan = await preprocess(req)
    except (GuardrailError, PlannerError) as exc:
        print(f"  LLM step failed: {exc}")
        return
    print(" ", plan.model_dump_json(exclude_none=True, indent=2).replace("\n", "\n  "))
    print(f"  ({time.perf_counter() - t:.1f}s)")
    if plan.intent == Intent.UNSUPPORTED:
        print("\n  -> stopped: unsupported question. Reason:", plan.reason)
        return

    stage(3, "API queries")
    queries = build_queries(plan)
    for q in queries:
        print(f"  [{q.label or 'query'}]")
        for key, value in q.params.items():
            print(f"    {key} = {value}")

    stage(4, "Fetch from ClinicalTrials.gov")
    t = time.perf_counter()
    try:
        async with make_client() as client:
            results = await client.fetch_all(queries, max_trials=req.max_trials)
    except CTGovError as exc:
        print(f"  API failed: {exc}")
        return
    for r in results:
        left_out = (f", broad search {r.broad_total_count} (text-only mentions left out: "
                    f"{r.broad_total_count - r.total_count})") if r.broad_total_count is not None else ""
        print(f"  [{r.query.label or 'query'}] matched {r.total_count}, fetched {len(r.studies)}, "
              f"pages {r.pages_fetched} ({r.cache_hits} cached){left_out}")
        for w in r.warnings:
            print("    warning:", w)
    print(f"  ({time.perf_counter() - t:.1f}s)")

    stage(5, "Normalize")
    normalized = []
    for r in results:
        trials, report = normalize_all(r.studies)
        normalized.append((r, trials))
        print(f"  [{r.query.label or 'query'}] {json.dumps(report.as_dict())}")

    stage(6, "Aggregate")
    groups = [Group(label=r.query.label, filters=r.query.filters, trials=trials) for r, trials in normalized]
    show_result(aggregate(plan, groups))

    print(f"\nTotal time: {time.perf_counter() - t0:.1f}s")


asyncio.run(main())