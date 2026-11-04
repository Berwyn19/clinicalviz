"""What the aggregator returns. The spec builder turns this into the final response.

Every chart except network and table uses the same row shape (Datum), so the
spec builder and frontend handle very few cases.
"""

from typing import Any, Literal

from pydantic import BaseModel


class Citation(BaseModel):
    nct_id: str
    field: str        # API field path, e.g. "protocolSection.designModule.phases"
    excerpt: str      # exact raw value from the API


class ExactCounts(BaseModel):
    """Per-bucket trial counts straight from the API, for one group.

    Used when the download was truncated: the downloaded trials are then only a sample,
    but the API can still count every bucket of a single-valued dimension exactly.
    """
    counts: dict[str, int]                 # dimension key ("PHASE2", "RECRUITING", "2020") -> trials
    total: int                             # trials matching the group's query

    @property
    def not_counted(self) -> int:
        """Trials in none of the counted buckets: no value, a value outside the counted
        keys (e.g. a year outside the range, a rare value absent from the sample)."""
        return max(0, self.total - sum(self.counts.values()))


class Datum(BaseModel):
    x: str | int | float               # category label, year, bin label, or number (scatter)
    y: int | float                     # trial count, or a number (scatter)
    series: str | None = None          # compare-group label; None for single-series charts
    trial_count: int
    nct_ids: list[str]                 # trials behind this datum (see nct_ids_complete)
    nct_ids_complete: bool = True      # False when y is an exact API count but only a sample was downloaded
    citations: list[Citation]          # first few, with exact excerpts
    bin_start: float | None = None     # histogram only (inclusive)
    bin_end: float | None = None       # histogram only (exclusive; None = open-ended)
    is_partial: bool | None = None     # time series: current, incomplete year
    label: str | None = None           # tooltip text (scatter: trial title)


class Node(BaseModel):
    id: str                            # "<type>:<key>", unique across node types
    label: str
    type: str                          # dimension name, e.g. "intervention", "lead_sponsor"
    weight: int                        # trials containing this entity
    is_query_match: bool = False       # matches a filter in the question (likely a hub)
    nct_ids: list[str]
    citations: list[Citation]


class Edge(BaseModel):
    source: str
    target: str
    weight: int                        # trials containing both entities
    nct_ids: list[str]
    citations: list[Citation]


class TableRow(BaseModel):
    nct_id: str
    url: str
    title: str
    status: str | None
    phase: str
    start_date: str | None
    lead_sponsor: str | None
    conditions: list[str]
    interventions: list[str]
    countries: list[str]
    series: str | None = None


class AggregateResult(BaseModel):
    kind: Literal["rows", "network", "table"]
    rows: list[Datum] = []
    nodes: list[Node] = []
    edges: list[Edge] = []
    table: list[TableRow] = []
    categories: list[str | int] = []   # x order for categorical charts
    series: list[str] = []             # series order (compare groups)
    notes: list[str] = []              # human-readable caveats for the response metadata
    stats: dict[str, Any] = {}         # numbers for the metadata (missing values, hidden values, ...)