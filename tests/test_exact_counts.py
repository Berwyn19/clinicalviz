"""Truncated downloads (review C1): exact API counts, or a clearly labelled sample.

The fake API (httpx.MockTransport) understands just enough of the real one:
count requests (pageSize=1) by start-year RANGE or any other condition, a sorted
'first' request, and paged downloads that stop at max_trials, newest first.
"""
import re
from datetime import date

import httpx
import pytest

from app.aggregation.aggregator import Group, aggregate
from app.aggregation.dimensions import DIMENSIONS, year_range
from app.aggregation.models import ExactCounts
from app.ctgov.client import CTGovClient, RateLimiter
from app.ctgov.counts import can_count, count_params
from app.ctgov.query_builder import build_queries
from app.pipeline import run_plan
from app.schemas.common import ChartType, Dimension, Intent, OverallStatus
from app.schemas.plan import Filters, QueryPlan
from app.schemas.trial import Trial

TODAY = date(2026, 11, 3)
YEAR_COUNTS = {2018: 400, 2019: 500, 2020: 4416, 2021: 300, 2022: 200, 2023: 100, 2024: 50, 2025: 25, 2026: 9}
OTHER = 110                                     # trials before 2018, after 2026, or with no date
TOTAL = sum(YEAR_COUNTS.values()) + OTHER
PER_PHASE = 1000                                # fake count for any non-year condition
# The download, newest first: what the real API returns with sort=StartDate:desc.
DOWNLOAD = [("2028-01", "PHASE2"), ("2027-05", "PHASE2"), ("2026-02", "PHASE2"),
            ("2026-01", "PHASE3"), ("2025-12", "PHASE3")]


def study(nct, start, phase=None):
    proto = {"identificationModule": {"nctId": nct, "briefTitle": "t"},
             "statusModule": {"startDateStruct": {"date": start}}}
    if phase:
        proto["designModule"] = {"phases": [phase]}
    return {"protocolSection": proto}


def fake_api(req: httpx.Request) -> httpx.Response:
    p = req.url.params
    if p.get("pageSize") == "1" and "sort" in p:                # earliest start date
        return httpx.Response(200, json={"studies": [study("NCT0", "2018-03")]})
    if p.get("pageSize") == "1":                                 # a count request
        m = re.search(r"RANGE\[(\d{4})-01-01,\d{4}-12-31\]$", p.get("filter.advanced", ""))
        n = YEAR_COUNTS.get(int(m.group(1)), 0) if m else PER_PHASE
        return httpx.Response(200, json={"totalCount": n, "studies": []})
    size = int(p["pageSize"])
    return httpx.Response(200, json={"totalCount": TOTAL, "nextPageToken": "more", "studies": [
        study(f"NCT{i}", start, phase) for i, (start, phase) in enumerate(DOWNLOAD[:size])]})


def client():
    return CTGovClient(http=httpx.AsyncClient(transport=httpx.MockTransport(fake_api)),
                       rate_limiter=RateLimiter(10_000), backoff_base=0)


TIME_PLAN = QueryPlan(intent=Intent.CHART, chart=ChartType.TIME_SERIES, group_by=Dimension.START_YEAR,
                      filters=Filters(condition="cancer"), assumptions=[])
PHASE_PLAN = QueryPlan(intent=Intent.CHART, chart=ChartType.BAR_CHART, group_by=Dimension.PHASE,
                       filters=Filters(condition="cancer"), assumptions=[])


# ------------------------------------------------------------------ the generic pieces

def test_only_single_valued_dimensions_with_an_api_condition_are_countable():
    assert can_count(Dimension.PHASE) and can_count(Dimension.START_YEAR)
    assert not can_count(Dimension.COUNTRY) and not can_count(Dimension.LEAD_SPONSOR)


def test_phase_condition_is_exact():
    cond = DIMENSIONS[Dimension.PHASE].api_condition
    assert cond("PHASE1/PHASE2") == ("AREA[Phase](PHASE1) AND AREA[Phase](PHASE2) AND NOT AREA[Phase](EARLY_PHASE1) "
                                     "AND NOT AREA[Phase](PHASE3) AND NOT AREA[Phase](PHASE4)")
    assert cond("NA") == "NOT AREA[Phase](EARLY_PHASE1 OR PHASE1 OR PHASE2 OR PHASE3 OR PHASE4)"


