"""Chart number accuracy: is every bar / point the number the registry gives?

For each count chart in planner_cases.json, the EXPECTED plan (not the LLM's) is run
through the real pipeline (`run_plan`, same settings as the app). Then every bar is
recounted with its own API search: the series' query AND "this bar's value", written
here independently of app/aggregation (status even uses a different API mechanism,
filter.overallStatus).

Each bar falls into one of three classes:
  complete   every matching trial was downloaded and counted locally
  api_exact  download truncated; the bar is an exact API count (pipeline counts.py)
  sample     download truncated; the bar counts the downloaded sample only

Two kinds of dimension:
  registry   one coded field (phase, status, year, sponsor class, study type,
             intervention type, enrollment bins): the bar should equal the recount exactly.
  text       free text the app normalizes (country, lead sponsor, condition, intervention).
             The recount is what you get by FILTERING on the bar's label (the API's
             phrase + synonym search), so a gap measures "bar vs. filtering on that label".

Usage:
  python -m evals.eval_counts
  python -m evals.eval_counts --only bar_phase_adalimumab --top 10
"""

import argparse
import asyncio
import json
import statistics
from collections import defaultdict
from datetime import date

from app.config import settings
from app.ctgov.client import CTGovClient, make_client
from app.ctgov.query_builder import DIMENSION_FIELDS
from app.pipeline import run_plan, uses_exact_counts
from app.schemas.common import ChartType, Dimension, NumericField
from app.schemas.plan import CompareGroup, Filters, QueryPlan

from .common import ROOT, pct, save

COUNT_CHARTS = {"bar_chart", "grouped_bar_chart", "time_series", "histogram"}
REGISTRY_DIMS = {"phase", "status", "start_year", "sponsor_class", "study_type", "intervention_type",
                 "enrollment"}
PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"]


# ------------------------------------------------------------------ expected plan -> QueryPlan

def _filters(spec: dict) -> Filters:
    clean = {}
    for k, v in spec.items():
        if k in ("drug", "condition", "sponsor", "country") and isinstance(v, list):
            v = next(x for x in v if x)          # first listed spelling
        clean[k] = v
    return Filters(**clean)


def gold_plan(case: dict) -> QueryPlan | None:
    options = case.get("alternatives") or [case["expect"]]
    e = next((o for o in options if isinstance(o.get("chart"), str) and o["chart"] in COUNT_CHARTS), None)
    if e is None or (e["chart"] == "histogram" and e["numeric_field"] not in ("enrollment", "start_year")):
        return None
    series = e["filters"]
    compare = None
    if len(series) > 1:
        compare = [CompareGroup(label=f"S{i + 1}: " + ", ".join(str(v if not isinstance(v, list) else v[0])
                                                                  for v in s.values()),
                                filters=_filters(s)) for i, s in enumerate(series)]
    return QueryPlan(intent="chart", chart=e["chart"], group_by=e.get("group_by"),
                     numeric_field=e.get("numeric_field"), sort=e.get("sort"), limit=e.get("limit"),
                     filters=Filters() if compare else _filters(series[0]), compare=compare, assumptions=[])


# ------------------------------------------------------------------ independent recount queries

def _phrase(label: str) -> str:
    return '"' + label.replace('"', " ").strip() + '"'


def _phase_keys(label: str) -> list[str] | None:
    """'Phase 1/2' -> [PHASE1, PHASE2]; 'Early Phase 1 / Phase 1' -> [EARLY_PHASE1, PHASE1]; 'N/A' -> []"""
    if label == "N/A":
        return []
    keys = []
    for part in label.split(" / "):
        if part == "Early Phase 1":
            keys.append("EARLY_PHASE1")
        elif part.startswith("Phase "):
            keys += [f"PHASE{n}" for n in part.removeprefix("Phase ").split("/")]
        else:
            return None
    return keys


def _code(label: str) -> str:
    """'Active not recruiting' -> 'ACTIVE_NOT_RECRUITING'"""
    return label.upper().replace(" ", "_")


