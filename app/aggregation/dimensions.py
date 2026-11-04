"""How to read each dimension / number field from a normalized Trial.

This is the only place that knows about individual dimensions. The aggregator
functions are generic: they call `extract` and never check which dimension
they are grouping by. Each value comes with the exact API field and raw text
it was read from, which become the citations.
"""

from dataclasses import dataclass, field
from typing import Callable, Literal

from app.processing.normalizer import make_key
from app.schemas.common import Dimension, NumericField, Phase
from app.schemas.request import MIN_YEAR
from app.schemas.trial import Trial


@dataclass(frozen=True)
class Evidence:
    key: str        # grouping key
    label: str      # display label
    field: str      # API field path the value was read from
    excerpt: str    # exact raw value


@dataclass(frozen=True)
class Context:
    """Request-level facts some extractors need (e.g. site status filtering)."""
    statuses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DimensionSpec:
    extract: Callable[[Trial, Context], list[Evidence]]
    multi_valued: bool                 # can one trial have several values?
    order: list[str] | None = None     # fixed key order; None = order by count
    # API search condition matching trials whose value is exactly this key. Set only when
    # the API can count a value exactly, so truncated downloads can use exact counts.
    api_condition: Callable[[str], str] | None = None
    label_of: Callable[[str], str] = str   # display label for a key (when no trial supplied one)


@dataclass(frozen=True)
class NumericSpec:
    extract: Callable[[Trial], tuple[float, Evidence] | None]
    unit: str
    scale: Literal["linear", "log"]
    # Histogram bins: (start inclusive, end exclusive or None, label). None = one bin per integer.
    bins: list[tuple[float, float | None, str]] | None = field(default=None)


# ------------------------------------------------------------------ labels

PHASE_ORDER = ["EARLY_PHASE1", "EARLY_PHASE1/PHASE1", "PHASE1", "PHASE1/PHASE2", "PHASE2",
               "PHASE2/PHASE3", "PHASE3", "PHASE3/PHASE4", "PHASE4", "NA"]
_PHASE_NAMES = {"EARLY_PHASE1": "Early Phase 1", "PHASE1": "1", "PHASE2": "2",
                "PHASE3": "3", "PHASE4": "4"}


def phase_label(key: str) -> str:
    """'PHASE1/PHASE2' -> 'Phase 1/2', 'NA' -> 'N/A'"""
    if key == "NA":
        return "N/A"
    parts = [_PHASE_NAMES.get(p, p) for p in key.split("/")]
    if parts[0] == "Early Phase 1":
        return " / ".join(["Early Phase 1"] + [f"Phase {p}" for p in parts[1:]])
    return "Phase " + "/".join(parts)


def pretty(code: str) -> str:
    """'ACTIVE_NOT_RECRUITING' -> 'Active not recruiting'"""
    return code.replace("_", " ").capitalize()


def _one(key, label, field_path, excerpt) -> list[Evidence]:
    return [Evidence(key, label, field_path, str(excerpt))]


def year_range(start_min: int | None, start_max: int | None, first: int, last: int, current: int
               ) -> tuple[int, int]:
    """Years a time series shows. The one rule, used for downloaded data and for API counts.

    Filter years win. Otherwise from the first start year (not before MIN_YEAR) to the
    current year. If only future years are asked for, show them.
    """
    lo = start_min or max(first, MIN_YEAR)
    hi = start_max or min(last, current)
    if lo > hi:
        hi = max(lo, last)
    return lo, hi


# ------------------------------------------------------------------ API conditions

_PHASES = [p.value for p in Phase if p != Phase.NA]


def _phase_condition(key: str) -> str:
    """Exactly this combination, as the normalizer labels it: 'PHASE1/PHASE2' = both and no other
    phase; 'NA' = none of the phases."""
    if key == "NA":
        return "NOT AREA[Phase](" + " OR ".join(_PHASES) + ")"
    wanted = key.split("/")
    return " AND ".join([f"AREA[Phase]({p})" for p in wanted]
                        + [f"NOT AREA[Phase]({p})" for p in _PHASES if p not in wanted])


def _field_condition(api_field: str) -> Callable[[str], str]:
    return lambda key: f"AREA[{api_field}]({key})"


def _year_condition(key: str) -> str:
    return f"AREA[StartDate]RANGE[{key}-01-01,{key}-12-31]"


# ------------------------------------------------------------------ extractors

def _phase(t, ctx):
    return _one(t.phase, phase_label(t.phase), "protocolSection.designModule.phases",
                ", ".join(t.phases) or "(no phase listed)")


def _start_year(t, ctx):
    if not t.start_year:
        return []
    return _one(str(t.start_year), str(t.start_year),
                "protocolSection.statusModule.startDateStruct.date", t.start_date)


def _status(t, ctx):
    return _one(t.status, pretty(t.status), "protocolSection.statusModule.overallStatus",
                t.status) if t.status else []


def _country(t, ctx):
    """Site-level: if the question filters by status, only count sites with that status.

    A site without its own status falls back to the trial's overall status.
    """
    countries = set()
    for site in t.sites:
        status = site.status or t.status
        if not ctx.statuses or status in ctx.statuses:
            countries.add(site.country)
    return [Evidence(make_key(c), c, "protocolSection.contactsLocationsModule.locations[].country", c)
            for c in sorted(countries)]


