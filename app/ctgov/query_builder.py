"""Turn a QueryPlan into ClinicalTrials.gov API v2 parameters.

Relevance is decided by the API itself, not by post-filtering:
each text filter (drug, condition, sponsor, country) is searched ONLY in the
structured fields that prove a trial really involves it, using AREA[Field](value).
The API still expands synonyms inside those fields ("heart attack" matches
"Myocardial Infarction"), but ignores titles, descriptions, and keywords.

The broad search (query.intr etc., which also searches titles and descriptions)
is kept only to report how many trials were left out, in the response metadata.

Only the plan's filters are sent to the API. group_by / chart / assumptions are
used later by the aggregator. A compare plan becomes one query per group.
"""

import re
from dataclasses import dataclass

from app.schemas.common import ChartType, Dimension, Intent, NumericField
from app.schemas.plan import Filters, QueryPlan

# Text filter -> the structured fields that count as evidence for it.
# One row per filter TYPE; the same rule applies to every question.
STRICT_FIELDS = {
    "drug": ["InterventionName", "InterventionOtherName", "InterventionMeshTerm"],
    "condition": ["Condition", "ConditionMeshTerm"],
    "sponsor": ["LeadSponsorName", "CollaboratorName"],
    "country": ["LocationCountry"],
}

# Text filter -> the API's broad search parameter (used only for the "left out" count).
BROAD_PARAMS = {
    "drug": "query.intr",
    "condition": "query.cond",
    "sponsor": "query.spons",
    "country": "query.locn",
}

# Always fetched: needed for citations, the trial table, and basic sanity checks.
BASE_FIELDS = ["NCTId", "BriefTitle", "OverallStatus", "StartDate", "Phase", "StudyType"]

# MeSH terms are the registry's standard drug names; the normalizer uses them to merge synonyms.
INTERVENTION_FIELDS = ["InterventionName", "InterventionOtherName", "InterventionType", "InterventionMeshTerm"]

# Extra fields needed to group by each dimension.
DIMENSION_FIELDS = {
    Dimension.PHASE: ["Phase"],
    Dimension.START_YEAR: ["StartDate"],
    Dimension.STATUS: ["OverallStatus"],
    Dimension.COUNTRY: ["LocationCountry", "LocationStatus"],
    Dimension.LEAD_SPONSOR: ["LeadSponsorName"],
    Dimension.SPONSOR_CLASS: ["LeadSponsorClass"],
    Dimension.INTERVENTION_TYPE: INTERVENTION_FIELDS,
    Dimension.INTERVENTION: INTERVENTION_FIELDS,
    Dimension.CONDITION: ["Condition"],
    Dimension.STUDY_TYPE: ["StudyType"],
}

NUMERIC_FIELDS = {
    NumericField.ENROLLMENT: ["EnrollmentCount"],
    NumericField.DURATION_MONTHS: ["StartDate", "CompletionDate"],
    NumericField.START_YEAR: ["StartDate"],
}

TABLE_FIELDS = ["LeadSponsorName", "Condition", "EnrollmentCount", *INTERVENTION_FIELDS]

SAMPLE_SORT = "StartDate:desc"


@dataclass(frozen=True)
class ApiQuery:
    label: str | None                     # compare-group label, None for a single query
    filters: Filters                      # merged filters this query was built from
    params: dict[str, str]                # strict search; paging params are added by the client
    broad_params: dict[str, str] | None = None  # broad search, only used to count left-out trials


# ------------------------------------------------------------------ building blocks

def sanitize(value: str) -> str:
    """Make a user value safe to put inside the API's query language.

    Removes brackets, parentheses, quotes and other symbols, and lowercases
    everything so words like OR / AND / NOT can't act as operators.
    """
    cleaned = re.sub(r"[^\w\s\-.,']", " ", value)
    cleaned = " ".join(cleaned.split()).strip(" -.,'").lower()
    if not cleaned:
        raise ValueError(f"filter value {value!r} is empty after cleaning")
    return cleaned