def recount_params(dim: str, row, base: dict[str, str]) -> dict[str, str] | None:
    """API params that count the trials behind this bar. None = can't recount this bar."""
    x = str(row.x)
    if x == "Unknown":
        return None
    params, extra = dict(base), None
    if dim == "phase":
        keys = _phase_keys(x)
        if keys is None:
            return None
        extra = " AND ".join([f"AREA[Phase]{k}" for k in keys] +
                             [f"NOT AREA[Phase]{p}" for p in PHASES if p not in keys])
    elif dim == "status":
        code = _code(x)
        allowed = params.get("filter.overallStatus")
        if allowed and code not in allowed.split(","):
            return None                            # bar outside the question's status filter
        params["filter.overallStatus"] = code
    elif dim == "start_year":
        extra = f"AREA[StartDate]RANGE[{x}-01-01,{x}-12-31]"
    elif dim == "enrollment":
        hi = "MAX" if row.bin_end is None else int(row.bin_end) - 1
        extra = f"AREA[EnrollmentCount]RANGE[{int(row.bin_start)},{hi}]"
    elif dim == "sponsor_class":
        extra = f"AREA[LeadSponsorClass]{_code(x)}"
    elif dim == "study_type":
        extra = f"AREA[StudyType]{_code(x)}"
    elif dim == "intervention_type":
        extra = f"AREA[InterventionType]{_code(x)}"
    elif dim == "country":
        extra = f"AREA[LocationCountry]{_phrase(x)}"
    elif dim == "lead_sponsor":
        extra = f"AREA[LeadSponsorName]{_phrase(x)}"
    elif dim == "condition":
        extra = f"(AREA[Condition]{_phrase(x)} OR AREA[ConditionMeshTerm]{_phrase(x)})"
    elif dim == "intervention":
        p = _phrase(x)
        extra = f"(AREA[InterventionName]{p} OR AREA[InterventionOtherName]{p} OR AREA[InterventionMeshTerm]{p})"
    else:
        return None
    if extra:
        params["filter.advanced"] = " AND ".join(c for c in (params.get("filter.advanced"), extra) if c)
    return params


async def ids_for(client: CTGovClient, params: dict[str, str], cap: int = 3000) -> set[str] | None:
    from app.ctgov.query_builder import ApiQuery
    total = await client.count(params)
    if total > cap:
        return None
    r = await client.fetch(ApiQuery(label=None, filters=Filters(), params={**params, "fields": "NCTId"}), cap)
    return {s["protocolSection"]["identificationModule"]["nctId"] for s in r.studies}


async def raw_values(client: CTGovClient, nct_ids: list[str], dim: str) -> dict[str, dict]:
    """The registry fields behind a few example trials, to explain a mismatch."""
    if not nct_ids:
        return {}
    fields = ["NCTId"] + DIMENSION_FIELDS.get(Dimension(dim), []) if dim != "enrollment" else ["NCTId", "EnrollmentCount"]
    data, _ = await client._get_page({"filter.ids": ",".join(nct_ids), "fields": ",".join(dict.fromkeys(fields)),
                                      "pageSize": "50"})
    out = {}
    for s in data.get("studies", []):
        nct = s["protocolSection"]["identificationModule"]["nctId"]
        out[nct] = _compact(s)
    return out


def _compact(study: dict) -> dict:
    p = study.get("protocolSection", {})
    out = {}
    for mod in p.values():
        for k, v in (mod or {}).items():
            if k in ("identificationModule",):
                continue
            out[k] = v
    out.pop("nctId", None)
    text = json.dumps(out)
    return json.loads(text) if len(text) < 600 else {"truncated": text[:600]}


# ------------------------------------------------------------------ running