def _lead_sponsor(t, ctx):
    if not t.lead_sponsor_key:
        return []
    return _one(t.lead_sponsor_key, t.lead_sponsor_display,
                "protocolSection.sponsorCollaboratorsModule.leadSponsor.name", t.lead_sponsor)


def _sponsor_class(t, ctx):
    return _one(t.sponsor_class, pretty(t.sponsor_class),
                "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
                t.sponsor_class) if t.sponsor_class else []


def _intervention_type(t, ctx):
    types = sorted({i.type for i in t.interventions if i.type})
    return [Evidence(x, pretty(x), "protocolSection.armsInterventionsModule.interventions[].type", x)
            for x in types]


def _intervention(t, ctx):
    found: dict[str, Evidence] = {}
    for iv in t.interventions:
        if not iv.is_drug_like:
            continue
        single = len(iv.components) == 1
        displays = iv.component_displays if len(iv.component_displays) == len(iv.components) else []
        for i, comp in enumerate(iv.components):
            label = displays[i] if displays else (iv.display if single else comp[:1].upper() + comp[1:])
            ev = Evidence(comp, label, "protocolSection.armsInterventionsModule.interventions[].name", iv.name)
            # Prefer evidence from a single-drug entry over a combined "A and B" entry.
            if comp not in found or single:
                found[comp] = ev
    return list(found.values())


def _condition(t, ctx):
    found = {}
    for c in t.conditions:
        found.setdefault(make_key(c), Evidence(make_key(c), c, "protocolSection.conditionsModule.conditions[]", c))
    return list(found.values())


def _study_type(t, ctx):
    return _one(t.study_type, pretty(t.study_type), "protocolSection.designModule.studyType",
                t.study_type) if t.study_type else []


DIMENSIONS: dict[Dimension, DimensionSpec] = {
    Dimension.PHASE: DimensionSpec(_phase, multi_valued=False, order=PHASE_ORDER, api_condition=_phase_condition,
                                   label_of=phase_label),
    Dimension.START_YEAR: DimensionSpec(_start_year, multi_valued=False, api_condition=_year_condition),
    Dimension.STATUS: DimensionSpec(_status, multi_valued=False, api_condition=_field_condition("OverallStatus"),
                                    label_of=pretty),
    Dimension.COUNTRY: DimensionSpec(_country, multi_valued=True),
    Dimension.LEAD_SPONSOR: DimensionSpec(_lead_sponsor, multi_valued=False),
    Dimension.SPONSOR_CLASS: DimensionSpec(_sponsor_class, multi_valued=False,
                                           api_condition=_field_condition("LeadSponsorClass"), label_of=pretty),
    Dimension.INTERVENTION_TYPE: DimensionSpec(_intervention_type, multi_valued=True),
    Dimension.INTERVENTION: DimensionSpec(_intervention, multi_valued=True),
    Dimension.CONDITION: DimensionSpec(_condition, multi_valued=True),
    Dimension.STUDY_TYPE: DimensionSpec(_study_type, multi_valued=False,
                                        api_condition=_field_condition("StudyType"), label_of=pretty),
}

# Which filter a dimension's values can match (used to mark hub nodes in networks).
FILTER_FOR_DIMENSION = {
    Dimension.INTERVENTION: "drug",
    Dimension.CONDITION: "condition",
    Dimension.LEAD_SPONSOR: "sponsor",
    Dimension.COUNTRY: "country",
}


# ------------------------------------------------------------------ numbers

def _enrollment(t):
    if t.enrollment is None:
        return None
    return t.enrollment, Evidence("", "", "protocolSection.designModule.enrollmentInfo.count", str(t.enrollment))


def _duration(t):
    if t.duration_months is None:
        return None
    return t.duration_months, Evidence(
        "", "", "protocolSection.statusModule.startDateStruct.date & completionDateStruct.date",
        f"start {t.start_date}, completion {t.completion_date}")


def _start_year_num(t):
    if not t.start_year:
        return None
    return t.start_year, Evidence("", "", "protocolSection.statusModule.startDateStruct.date", t.start_date)


ENROLLMENT_BINS = [
    (0, 1, "0"), (1, 11, "1–10"), (11, 51, "11–50"), (51, 101, "51–100"), (101, 501, "101–500"),
    (501, 1001, "501–1,000"), (1001, 5001, "1,001–5,000"), (5001, 10001, "5,001–10,000"),
    (10001, None, "10,001+"),
]
DURATION_BINS = [(m, m + 12, f"{m}–{m + 12}") for m in range(0, 120, 12)] + [(120, None, "120+")]

NUMERICS: dict[NumericField, NumericSpec] = {
    NumericField.ENROLLMENT: NumericSpec(_enrollment, unit="participants", scale="log", bins=ENROLLMENT_BINS),
    NumericField.DURATION_MONTHS: NumericSpec(_duration, unit="months", scale="linear", bins=DURATION_BINS),
    NumericField.START_YEAR: NumericSpec(_start_year_num, unit="year", scale="linear", bins=None),
}