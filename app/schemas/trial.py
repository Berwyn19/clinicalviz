"""The normalized trial: what every step after the normalizer works with.

Each cleaned value sits next to the raw API value it came from, so citations
can always quote exactly what ClinicalTrials.gov said.
"""

from pydantic import BaseModel


class Intervention(BaseModel):
    name: str                     # raw, exactly as in the API (citation excerpt)
    type: str | None              # DRUG, BIOLOGICAL, DEVICE, PROCEDURE, ... (None if not fetched)
    other_names: list[str] = []
    key: str                      # grouping key, e.g. "pembrolizumab"
    display: str                  # cleaned name for labels, e.g. "Pembrolizumab"
    is_drug_like: bool            # drug/biological and not a placebo
    components: list[str] = []    # drug keys this entry stands for ("A and B" -> ["a", "b"])


class Site(BaseModel):
    country: str
    status: str | None = None     # site-level recruitment status (may differ from the trial's)


class Trial(BaseModel):
    nct_id: str
    title: str
    status: str | None
    study_type: str | None

    phases: list[str]             # raw, e.g. ["PHASE1", "PHASE2"]
    phase: str                    # one combined label, e.g. "PHASE1/PHASE2"; "NA" if none

    start_date: str | None        # raw, e.g. "2016-09" or "2016-09-16"
    start_year: int | None
    completion_date: str | None   # raw
    duration_months: float | None

    enrollment: int | None

    lead_sponsor: str | None      # raw
    lead_sponsor_key: str | None  # grouping key, legal suffixes removed
    lead_sponsor_display: str | None
    sponsor_class: str | None     # INDUSTRY, NIH, OTHER, ...

    conditions: list[str] = []
    interventions: list[Intervention] = []
    sites: list[Site] = []

    @property
    def drug_keys(self) -> set[str]:
        """Distinct drugs in this trial: combined entries split, placebos and non-drugs left out."""
        return {c for i in self.interventions if i.is_drug_like for c in i.components}

    @property
    def countries(self) -> set[str]:
        return {s.country for s in self.sites}