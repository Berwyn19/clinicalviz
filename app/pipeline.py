"""Plan -> chart data: build queries, fetch, normalize, (exact counts), aggregate.

Shared by the scripts and, later, the API endpoint, so they can't drift apart.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date

from app.aggregation.aggregator import Group, aggregate
from app.aggregation.dimensions import DIMENSIONS, Context, year_range
from app.aggregation.models import AggregateResult
from app.ctgov.client import CTGovClient, FetchResult
from app.ctgov.counts import can_count, exact_counts, first_start_year
from app.ctgov.query_builder import build_queries
from app.processing.normalizer import NormalizeReport, normalize_all
from app.schemas.common import ChartType, Intent
from app.schemas.plan import QueryPlan

log = logging.getLogger(__name__)

COUNT_CHARTS = {ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART, ChartType.TIME_SERIES}


@dataclass
class PipelineResult:
    result: AggregateResult
    fetches: list[FetchResult]
    reports: list[NormalizeReport]
    exact_count_requests: int = 0
    warnings: list[str] = field(default_factory=list)


def uses_exact_counts(plan: QueryPlan) -> bool:
    return plan.intent == Intent.CHART and plan.chart in COUNT_CHARTS and can_count(plan.group_by)


async def run_plan(plan: QueryPlan, client: CTGovClient, max_trials: int, max_count_requests: int,
                   today: date | None = None) -> PipelineResult:
    today = today or date.today()
    fetches = await client.fetch_all(build_queries(plan), max_trials=max_trials)

    groups, reports = [], []
    for r in fetches:
        trials, report = normalize_all(r.studies)
        reports.append(report)
        groups.append(Group(label=r.query.label, filters=r.query.filters, trials=trials,
                            total_count=r.total_count, drug_aliases=report.drug_aliases))

    warnings, count_requests = [], 0
    truncated = [(g, r) for g, r in zip(groups, fetches) if g.truncated]
    if truncated and uses_exact_counts(plan):
        count_requests = await _add_exact_counts(plan, groups, truncated, client, max_count_requests,
                                                 today, warnings)

    result = aggregate(plan, groups, today=today)
    if warnings:
        result = result.model_copy(update={"notes": warnings + result.notes})
    return PipelineResult(result=result, fetches=fetches, reports=reports,
                          exact_count_requests=count_requests, warnings=warnings)


async def _count_keys(plan, groups, truncated, client, today) -> tuple[list[str], int]:
    """Which values to count, and how many requests it took to decide.

    Time series: every year in the chart's range (same year_range rule as the chart).
    The download is sorted newest first, so its latest year is the true latest; the
    earliest comes from one sorted request per truncated group.
    Bar charts: the values seen in the downloaded sample. A value too rare to appear
    in the sample is not counted; it shows up in the "not in the bars shown" note.
    """
    spec = DIMENSIONS[plan.group_by]
    if plan.chart == ChartType.TIME_SERIES:
        firsts = await asyncio.gather(*(first_start_year(client, r.query) for _, r in truncated))
        years = [t.start_year for g in groups for t in g.trials if t.start_year] + [y for y in firsts if y]
        if not years:
            return [], len(truncated)
        lo, hi = year_range(plan.filters.start_year_min, plan.filters.start_year_max,
                            min(years), max(years), today.year)
        return [str(y) for y in range(lo, hi + 1)], len(truncated)
    keys = {ev.key for g in groups for t in g.trials for ev in spec.extract(t, Context())}
    return sorted(keys), 0


async def _add_exact_counts(plan, groups, truncated, client, budget, today, warnings) -> int:
    keys, spent = await _count_keys(plan, groups, truncated, client, today)
    needed = len(keys) * len(truncated)
    if not keys or needed > budget:
        warnings.append(f"Exact counts would need {needed} API requests (limit {budget}); "
                        "counts are from the downloaded sample.")
        return spent
    results = await asyncio.gather(*(exact_counts(client, r.query, plan.group_by, keys, r.total_count)
                                     for _, r in truncated))
    for (g, _), exact in zip(truncated, results):
        g.exact = exact
    log.info("exact counts for %d groups with %d requests", len(truncated), needed)
    return spent + needed
