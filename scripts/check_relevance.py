"""Why does the API return trials where the drug is NOT in a structured field?

query.intr searches 12 fields (ClinicalTrials.gov "InterventionSearch" area):
  InterventionName, InterventionOtherName, InterventionMeshTerm   <- STRONG evidence
  BriefTitle, OfficialTitle, ArmGroupLabel, ArmGroupDescription,
  InterventionDescription, Keyword, InterventionAncestorTerm       <- WEAK evidence
  (+ InterventionType, ArmGroupType: enums, irrelevant for drug names)

For each drug this script:
  1. labels every trial STRONG or WEAK
  2. for WEAK trials, finds which weak field mentions the drug (or a synonym)
  3. writes a stratified sample with the matching text snippet to a CSV for review

Usage:
  python -m scripts.check_relevance
  python -m scripts.check_relevance metformin --per-bucket 10
"""

import argparse
import asyncio
import csv
import json
import random
import re
from collections import Counter
from pathlib import Path

from app.ctgov.client import make_client
from app.ctgov.query_builder import ApiQuery
from app.schemas.plan import Filters

FIELDS = [
    "NCTId", "BriefTitle", "OfficialTitle", "Keyword", "DesignPrimaryPurpose",
    "InterventionName", "InterventionOtherName", "InterventionType", "InterventionDescription",
    "InterventionMeshTerm", "InterventionAncestorTerm",
    "ArmGroupLabel", "ArmGroupType", "ArmGroupDescription", "ArmGroupInterventionName",
]
OUT_DIR = Path("evals/results")

# Order = priority when a weak trial mentions the drug in several places.
WEAK_FIELDS = ["title", "arm_label", "intervention_description", "arm_description",
               "keyword", "ancestor_term"]


# ------------------------------------------------------------------ text helpers

def get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def norm(text: str) -> str:
    """'ALKS 4230 + Pembrolizumab' -> ' alks 4230 pembrolizumab ' (padded for whole-word search)."""
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def contains(text: str, term: str) -> bool:
    return norm(term) in norm(text)


def snippet(text: str, term: str, width: int = 90) -> str:
    """The original text around the first whole-word match of term."""
    words = re.findall(r"[a-z0-9]+", term.lower())
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(map(re.escape, words)) + r"(?![a-z0-9])"
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return ""
    start, end = max(0, m.start() - width), min(len(text), m.end() + width)
    return ("..." if start else "") + " ".join(text[start:end].split()) + ("..." if end < len(text) else "")


# ------------------------------------------------------------------ record helpers

def weak_field_texts(study: dict) -> dict[str, list[str]]:
    proto = study.get("protocolSection", {})
    arms = get(proto, "armsInterventionsModule", "armGroups", default=[])
    ivs = get(proto, "armsInterventionsModule", "interventions", default=[])
    return {
        "title": [get(proto, "identificationModule", "briefTitle", default=""),
                  get(proto, "identificationModule", "officialTitle", default="")],
        "arm_label": [a.get("label", "") for a in arms],
        "intervention_description": [iv.get("description", "") for iv in ivs],
        "arm_description": [a.get("description", "") for a in arms],
        "keyword": get(proto, "conditionsModule", "keywords", default=[]),
        "ancestor_term": [t.get("term", "") for t in
                          get(study, "derivedSection", "interventionBrowseModule", "ancestors", default=[])],
    }


def strong_check(study: dict, drug: str) -> tuple[set[str], list[str]]:
    proto = study.get("protocolSection", {})
    sources, matched_names = set(), []
    for iv in get(proto, "armsInterventionsModule", "interventions", default=[]):
        name = iv.get("name", "")
        if contains(name, drug):
            sources.add("intervention_name")
            matched_names.append(name)
        elif any(contains(o, drug) for o in iv.get("otherNames", [])):
            sources.add("other_names")
            matched_names.append(name)
    mesh = [m.get("term", "") for m in get(study, "derivedSection", "interventionBrowseModule", "meshes", default=[])]
    if any(contains(t, drug) for t in mesh):
        sources.add("mesh_term")
    return sources, matched_names


def drug_role(study: dict, matched_names: list[str]) -> str:
    arms = get(study, "protocolSection", "armsInterventionsModule", "armGroups", default=[])
    types = {a.get("type", "UNKNOWN") for a in arms
             if any(contains(x, n) for x in a.get("interventionNames", []) for n in matched_names)}
    if not matched_names:
        return "mesh_only"
    if "EXPERIMENTAL" in types:
        return "experimental"
    if "ACTIVE_COMPARATOR" in types:
        return "active_comparator"
    if types & {"PLACEBO_COMPARATOR", "SHAM_COMPARATOR"}:
        return "placebo_or_sham"
    if types:
        return "other_arm:" + ",".join(sorted(types))
    return "not_linked_to_any_arm" if arms else "no_arms_listed"


