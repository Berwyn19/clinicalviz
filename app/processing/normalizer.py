"""Raw ClinicalTrials.gov records -> flat, clean Trial objects.

Relevance is NOT decided here (the strict API query already did that).
This step only flattens the nested JSON and cleans messy values:
  - phase lists -> one label ("PHASE1/PHASE2"), missing -> "NA"
  - partial dates ("2016-09") -> start_year, duration in months
  - drug names -> grouping key (dose and parentheses removed, combos split, placebos removed
    per component, synonyms merged using the records' own otherNames and MeSH terms)
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

# --- drug names (general rules only; synonyms are learned from the data, see build_drug_aliases) ---
DOSE_RE = re.compile(
    r"\s*\b\d+(?:\.\d+)?\s*(?:mg/kg|mg/m2|mg/m²|mg|mcg|µg|ug|g|ml|iu|units?)\b.*$", re.IGNORECASE)
PARENS_RE = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
COMBO_SPLIT_RE = re.compile(r"\s+(?:and|plus|with)\s+|\s*\+\s*", re.IGNORECASE)
PLACEBO_RE = re.compile(r"\bplacebo\b", re.IGNORECASE)

# --- synonym merging (see build_drug_aliases) ---
ALIAS_MIN_SUPPORT = 2      # entries that must link the two names
ALIAS_MIN_SHARE = 0.8      # share of a name's links that must point to the same drug

# --- sponsors ---
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
    drug_aliases: dict[str, str] = field(default_factory=dict)  # merged name key -> canonical key

    def as_dict(self) -> dict:
        return {"total": self.total, "kept": self.kept, "skipped": dict(self.skipped),
                "missing": dict(self.missing), "drug_aliases_merged": len(self.drug_aliases)}


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


def split_drug_names(name: str) -> list[str]:
    """One intervention entry -> the cleaned drug names in it.

    Splits before cleaning, so 'A 200 mg + B 20 mg' keeps B. Splits only if every
    part still looks like a name, so 'Drug A and B' stays one name.
    """
    parts = [clean_drug_name(p) for p in COMBO_SPLIT_RE.split(PARENS_RE.sub("", name))]
    if len(parts) > 1 and all(len(make_key(p)) >= 3 for p in parts):
        return list(dict.fromkeys(parts))
    return [clean_drug_name(name) or name]


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
    other_names = [o for o in raw.get("otherNames") or [] if isinstance(o, str)]
    in_parens = [m.group(1) for m in PARENS_RE.finditer(name)]

    # Placebos are removed per component, so 'Pembrolizumab + Placebo' still counts pembrolizumab.
    real = [n for n in split_drug_names(name) if not PLACEBO_RE.search(n)]
    displays = {}
    for n in real:
        if make_key(n):
            displays.setdefault(make_key(n), n)
    components = list(displays)

    display = " + ".join(displays.values()) if components else (clean_drug_name(name) or name)
    aliases = {make_key(clean_drug_name(o)) for o in [*other_names, *in_parens]}

    return Intervention(
        name=name, type=itype, other_names=other_names,
        key=components[0] if len(components) == 1 else make_key(display), display=display,
        is_drug_like=(itype is None or itype in DRUG_LIKE_TYPES) and bool(components),
        components=components, component_displays=list(displays.values()),
        alias_keys=sorted(a for a in aliases if len(a) >= 3 and a not in components),
    )


# ------------------------------------------------------------------ drug synonyms

def _mesh_links(trial: Trial) -> list[tuple[str, str]]:
    """Link a drug entry to a MeSH term by elimination, within one trial.

    MeSH terms are the registry's standard drug names, but they belong to the trial,
    not to one intervention. If exactly one drug entry matches no MeSH term and exactly
    one MeSH term matches no entry, they name the same drug ('Keytruda' <-> 'pembrolizumab').
    """
    mesh = {make_key(m) for m in trial.drug_mesh_terms} - {""}
    entries = [iv for iv in trial.interventions if iv.is_drug_like and len(iv.components) == 1]
    unmatched = [iv for iv in entries if not ({iv.components[0], *iv.alias_keys} & mesh)]
    used = {k for iv in entries for k in (iv.components[0], *iv.alias_keys)}
    free_mesh = mesh - used
    if len(unmatched) == 1 and len(free_mesh) == 1:
        return [(unmatched[0].components[0], next(iter(free_mesh)))]
    return []


def build_drug_aliases(trials: list[Trial]) -> dict[str, str]:
    """Learn which drug names are synonyms from the records themselves.

    Evidence that two names are the same drug:
      - a single-drug entry lists the other name in otherNames or in parentheses
        ('Pembrolizumab', otherNames ['Keytruda', 'MK-3475']), or
      - a MeSH term is linked to the entry by elimination (see _mesh_links).
    A name is merged into another only if
      - at least ALIAS_MIN_SUPPORT pieces of evidence link them,
      - at least ALIAS_MIN_SHARE of all its links point to that same drug
        (so 'Anti-PD-1', listed for both pembrolizumab and nivolumab, is never merged), and
      - the target is used as a drug name in more entries (the common name wins).
    Returns {name key -> canonical key}; names that stay themselves are left out.
    """
    used = Counter()                      # key -> entries that list it as a drug
    links: dict[str, Counter] = {}        # key -> linked key -> pieces of evidence

    def link(a: str, b: str) -> None:
        links.setdefault(a, Counter())[b] += 1
        links.setdefault(b, Counter())[a] += 1

    for t in trials:
        for iv in t.interventions:
            if not iv.is_drug_like:
                continue
            used.update(iv.components)
            if len(iv.components) == 1:   # in a combo entry we can't tell which drug an alias belongs to
                for alias in iv.alias_keys:
                    link(iv.components[0], alias)
        for a, b in _mesh_links(t):
            link(a, b)

    canonical: dict[str, str] = {}

    def resolve(k: str) -> str:
        while k in canonical:      # chains always point to a more-used name, so this ends
            k = canonical[k]
        return k

    # Repeat until nothing changes: merging 'glucophage' into 'metformin' can make
    # 'metformin hydrochloride' (linked to both) clear the share threshold on the next pass.
    order = sorted(links, key=lambda k: (-used[k], k))
    changed = True
    while changed:
        changed = False
        for key in order:
            if key in canonical:
                continue
            partners = Counter()
            for other, n in links[key].items():
                partners[resolve(other)] += n
            partners.pop(key, None)
            if not partners:
                continue
            target, support = partners.most_common(1)[0]
            if (support >= ALIAS_MIN_SUPPORT and support / sum(partners.values()) >= ALIAS_MIN_SHARE
                    and used[target] > used[key]):
                canonical[key] = target
                changed = True
    return {k: resolve(k) for k in canonical}


def apply_drug_aliases(trials: list[Trial], aliases: dict[str, str]) -> None:
    """Rewrite drug components to their canonical names, in place.

    The display label of a canonical drug is its most common cleaned name ('Pembrolizumab').
    """
    if not aliases:
        return
    names: dict[str, Counter] = {}
    for t in trials:
        for iv in t.interventions:
            for comp, disp in zip(iv.components, iv.component_displays):
                names.setdefault(comp, Counter())[disp] += 1
    best = {k: c.most_common(1)[0][0] for k, c in names.items()}

    for t in trials:
        for iv in t.interventions:
            if not any(c in aliases for c in iv.components):
                continue
            comps = list(dict.fromkeys(aliases.get(c, c) for c in iv.components))
            iv.components = comps
            iv.component_displays = [best.get(c, c) for c in comps]
            if len(comps) == 1:
                iv.key, iv.display = comps[0], iv.component_displays[0]


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
        drug_mesh_terms=[m.get("term") for m in
                         _get(study, "derivedSection", "interventionBrowseModule", "meshes", default=[]) or []
                         if isinstance(m, dict) and isinstance(m.get("term"), str)],
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
    report.drug_aliases = build_drug_aliases(trials)
    apply_drug_aliases(trials, report.drug_aliases)
    return trials, report