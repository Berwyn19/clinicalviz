"""Aggregator output -> the response a frontend renders: {visualization, meta}.

visualization = {type, title, encoding, data}
  - data: flat records named after what they hold ({"phase": "Phase 2", "trial_count": 78}).
  - encoding: which record field goes on which visual channel, plus axis titles, value order,
    scale and units, so a renderer never has to guess.
meta = how the question was read, filters, notes, coverage, and the API requests behind the data.

This is also where size is decided: trial ID lists are capped per datum (`trial_count`
keeps the true total and `nct_ids_complete` says whether the list is cut).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.aggregation.dimensions import NUMERICS, phase_label, pretty
from app.aggregation.models import AggregateResult, Datum
from app.schemas.common import ChartType, Dimension, Intent, NumericField
from app.schemas.plan import Filters, QueryPlan

MAX_IDS_PER_DATUM = 20
MAX_CITATIONS_PER_DATUM = 3

DIM_TITLES = {
    Dimension.PHASE: "Phase", Dimension.START_YEAR: "Start year", Dimension.STATUS: "Status",
    Dimension.COUNTRY: "Country", Dimension.LEAD_SPONSOR: "Lead sponsor", Dimension.SPONSOR_CLASS: "Sponsor type",
    Dimension.INTERVENTION_TYPE: "Intervention type", Dimension.INTERVENTION: "Drug / intervention",
    Dimension.CONDITION: "Condition", Dimension.STUDY_TYPE: "Study type",
}
NUM_TITLES = {NumericField.ENROLLMENT: "Enrollment", NumericField.DURATION_MONTHS: "Duration",
              NumericField.START_YEAR: "Start year"}


class Visualization(BaseModel):
    type: Literal["bar_chart", "grouped_bar_chart", "time_series", "histogram", "scatter_plot",
                  "network_graph", "table"]
    title: str
    encoding: dict[str, Any] = Field(..., description="Visual channel -> {field, type, title, ...}.")
    data: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] = Field(
        ..., description="Records; for network_graph {nodes: [...], edges: [...]}.")


class Source(BaseModel):
    label: str | None = None
    request_url: str             # the strict search sent to ClinicalTrials.gov
    matched: int                 # trials the API matched
    downloaded: int
    broad_matched: int | None = None  # broad text search total (incl. text-only mentions)


class Meta(BaseModel):
    source: str = "clinicaltrials.gov"
    query: str | None = None
    plan: QueryPlan = Field(..., description="How the question was read (the LLM's validated plan).")
    filters: dict[str, Any] = Field(..., description="Filters applied to all series.")
    series_filters: dict[str, dict[str, Any]] | None = Field(None, description="Compare groups: label -> filters.")
    assumptions: list[str] = []
    notes: list[str] = []
    count_source: Literal["downloaded", "api_counts", "sample"] | None = Field(
        None, description="downloaded: every matching trial counted; api_counts: download was capped, values "
                          "are exact per-value API counts; sample: download was capped, values count the sample.")
    sort: str | None = None
    limit: int | None = None
    time_granularity: Literal["year"] | None = None
    stats: dict[str, Any] = {}
    sources: list[Source] = []
    timings: dict[str, float] = {}
    reason: str | None = Field(None, description="Why the question is unsupported.")


class QueryResponse(BaseModel):
    status: Literal["ok", "unsupported"]
    visualization: Visualization | None = None
    meta: Meta


# ------------------------------------------------------------------ titles

def _clean(f: Filters) -> dict[str, Any]:
    return f.model_dump(mode="json", exclude_none=True)


def subject(f: Filters) -> str:
    """Filters -> 'Recruiting phase 3 psoriasis trials of adalimumab in Germany since 2015'."""
    words = []
    if f.statuses:
        words.append(" / ".join(pretty(s.value) for s in f.statuses).lower())
    if f.phases:
        words.append(" / ".join(phase_label(p.value) for p in f.phases).lower())
    if f.condition:
        words.append(f.condition)
    words.append("trials")
    if f.drug:
        words.append(f"of {f.drug}")
    if f.sponsor:
        words.append(f"by {f.sponsor}")
    if f.country:
        words.append(f"in {f.country}")
    lo, hi = f.start_year_min, f.start_year_max
    if lo and hi:
        words.append(f"started in {lo}" if lo == hi else f"started {lo}-{hi}")
    elif lo:
        words.append(f"started since {lo}")
    elif hi:
        words.append(f"started up to {hi}")
    text = " ".join(words)
    return text[0].upper() + text[1:]


def title(plan: QueryPlan) -> str:
    groups = [g.label for g in plan.compare or []]
    who = subject(plan.filters)
    if groups:
        who = f"{' vs '.join(groups)} trials" if who == "Trials" else f"{who}: {' vs '.join(groups)}"
    chart = plan.chart if plan.intent == Intent.CHART else ChartType.TABLE
    if chart in (ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART):
        by = DIM_TITLES[plan.group_by].lower()
        top = f"top {plan.limit} " if plan.limit and plan.sort != "value_asc" else ""
        return f"{who}, {top}by {by}"
    if chart == ChartType.TIME_SERIES:
        return f"{who}, by start year"
    if chart == ChartType.HISTOGRAM:
        return f"{NUM_TITLES[plan.numeric_field]} distribution: {who}"
    if chart == ChartType.SCATTER_PLOT:
        return f"{NUM_TITLES[plan.y_field]} vs {NUM_TITLES[plan.x_field].lower()}: {who}"
    if chart == ChartType.NETWORK_GRAPH:
        a, b = DIM_TITLES[plan.network.source], DIM_TITLES[plan.network.target]
        return f"{a} network: {who}" if a == b else f"{a} and {b.lower()} network: {who}"
    return who


# ------------------------------------------------------------------ data

def _ids(nct_ids: list[str], count: int, complete: bool = True) -> dict[str, Any]:
    shown = nct_ids[:MAX_IDS_PER_DATUM]
    return {"nct_ids": shown, "nct_ids_complete": complete and len(shown) == count}


def _cites(d) -> list[dict]:
    return [c.model_dump() for c in d.citations[:MAX_CITATIONS_PER_DATUM]]


def _hoist_citation_field(records: list[dict], encoding: dict) -> None:
    """If every citation names the same API field, state it once in <encoding>.citations.field."""
    cites = [c for rec in records for c in rec.get("citations", [])]
    fields = {c["field"] for c in cites}
    if len(fields) == 1:
        for c in cites:
            del c["field"]
        encoding["citations"] = {"field": fields.pop()}


def _row(d: Datum, x_field: str, series: bool) -> dict[str, Any]:
    rec = {x_field: d.x, "trial_count": d.trial_count}
    if series:
        rec["series"] = d.series
    if d.is_partial is not None:
        rec["is_partial"] = d.is_partial
    if d.bin_start is not None:
        rec["bin_start"], rec["bin_end"] = d.bin_start, d.bin_end
    return {**rec, **_ids(d.nct_ids, d.trial_count, d.nct_ids_complete), "citations": _cites(d)}


def _axis(field: str, kind: str, label: str, **extra) -> dict[str, Any]:
    return {"field": field, "type": kind, "title": label, **{k: v for k, v in extra.items() if v is not None}}


def _color(r: AggregateResult) -> dict[str, Any]:
    return {"color": _axis("series", "nominal", "Group", domain=r.series)} if r.series else {}


def _count_axis() -> dict[str, Any]:
    return _axis("trial_count", "quantitative", "Trials")


def encode(plan: QueryPlan, r: AggregateResult) -> tuple[str, dict, Any]:
    """-> (type, encoding, data)"""
    if r.kind == "table":
        cols = [("nct_id", "Trial"), ("title", "Title"), ("status", "Status"), ("phase", "Phase"),
                ("start_date", "Start date"), ("lead_sponsor", "Lead sponsor"), ("conditions", "Conditions"),
                ("interventions", "Interventions"), ("countries", "Countries"), ("url", "Link")]
        cols += [("series", "Group")] if r.series else []
        return "table", {"columns": [{"field": f, "title": t} for f, t in cols]}, \
            [row.model_dump(exclude_none=True) for row in r.table]

    if r.kind == "network":
        nodes = [{"id": n.id, "label": n.label, "type": n.type, "weight": n.weight,
                  "is_query_match": n.is_query_match, **_ids(n.nct_ids, n.weight), "citations": _cites(n)}
                 for n in r.nodes]
        edges = [{"source": e.source, "target": e.target, "weight": e.weight,
                  **_ids(e.nct_ids, e.weight), "citations": _cites(e)} for e in r.edges]
        enc = {"nodes": {"id": "id", "label": "label", "size": _axis("weight", "quantitative", "Trials"),
                         "color": _axis("type", "nominal", "Entity type"), "highlight": "is_query_match"},
               "edges": {"source": "source", "target": "target", "width": _axis("weight", "quantitative",
                                                                                "Trials with both")}}
        return "network_graph", enc, {"nodes": nodes, "edges": edges}

    chart = plan.chart
    series = bool(r.series)
    if chart == ChartType.SCATTER_PLOT:
        xs, ys = NUMERICS[plan.x_field], NUMERICS[plan.y_field]
        xf, yf = plan.x_field.value, plan.y_field.value
        data = [{"nct_id": d.nct_ids[0], "title": d.label, xf: d.x, yf: d.y,
                 **({"series": d.series} if series else {}), "citations": _cites(d)} for d in r.rows]
        enc = {"x": _axis(xf, "quantitative", NUM_TITLES[plan.x_field], unit=xs.unit, scale=xs.scale),
               "y": _axis(yf, "quantitative", NUM_TITLES[plan.y_field], unit=ys.unit, scale=ys.scale),
               "tooltip": ["title", "nct_id"], **_color(r)}
        return "scatter_plot", enc, data

    if chart == ChartType.HISTOGRAM:
        unit = NUMERICS[plan.numeric_field].unit
        enc = {"x": _axis("bin", "ordinal", f"{NUM_TITLES[plan.numeric_field]} ({unit})", sort=r.categories,
                          unit=unit, bin_start="bin_start", bin_end="bin_end"),
               "y": _count_axis(), **_color(r)}
        return "histogram", enc, [_row(d, "bin", series) for d in r.rows]

    if chart == ChartType.TIME_SERIES:
        enc = {"x": _axis("start_year", "temporal", "Start year", time_unit="year", sort=r.categories),
               "y": _count_axis(), "partial": "is_partial", **_color(r)}
        return "time_series", enc, [_row(d, "start_year", series) for d in r.rows]

    field = plan.group_by.value
    enc = {"x": _axis(field, "nominal", DIM_TITLES[plan.group_by], sort=r.categories), "y": _count_axis(),
           **_color(r)}
    return chart.value, enc, [_row(d, field, series) for d in r.rows]


def count_source(stats: dict) -> str | None:
    if "matched" not in stats:
        return None
    if stats.get("sampled"):
        return "sample"
    return "api_counts" if stats.get("exact_counts") else "downloaded"


def build_response(plan: QueryPlan, r: AggregateResult | None, query: str | None = None,
                   sources: list[Source] | None = None, timings: dict | None = None) -> QueryResponse:
    meta = Meta(
        query=query, plan=plan, filters=_clean(plan.filters),
        series_filters={g.label: _clean(g.filters) for g in plan.compare} if plan.compare else None,
        assumptions=plan.assumptions, sort=plan.sort, limit=plan.limit,
        time_granularity="year" if plan.chart == ChartType.TIME_SERIES else None,
        sources=sources or [], timings=timings or {}, reason=plan.reason,
    )
    if plan.intent == Intent.UNSUPPORTED or r is None:
        return QueryResponse(status="unsupported", meta=meta)
    stats = {k: v for k, v in r.stats.items() if k != "exact_counts"}
    meta = meta.model_copy(update={"notes": r.notes, "stats": stats, "count_source": count_source(r.stats)})
    vtype, enc, data = encode(plan, r)
    if isinstance(data, dict):   # network: nodes and edges cite different fields
        _hoist_citation_field(data["nodes"], enc["nodes"])
        _hoist_citation_field(data["edges"], enc["edges"])
    else:
        _hoist_citation_field(data, enc)
    return QueryResponse(status="ok", visualization=Visualization(type=vtype, title=title(plan), encoding=enc,
                                                                  data=data), meta=meta)