def area_group(fields: list[str], value: str) -> str:
    """('drug' fields, 'pembrolizumab') -> (AREA[InterventionName](pembrolizumab) OR ...)"""
    v = sanitize(value)
    return "(" + " OR ".join(f"AREA[{field}]({v})" for field in fields) + ")"


def structural_conditions(f: Filters) -> list[str]:
    """Phase and date conditions: identical for strict and broad searches."""
    conditions = []
    if f.phases:
        conditions.append("AREA[Phase](" + " OR ".join(p.value for p in f.phases) + ")")
    if f.start_year_min or f.start_year_max:
        lo = f"{f.start_year_min}-01-01" if f.start_year_min else "MIN"
        hi = f"{f.start_year_max}-12-31" if f.start_year_max else "MAX"
        conditions.append(f"AREA[StartDate]RANGE[{lo},{hi}]")
    return conditions


def _with_common(params: dict[str, str], advanced: list[str], f: Filters) -> dict[str, str]:
    if advanced:
        params["filter.advanced"] = " AND ".join(advanced)
    if f.statuses:
        params["filter.overallStatus"] = ",".join(s.value for s in f.statuses)
    return params


def strict_params(f: Filters) -> dict[str, str]:
    advanced = [area_group(fields, getattr(f, name))
                for name, fields in STRICT_FIELDS.items() if getattr(f, name)]
    return _with_common({}, advanced + structural_conditions(f), f)


def broad_params(f: Filters) -> dict[str, str]:
    params = {BROAD_PARAMS[name]: sanitize(getattr(f, name))
              for name in BROAD_PARAMS if getattr(f, name)}
    return _with_common(params, structural_conditions(f), f)


def has_text_filter(f: Filters) -> bool:
    return any(getattr(f, name) for name in STRICT_FIELDS)


# ------------------------------------------------------------------ plan -> queries

def merge_filters(base: Filters, override: Filters) -> Filters:
    """Group filters win over shared filters, field by field."""
    changes = {k: v for k, v in override.model_dump().items() if v is not None}
    return base.model_copy(update=changes)


def select_fields(plan: QueryPlan, filters: Filters) -> list[str]:
    """Only ask the API for fields this plan will actually use."""
    fields = list(BASE_FIELDS)
    for name, strict in STRICT_FIELDS.items():
        if getattr(filters, name):
            fields += strict          # the evidence fields double as citation sources
    if plan.group_by:
        fields += DIMENSION_FIELDS[plan.group_by]
    for numeric in (plan.numeric_field, plan.x_field, plan.y_field):
        if numeric:
            fields += NUMERIC_FIELDS[numeric]
    if plan.network:
        fields += DIMENSION_FIELDS[plan.network.source] + DIMENSION_FIELDS[plan.network.target]
    if plan.chart == ChartType.TABLE:
        fields += TABLE_FIELDS
    return list(dict.fromkeys(fields))  # dedupe, keep order


def build_queries(plan: QueryPlan) -> list[ApiQuery]:
    if plan.intent == Intent.UNSUPPORTED:
        raise ValueError("cannot build API queries for an unsupported plan")

    queries = []
    for group in plan.compare or [None]:
        filters = merge_filters(plan.filters, group.filters) if group else plan.filters
        params = strict_params(filters)
        params["fields"] = ",".join(select_fields(plan, filters))
        # Newest first: if the download is cut at max_trials, the sample is "the N trials with
        # the latest start dates" (describable) instead of an arbitrary slice.
        params["sort"] = SAMPLE_SORT
        queries.append(ApiQuery(
            label=group.label if group else None,
            filters=filters,
            params=params,
            # Only worth a second request if strict vs broad can actually differ.
            broad_params=broad_params(filters) if has_text_filter(filters) else None,
        ))
    return queries