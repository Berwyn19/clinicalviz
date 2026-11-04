"""The plan: the only thing the LLM produces.

Two layers of checking:
1. Structure: OpenAI structured outputs force the LLM's JSON to match QueryPlan
   (right field names, enum values only).
2. Logic: `find_plan_errors` checks that the fields make sense together
   (e.g. a histogram needs a numeric field). Errors are sent back to the LLM
   so it can fix the plan.

Note: no pydantic validators on these models on purpose. If a validator raised
inside the OpenAI SDK's parse step we'd lose a clean, readable error message
to feed back to the LLM. Rules live in `find_plan_errors` instead.
"""

from typing import Literal

from pydantic import BaseModel, Field

from .common import (
    NETWORK_NODE_DIMENSIONS,
    ChartType,
    Dimension,
    Intent,
    NumericField,
    OverallStatus,
    Phase,
)
from .request import MAX_YEAR, MIN_YEAR

MAX_COMPARE_GROUPS = 5
MAX_LIMIT = 50


class Filters(BaseModel):
    drug: str | None = Field(None, description="Drug/intervention name, e.g. 'pembrolizumab'.")
    condition: str | None = Field(None, description="Disease/condition, e.g. 'breast cancer'.")
    sponsor: str | None = Field(None, description="Sponsor organization name.")
    country: str | None = Field(None, description="Country where the trial has a site.")
    phases: list[Phase] | None = None
    statuses: list[OverallStatus] | None = None
    start_year_min: int | None = None
    start_year_max: int | None = None


class CompareGroup(BaseModel):
    label: str = Field(..., description="Short label shown in the legend, e.g. 'Pembrolizumab'.")
    filters: Filters = Field(..., description="Filters for this group, applied on top of the base filters.")


class NetworkSpec(BaseModel):
    source: Dimension
    target: Dimension = Field(..., description="Same as source for co-occurrence (e.g. drug-drug).")


SortOrder = Literal["value_desc", "value_asc", "key_asc"]


class QueryPlan(BaseModel):
    intent: Intent
    reason: str | None = Field(None, description="Required if intent is 'unsupported'.")
    chart: ChartType | None = None
    filters: Filters
    group_by: Dimension | None = None
    compare: list[CompareGroup] | None = None
    numeric_field: NumericField | None = Field(None, description="Histogram only.")
    x_field: NumericField | None = Field(None, description="Scatter plot only.")
    y_field: NumericField | None = Field(None, description="Scatter plot only.")
    network: NetworkSpec | None = Field(None, description="Network graph only.")
    sort: SortOrder | None = None
    limit: int | None = Field(None, description=f"Top-N cutoff, 1-{MAX_LIMIT}.")
    assumptions: list[str] = Field(..., description="How ambiguous parts of the question were interpreted.")


# ---------------------------------------------------------------- logic rules

def _check_filters(f: Filters, where: str) -> list[str]:
    errors = []
    for name in ("start_year_min", "start_year_max"):
        year = getattr(f, name)
        if year is not None and not (MIN_YEAR <= year <= MAX_YEAR):
            errors.append(f"{where}.{name}={year} is outside {MIN_YEAR}-{MAX_YEAR}.")
    if f.start_year_min and f.start_year_max and f.start_year_min > f.start_year_max:
        errors.append(f"{where}: start_year_min is after start_year_max.")
    return errors


def find_plan_errors(plan: QueryPlan) -> list[str]:
    """Return human-readable problems with the plan. Empty list = valid."""
    if plan.intent == Intent.UNSUPPORTED:
        return [] if plan.reason else ["intent is 'unsupported' but no reason was given."]

    errors = _check_filters(plan.filters, "filters")
    for i, group in enumerate(plan.compare or []):
        errors += _check_filters(group.filters, f"compare[{i}].filters")

    if plan.limit is not None and not (1 <= plan.limit <= MAX_LIMIT):
        errors.append(f"limit must be between 1 and {MAX_LIMIT}.")

    if plan.intent == Intent.TRIAL_LIST:
        if plan.chart not in (None, ChartType.TABLE):
            errors.append("intent 'trial_list' must use chart 'table'.")
        return errors

    # intent == CHART
    chart = plan.chart
    compare = plan.compare or []

    if chart is None or chart == ChartType.TABLE:
        errors.append("intent 'chart' needs a chart type other than 'table'.")
        return errors

    if compare:
        labels = [g.label for g in compare]
        if len(compare) < 2 or len(compare) > MAX_COMPARE_GROUPS:
            errors.append(f"compare needs 2-{MAX_COMPARE_GROUPS} groups.")
        if len(set(labels)) != len(labels):
            errors.append("compare group labels must be unique.")

    if chart == ChartType.BAR_CHART:
        if plan.group_by is None:
            errors.append("bar_chart needs group_by.")
        if compare:
            errors.append("bar_chart cannot use compare; use grouped_bar_chart.")

    elif chart == ChartType.GROUPED_BAR_CHART:
        if plan.group_by is None:
            errors.append("grouped_bar_chart needs group_by.")
        if not compare:
            errors.append("grouped_bar_chart needs compare groups.")

    elif chart == ChartType.TIME_SERIES:
        if plan.group_by != Dimension.START_YEAR:
            errors.append("time_series needs group_by='start_year'.")

    elif chart == ChartType.HISTOGRAM:
        if plan.numeric_field is None:
            errors.append("histogram needs numeric_field.")

    elif chart == ChartType.SCATTER_PLOT:
        if plan.x_field is None or plan.y_field is None:
            errors.append("scatter_plot needs x_field and y_field.")
        elif plan.x_field == plan.y_field:
            errors.append("scatter_plot x_field and y_field must differ.")

    elif chart == ChartType.NETWORK_GRAPH:
        if plan.network is None:
            errors.append("network_graph needs network.source and network.target.")
        else:
            allowed = sorted(d.value for d in NETWORK_NODE_DIMENSIONS)
            for end in (plan.network.source, plan.network.target):
                if end not in NETWORK_NODE_DIMENSIONS:
                    errors.append(f"network node type '{end.value}' not allowed; use one of {allowed}.")

    return errors