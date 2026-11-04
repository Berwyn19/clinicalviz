"""Raw ClinicalTrials.gov records -> flat, clean Trial objects.

Relevance is NOT decided here (the strict API query already did that).
This step only flattens the nested JSON and cleans messy values:
  - phase lists -> one label ("PHASE1/PHASE2"), missing -> "NA"
  - partial dates ("2016-09") -> start_year, duration in months
  - drug names -> grouping key (dose and parentheses removed, combos split, placebo flagged)
  - sponsor names -> grouping key (legal suffixes like LLC / Inc. removed)
  - locations -> distinct (country, site status) pairs

It never crashes on a bad record: the record is skipped and counted in the report.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from app.schemas.trial import Intervention, Site, Trial

log = logging.getLogger(__name__)

PHASE_ORDER = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"]
DRUG_LIKE_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}

DOSE_RE = re.compile(
    r"\s*\b\d+(?:\.\d+)?\s*(?:mg/kg|mg/m2|mg/m²|mg|mcg|µg|ug|g|ml|iu|units?)\b.*$", re.IGNORECASE)
PARENS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
COMBO_SPLIT_RE = re.compile(r"\s+(?:and|plus|with)\s+|\s*\+\s*", re.IGNORECASE)
SPONSOR_SUFFIX_RE = re.compile(
    r"[,\s]+(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|ag|"
    r"s\.?a|plc|n\.?v|b\.?v|s\.?p\.?a|pty)\.?$", re.IGNORECASE)
DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


# ------------------------------------------------------------------ report

@dataclass
class NormalizeReport:
    total: int = 0
    kept: int = 0
    skipped: Counter = field(default_factory=Counter)   # reason -> count
    missing: Counter = field(default_factory=Counter)   # field -> trials missing it

    def as_dict(self) -> dict:
        return {"total": self.total, "kept": self.kept,
                "skipped": dict(self.skipped), "missing": dict(self.missing)}


# ------------------------------------------------------------------ small helpers

def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def make_key(text: str) -> str:
    """Grouping key: lowercase, punctuation -> spaces. 'MK-3475' -> 'mk 3475'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def phase_label(phases: list[str]) -> str:
    known = sorted({p for p in phases if p in PHASE_ORDER}, key=PHASE_ORDER.index)
    return "/".join(known) if known else "NA"


def parse_date(raw: str | None) -> tuple[int, int | None, int | None] | None:
    m = DATE_RE.match(raw or "")
    if not m:
        return None
    y, mo, d = m.groups()
    return int(y), int(mo) if mo else None, int(d) if d else None


def months_between(start: str | None, end: str | None) -> float | None:
    a, b = parse_date(start), parse_date(end)
    if not a or not b:
        return None
    # Missing month -> mid-year, missing day -> mid-month.
    (y1, m1, d1), (y2, m2, d2) = ((y, m or 7, d or 15) for y, m, d in (a, b))
    months = (y2 - y1) * 12 + (m2 - m1) + (d2 - d1) / 30.44
    return round(months, 1) if months >= 0 else None


def clean_drug_name(name: str) -> str:
    """'Pembrolizumab 200 mg IV (MK-3475)' -> 'Pembrolizumab'"""
    cleaned = PARENS_RE.sub("", name)
    cleaned = DOSE_RE.sub("", cleaned)
    return " ".join(cleaned.split()).strip(" ,;:-")


def clean_sponsor(name: str) -> str:
    """'Merck Sharp & Dohme LLC' -> 'Merck Sharp & Dohme'"""
    cleaned = name.strip()
    while True:
        shorter = SPONSOR_SUFFIX_RE.sub("", cleaned).rstrip(" ,&").removesuffix(" and").strip()
        if shorter == cleaned or not shorter:
            return cleaned
        cleaned = shorter


# ------------------------------------------------------------------ pieces