async def eval_case(case: dict, client: CTGovClient, top: int) -> dict | None:
    plan = gold_plan(case)
    if plan is None:
        return None
    if plan.chart == ChartType.HISTOGRAM:
        dim = "enrollment" if plan.numeric_field == NumericField.ENROLLMENT else "start_year"
    else:
        dim = plan.group_by.value

    res = await run_plan(plan, client, max_trials=10000, max_count_requests=settings.ctgov_exact_count_max_requests,
                         today=date.today())
    by_series = {f.query.label: f for f in res.fetches}
    exact_used = uses_exact_counts(plan) and res.exact_count_requests > 0 and not res.warnings

    # Which bars to check: all for time series / histograms / short bar charts, the top N otherwise.
    rows = list(res.result.rows)
    if plan.chart in (ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART):
        keep = []
        for label in {r.series for r in rows}:
            series_rows = sorted((r for r in rows if r.series == label), key=lambda r: -r.y)
            keep += series_rows[:top]
        rows = keep

    async def check(row):
        f = by_series[row.series]
        base = {k: v for k, v in f.query.params.items() if k not in ("fields", "sort")}
        params = recount_params(dim, row, base)
        if params is None:
            return None
        cls = "complete" if not f.truncated else ("api_exact" if exact_used else "sample")
        truth = await client.count(params)
        item = {"series": row.series, "x": row.x, "app": row.y, "recount": truth, "class": cls,
                "kind": "registry" if dim in REGISTRY_DIMS else "text"}
        if cls == "complete" and truth != row.y:
            truth_ids = await ids_for(client, params)
            if truth_ids is not None:
                app_ids = set(row.nct_ids)
                only_app, only_api = sorted(app_ids - truth_ids), sorted(truth_ids - app_ids)
                item["only_in_app"] = len(only_app)
                item["only_in_recount"] = len(only_api)
                item["examples_only_in_app"] = await raw_values(client, only_app[:2], dim)
                item["examples_only_in_recount"] = await raw_values(client, only_api[:2], dim)
        return item

    checked = [c for c in await asyncio.gather(*(check(r) for r in rows)) if c]
    return {"id": case["id"], "question": case["question"], "plan": plan.model_dump(mode="json", exclude_none=True),
            "dimension": dim, "matched": {str(k): f.total_count for k, f in by_series.items()},
            "downloaded": {str(k): len(f.studies) for k, f in by_series.items()},
            "exact_count_requests": res.exact_count_requests, "warnings": res.warnings, "bars": checked}


def _summary(bars: list[dict]) -> str:
    n = len(bars)
    if not n:
        return "no bars"
    exact = sum(b["app"] == b["recount"] for b in bars)
    within5 = sum(abs(b["app"] - b["recount"]) <= 0.05 * max(b["recount"], 1) for b in bars)
    total = sum(b["recount"] for b in bars)
    abs_err = sum(abs(b["app"] - b["recount"]) for b in bars)
    signed = sum(b["app"] - b["recount"] for b in bars)
    rel = [abs(b["app"] - b["recount"]) / b["recount"] for b in bars if b["recount"]]
    return (f"exactly equal {pct(exact, n)}; within 5% {pct(within5, n)}; "
            f"total abs error {100 * abs_err / max(total, 1):.1f}% of {total:,} trials "
            f"(app {'over' if signed > 0 else 'under'}counts by {abs(signed):,} net); "
            f"median bar error {100 * statistics.median(rel) if rel else 0:.1f}%")


def report(cases: list[dict]) -> str:
    bars = [dict(b, case=c["id"], dim=c["dimension"]) for c in cases for b in c["bars"]]
    lines = ["## Chart numbers", "", f"{len(cases)} charts, {len(bars)} bars/points recounted.", ""]
    for cls in ("complete", "api_exact", "sample"):
        for kind in ("registry", "text"):
            sub = [b for b in bars if b["class"] == cls and b["kind"] == kind]
            if sub:
                lines.append(f"- **{cls} / {kind}** ({len(sub)} bars): {_summary(sub)}")
    lines += ["", "By dimension (complete downloads only):", ""]
    by_dim = defaultdict(list)
    for b in bars:
        if b["class"] == "complete":
            by_dim[b["dim"]].append(b)
    for dim, sub in sorted(by_dim.items()):
        lines.append(f"- {dim} ({len(sub)} bars): {_summary(sub)}")
    lines += ["", "Largest gaps:", ""]
    worst = sorted(bars, key=lambda b: -abs(b["app"] - b["recount"]))[:15]
    for b in worst:
        if b["app"] == b["recount"]:
            break
        extra = ""
        if "only_in_app" in b:
            extra = f" (only in app {b['only_in_app']}, only in recount {b['only_in_recount']})"
        lines.append(f"- `{b['case']}` {b['dim']}={b['x']!r} {b['series'] or ''}: app {b['app']:,} vs "
                     f"recount {b['recount']:,} [{b['class']}]{extra}")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--top", type=int, default=15, help="bars checked per series for category charts")
    args = parser.parse_args()
    cases = json.loads((ROOT / "planner_cases.json").read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    async with make_client() as client:
        results = []
        for c in cases:   # one chart at a time: the rate limiter is shared anyway
            r = await eval_case(c, client, args.top)
            if r:
                print(f"  {r['id']}: {len(r['bars'])} bars", flush=True)
                results.append(r)
    text = report(results)
    path = save("counts", {"cases": results, "summary": text})
    print(text)
    print(f"\nfull results: {path}")


if __name__ == "__main__":
    asyncio.run(main())