def collect_synonyms(studies: list[dict], drug: str) -> list[str]:
    """otherNames of interventions named exactly <drug>, e.g. Keytruda, MK-3475."""
    target, found = norm(drug), Counter()
    for s in studies:
        for iv in get(s, "protocolSection", "armsInterventionsModule", "interventions", default=[]):
            if norm(iv.get("name", "")) == target:
                found.update(o for o in iv.get("otherNames", []) if len(o) >= 4)
    # keep names seen in at least 3 trials, to avoid one-off noise
    return [name for name, n in found.most_common() if n >= 3 and norm(name) != target]


def locate_weak(study: dict, terms: list[str]) -> tuple[str, str, str]:
    """(field, term, snippet) of the first weak field that mentions any term."""
    texts = weak_field_texts(study)
    for field in WEAK_FIELDS:
        for text in texts[field]:
            for term in terms:
                if text and contains(text, term):
                    return field, term, snippet(text, term)
    return "not_found", "", ""


# ------------------------------------------------------------------ report

def pct(n: int, total: int) -> str:
    return f"{n:>5} ({100 * n / total:5.1f}%)" if total else f"{n:>5}"


def print_counter(title: str, counter: Counter, total: int):
    print(f"  {title}")
    for key, n in counter.most_common():
        print(f"    {key:<40} {pct(n, total)}")


async def analyze(drug: str, per_bucket: int, seed: int):
    query = ApiQuery(label=drug, filters=Filters(drug=drug),
                     params={"query.intr": drug, "fields": ",".join(FIELDS)})
    async with make_client() as client:
        result = await client.fetch(query, max_trials=10000)
    studies = result.studies
    total = len(studies)

    synonyms = collect_synonyms(studies, drug)
    terms = [drug, *synonyms]

    strong, weak = [], []
    for s in studies:
        sources, names = strong_check(s, drug)
        nct = get(s, "protocolSection", "identificationModule", "nctId")
        purpose = get(s, "protocolSection", "designModule", "designInfo", "primaryPurpose", default="MISSING")
        if sources:
            strong.append({"nct_id": nct, "sources": sources, "role": drug_role(s, names), "purpose": purpose})
        else:
            field, term, snip = locate_weak(s, terms)
            weak.append({
                "nct_id": nct, "purpose": purpose, "field": field, "term": term, "snippet": snip,
                "title": get(s, "protocolSection", "identificationModule", "briefTitle", default=""),
                "interventions": [iv.get("name", "") for iv in
                                  get(s, "protocolSection", "armsInterventionsModule", "interventions", default=[])],
            })

    print(f"\n=== {drug} === API matched {result.total_count}, fetched {total}")
    print(f"  synonyms used for weak search: {', '.join(synonyms[:8]) or '(none)'}")
    print(f"  strong matches {pct(len(strong), total)}")
    print(f"  weak matches   {pct(len(weak), total)}")
    print_counter("strong: role of the drug", Counter(r["role"] for r in strong), len(strong))
    print_counter("weak: where the drug is mentioned (first match, by priority)",
                  Counter(r["field"] for r in weak), len(weak))
    print_counter("weak: matched via drug name or synonym",
                  Counter("drug name" if r["term"] == drug else ("synonym" if r["term"] else "-") for r in weak),
                  len(weak))
    print_counter("weak: primary purpose", Counter(r["purpose"] for r in weak), len(weak))

    # Stratified sample: up to N trials from each "where mentioned" bucket.
    rng = random.Random(seed)
    sample = []
    for field in [*WEAK_FIELDS, "not_found"]:
        bucket = [r for r in weak if r["field"] == field]
        sample += rng.sample(bucket, min(per_bucket, len(bucket)))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"relevance_review_{drug.lower()}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["is_relevant (y/n)", "nct_id", "matched_in", "matched_term", "snippet",
                    "title", "interventions", "url"])
        for r in sample:
            w.writerow(["", r["nct_id"], r["field"], r["term"], r["snippet"], r["title"],
                        " | ".join(r["interventions"]), f"https://clinicaltrials.gov/study/{r['nct_id']}"])
    print(f"\n  {len(sample)} weak samples (up to {per_bucket} per bucket) written to {csv_path}")

    for field in WEAK_FIELDS:
        example = next((r for r in sample if r["field"] == field), None)
        if example:
            print(f"    [{field}] {example['nct_id']}: {example['snippet'][:160]}")

    summary = {
        "drug": drug, "api_total": result.total_count, "fetched": total, "synonyms": synonyms,
        "strong": len(strong), "weak": len(weak),
        "strong_roles": Counter(r["role"] for r in strong),
        "weak_fields": Counter(r["field"] for r in weak),
        "weak_purposes": Counter(r["purpose"] for r in weak),
    }
    (OUT_DIR / f"relevance_summary_{drug.lower()}.json").write_text(json.dumps(summary, indent=2))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("drugs", nargs="*", default=["pembrolizumab", "nivolumab"])
    parser.add_argument("--per-bucket", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    for drug in args.drugs:
        await analyze(drug, args.per_bucket, args.seed)


asyncio.run(main())