def test_count_params_keep_the_query_filters():
    q = build_queries(QueryPlan(intent=Intent.CHART, chart=ChartType.BAR_CHART, group_by=Dimension.STATUS,
                                filters=Filters(drug="x", statuses=[OverallStatus.RECRUITING]), assumptions=[]))[0]
    params = count_params(q, Dimension.STATUS, "RECRUITING")
    assert params["filter.advanced"].endswith("AND AREA[OverallStatus](RECRUITING)")
    assert "AREA[InterventionName](x)" in params["filter.advanced"]
    assert params["filter.overallStatus"] == "RECRUITING" and "fields" not in params and "sort" not in params


def test_year_range_is_one_rule():
    assert year_range(None, None, 1975, 2031, 2026) == (1990, 2026)   # MIN_YEAR floor, no future
    assert year_range(2027, None, 2027, 2030, 2026) == (2027, 2030)   # only future asked: show it
    assert year_range(2000, 2010, 1975, 2031, 2026) == (2000, 2010)


def test_download_is_sorted_newest_first():
    assert build_queries(TIME_PLAN)[0].params["sort"] == "StartDate:desc"


# ------------------------------------------------------------------ end to end with the fake API

@pytest.mark.asyncio
async def test_truncated_time_series_uses_exact_counts():
    async with client() as c:
        out = await run_plan(TIME_PLAN, c, max_trials=5, max_count_requests=100, today=TODAY)
    r = out.result
    by_year = {row.x: row for row in r.rows}
    assert r.categories == list(range(2018, 2027))                    # first year from the API, up to today
    assert by_year[2020].y == 4416 and by_year[2020].nct_ids == []     # the sample has none from 2020
    assert by_year[2026].y == 9 and len(by_year[2026].nct_ids) == 2 and not by_year[2026].nct_ids_complete
    assert r.stats["outside_or_missing_not_shown"] == OTHER
    assert r.stats["exact_counts"] and not r.stats["sampled"]
    assert r.notes[0].startswith("Counts are exact")
    assert out.exact_count_requests == 1 + len(YEAR_COUNTS)            # 1 'first year' + 1 per year


@pytest.mark.asyncio
async def test_truncated_bar_chart_counts_the_values_seen_in_the_sample():
    async with client() as c:
        out = await run_plan(PHASE_PLAN, c, max_trials=5, max_count_requests=100, today=TODAY)
    r = out.result
    assert {row.x: row.y for row in r.rows} == {"Phase 2": PER_PHASE, "Phase 3": PER_PHASE}
    assert out.exact_count_requests == 2
    assert any(f"{TOTAL - 2 * PER_PHASE} trials are not in the bars shown" in n for n in r.notes)


@pytest.mark.asyncio
async def test_over_budget_falls_back_to_a_labelled_sample():
    async with client() as c:
        out = await run_plan(TIME_PLAN, c, max_trials=5, max_count_requests=3, today=TODAY)
    r = out.result
    assert r.stats["sampled"] and not r.stats["exact_counts"]
    assert any("would need" in n for n in r.notes)
    assert any(n.startswith("Based on a sample") for n in r.notes)


# ------------------------------------------------------------------ aggregator alone

def trial(nct, phase="NA"):
    return Trial(nct_id=nct, title="t", status=None, study_type=None, phases=[], phase=phase,
                 start_date=None, start_year=None, completion_date=None, duration_months=None,
                 enrollment=None, lead_sponsor=None, lead_sponsor_key=None, lead_sponsor_display=None,
                 sponsor_class=None)


BAR = QueryPlan(intent=Intent.CHART, chart=ChartType.BAR_CHART, group_by=Dimension.PHASE,
                filters=Filters(), assumptions=[])


def test_untruncated_group_has_no_coverage_note():
    r = aggregate(BAR, [Group(None, Filters(), [trial("A")], total_count=1)])
    assert not r.stats["sampled"] and not any("sample" in n for n in r.notes)


def test_truncated_group_without_exact_counts_says_it_is_a_sample():
    r = aggregate(BAR, [Group(None, Filters(), [trial("A"), trial("B")], total_count=900)])
    assert r.stats["sampled"] and r.stats["matched"] == 900 and r.stats["downloaded"] == 2
    assert r.notes[0].startswith("Based on a sample")


def test_exact_counts_replace_sample_counts_in_bar_charts():
    exact = ExactCounts(counts={"PHASE2": 700, "NA": 150, "PHASE3": 0}, total=1000)
    g = Group(None, Filters(), [trial("A", "PHASE2"), trial("B", "PHASE3")], total_count=1000, exact=exact)
    r = aggregate(BAR, [g])
    assert {row.x: row.y for row in r.rows} == {"Phase 2": 700, "N/A": 150}
    assert any("150 trials are not in the bars shown" in n for n in r.notes)
    assert r.stats["trials"] == 1000
