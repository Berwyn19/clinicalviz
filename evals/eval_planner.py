"""Planner accuracy: does the question become the right plan?

Runs the real guardrail + planner (the same `preprocess` the API uses) on every case in
planner_cases.json, several times each (the LLM is not deterministic), and scores only
the fields each case lists. A run passes when every scored field matches.

Usage:
  python -m evals.eval_planner            # 3 runs per case
  python -m evals.eval_planner --runs 5 --only ts_semaglutide
"""

import argparse
import asyncio
import itertools
import json
import time
from collections import defaultdict

from openai import AsyncOpenAI

from app.config import settings
from app.ctgov.query_builder import merge_filters
from app.planner.preprocess import preprocess
from app.schemas.plan import QueryPlan
from app.schemas.request import QueryRequest

from .common import ROOT, norm_text, pct, save

TEXT = ("drug", "condition", "sponsor", "country")
SETS = ("phases", "statuses")
YEARS = ("start_year_min", "start_year_max")


# ------------------------------------------------------------------ scoring

def _enum(v):
    return getattr(v, "value", v)


def _text_ok(expected, actual) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return norm_text(actual) in {norm_text(o) for o in options}


def filters_ok(expected: dict, actual: dict) -> bool:
    """One series' effective filters. Fields not listed must be empty."""
    for name in TEXT:
        if not _text_ok(expected.get(name), actual.get(name)):
            return False
    for name in SETS:
        if set(expected.get(name) or []) != {_enum(v) for v in actual.get(name) or []}:
            return False
    return all(expected.get(name) == actual.get(name) for name in YEARS)


def effective_filters(plan: QueryPlan) -> list[dict]:
    groups = plan.compare or []
    filters = [merge_filters(plan.filters, g.filters) for g in groups] or [plan.filters]
    return [f.model_dump(mode="json") for f in filters]


def series_match(expected: list[dict], actual: list[dict]) -> bool:
    """Same number of series and some one-to-one pairing where every pair matches."""
    if len(expected) != len(actual):
        return False
    return any(all(filters_ok(e, a) for e, a in zip(expected, perm))
               for perm in itertools.permutations(actual))


def _value_ok(expected, actual) -> bool:
    if isinstance(expected, dict) and "any" in expected:
        return actual in expected["any"]
    return actual == expected


def score(expect: dict, plan: QueryPlan) -> dict[str, bool]:
    """field -> correct?, for the fields this case lists."""
    p = plan.model_dump(mode="json")
    out = {}
    for name, expected in expect.items():
        if name == "filters":
            out[name] = plan.intent.value != "unsupported" and series_match(expected, effective_filters(plan))
        elif name == "xy":
            out[name] = sorted(expected) == sorted([p["x_field"] or "", p["y_field"] or ""])
        elif name == "network":
            net = p["network"] or {}
            out[name] = sorted(expected) == sorted([net.get("source") or "", net.get("target") or ""])
        else:
            out[name] = _value_ok(expected, p.get(name))
    return out


def best_alternative(case: dict, plan: QueryPlan) -> dict[str, bool]:
    options = case.get("alternatives") or [case["expect"]]
    scored = [score(e, plan) for e in options]
    return max(scored, key=lambda s: (all(s.values()), sum(s.values())))


def describe(plan: QueryPlan) -> dict:
    p = plan.model_dump(mode="json", exclude_none=True)
    p.pop("assumptions", None)
    if plan.compare:
        p["effective_filters"] = [{k: v for k, v in f.items() if v} for f in effective_filters(plan)]
    return p


# ------------------------------------------------------------------ running

async def run_case(case: dict, llm: AsyncOpenAI, runs: int, sem: asyncio.Semaphore) -> dict:
    req = QueryRequest(query=case["question"], **case.get("fields", {}))
    results = []
    for _ in range(runs):
        async with sem:
            t0 = time.monotonic()
            try:
                plan = await preprocess(req, llm)
                fields = best_alternative(case, plan)
                results.append({"ok": all(fields.values()), "fields": fields, "plan": describe(plan),
                                "seconds": round(time.monotonic() - t0, 2)})
            except Exception as exc:  # a failed plan is a wrong answer, not a crash of the eval
                results.append({"ok": False, "fields": {}, "error": f"{type(exc).__name__}: {exc}",
                                "seconds": round(time.monotonic() - t0, 2)})
    signatures = {json.dumps(r.get("plan", r.get("error")), sort_keys=True) for r in results}
    return {"id": case["id"], "question": case["question"], "fields_given": case.get("fields", {}),
            "runs": results, "consistent": len(signatures) == 1}


def report(cases: list[dict]) -> str:
    runs = [r for c in cases for r in c["runs"]]
    lines = ["## Planner", "",
             f"- Runs that produced the expected plan: {pct(sum(r['ok'] for r in runs), len(runs))}",
             f"- Questions right on every run: {pct(sum(all(r['ok'] for r in c['runs']) for c in cases), len(cases))}",
             f"- Questions with the identical plan on every run: {pct(sum(c['consistent'] for c in cases), len(cases))}",
             f"- Runs that errored: {sum('error' in r for r in runs)}",
             "", "Per field (over runs where the field is scored):", ""]
    per_field = defaultdict(lambda: [0, 0])
    for r in runs:
        for name, ok in r["fields"].items():
            per_field[name][0] += ok
            per_field[name][1] += 1
    for name, (k, n) in sorted(per_field.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        lines.append(f"- {name}: {pct(k, n)}")
    lines += ["", "Failures:", ""]
    for c in cases:
        bad = [r for r in c["runs"] if not r["ok"]]
        if not bad:
            continue
        lines.append(f"- `{c['id']}` ({len(bad)}/{len(c['runs'])} runs) \"{c['question']}\"")
        for r in bad[:2]:
            wrong = [k for k, v in r["fields"].items() if not v]
            lines.append(f"  - wrong: {', '.join(wrong) or '-'}; got: "
                         f"{r.get('error') or json.dumps(r['plan'])[:400]}")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    cases = json.loads((ROOT / "planner_cases.json").read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    llm = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)
    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(run_case(c, llm, args.runs, sem) for c in cases))
    text = report(results)
    path = save("planner", {"model": settings.planner_model, "guardrail_model": settings.guardrail_model,
                            "runs_per_case": args.runs, "cases": results, "summary": text})
    print(text)
    print(f"\nfull results: {path}")


if __name__ == "__main__":
    asyncio.run(main())