def normalize_intervention(raw: dict) -> Intervention:
    name = raw.get("name") or ""
    itype = raw.get("type")
    display = clean_drug_name(name) or name
    key = make_key(display)
    is_placebo = "placebo" in key
    is_drug_like = (itype is None or itype in DRUG_LIKE_TYPES) and not is_placebo

    # "A and B" / "A + B": split into components, but only if every part looks like a name.
    parts = [make_key(clean_drug_name(p)) for p in COMBO_SPLIT_RE.split(display)]
    components = parts if len(parts) > 1 and all(len(p) >= 3 for p in parts) else [key]

    return Intervention(
        name=name, type=itype, other_names=raw.get("otherNames", []),
        key=key, display=display, is_drug_like=is_drug_like,
        components=[c for c in components if c],
    )


def normalize_sites(locations: list[dict]) -> list[Site]:
    seen, sites = set(), []
    for loc in locations:
        country = (loc.get("country") or "").strip()
        pair = (country, loc.get("status"))
        if country and pair not in seen:
            seen.add(pair)
            sites.append(Site(country=country, status=loc.get("status")))
    return sites


def normalize(study: dict) -> Trial:
    """One raw record -> Trial. Raises ValueError if the record has no NCT ID."""
    p = study.get("protocolSection")
    nct_id = _get(p, "identificationModule", "nctId")
    if not nct_id:
        raise ValueError("record has no NCT ID")

    phases = _get(p, "designModule", "phases", default=[]) or []
    start = _get(p, "statusModule", "startDateStruct", "date")
    completion = _get(p, "statusModule", "completionDateStruct", "date")
    start_parsed = parse_date(start)
    sponsor = _get(p, "sponsorCollaboratorsModule", "leadSponsor", "name")
    sponsor_display = clean_sponsor(sponsor) if sponsor else None
    enrollment = _get(p, "designModule", "enrollmentInfo", "count")

    return Trial(
        nct_id=nct_id,
        title=_get(p, "identificationModule", "briefTitle", default=""),
        status=_get(p, "statusModule", "overallStatus"),
        study_type=_get(p, "designModule", "studyType"),
        phases=phases,
        phase=phase_label(phases),
        start_date=start,
        start_year=start_parsed[0] if start_parsed else None,
        completion_date=completion,
        duration_months=months_between(start, completion),
        enrollment=enrollment if isinstance(enrollment, int) else None,
        lead_sponsor=sponsor,
        lead_sponsor_key=make_key(sponsor_display) if sponsor_display else None,
        lead_sponsor_display=sponsor_display,
        sponsor_class=_get(p, "sponsorCollaboratorsModule", "leadSponsor", "class"),
        conditions=_get(p, "conditionsModule", "conditions", default=[]) or [],
        interventions=[normalize_intervention(i) for i in
                       _get(p, "armsInterventionsModule", "interventions", default=[]) or []],
        sites=normalize_sites(_get(p, "contactsLocationsModule", "locations", default=[]) or []),
    )


def normalize_all(studies: list[dict], needed: set[str] | None = None) -> tuple[list[Trial], NormalizeReport]:
    """Normalize every record. `needed` = Trial fields the plan uses; missing ones are counted.

    Duplicate NCT IDs (possible across API pages) are kept once.
    """
    report = NormalizeReport(total=len(studies))
    trials, seen = [], set()
    for study in studies:
        try:
            trial = normalize(study)
        except Exception as exc:  # bad record: skip it, never fail the request
            report.skipped[f"invalid record: {type(exc).__name__}"] += 1
            log.warning("skipping record: %s", exc)
            continue
        if trial.nct_id in seen:
            report.skipped["duplicate NCT ID"] += 1
            continue
        seen.add(trial.nct_id)
        for name in needed or ():
            value = getattr(trial, name, None)
            if value in (None, [], "NA"):
                report.missing[name] += 1
        trials.append(trial)
    report.kept = len(trials)
    return trials, report