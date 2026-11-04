"""Retrieval accuracy: does a filter match the right trials?

The app searches a drug only in InterventionName / OtherName / MeshTerm, and a condition
only in Condition / ConditionMeshTerm ("strict", app/ctgov/query_builder.py). The API's
broad search (query.intr / query.cond) also matches titles, descriptions and keywords.

For each drug and condition used in planner_cases.json:
  S = strict matches (what the app counts)
  E = broad matches the strict search leaves out
A random sample from S and from E is labelled by an LLM judge that reads the trial record
("does this trial give drug X / study condition X?").

  precision = share of S that is relevant
  recall    = relevant in S / (relevant in S + relevant in E), scaled to the population sizes

Recall is relative to the broad search: a trial that never mentions the term in any searched
field is invisible to both. Every judgement is written to a CSV so you can audit the judge.

Usage:
  python -m evals.eval_retrieval                 # all terms, 30 trials per side
  python -m evals.eval_retrieval --per-side 50 --terms drug:semaglutide condition:asthma
"""

import argparse
import asyncio
import csv
import json
import random
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings
from app.ctgov.client import CTGovClient, make_client
from app.ctgov.query_builder import BROAD_PARAMS, STRICT_FIELDS, ApiQuery, area_group, sanitize
from app.schemas.plan import Filters

from .common import RESULTS, ROOT, pct, save, wilson

JUDGE_MODEL = "gpt-4.1"
ID_CAP = 20000
RECORD_FIELDS = ",".join([
    "NCTId", "BriefTitle", "OfficialTitle", "StudyType", "BriefSummary", "Condition", "Keyword",
    "InterventionType", "InterventionName", "InterventionOtherName", "InterventionDescription",
    "ArmGroupLabel", "ArmGroupType", "ArmGroupDescription", "ArmGroupInterventionName",
])

QUESTION = {
    "drug": ("Does this trial give participants the drug or intervention \"{term}\" as part of the protocol, "
             "in any arm (experimental, comparator or background therapy)? Brand names, development codes, "
             "biosimilars and formulations of it count. For an observational study, answer yes only if the "
             "study is specifically about people receiving it. Answer no if it is only mentioned (e.g. as "
             "prior therapy, an exclusion criterion, or background in the summary)."),
    "condition": ("Does this trial study \"{term}\": are its participants people with this condition (or a "
                  "subtype of it), or is the condition a primary focus of the study? Answer no if it is only "
                  "mentioned in passing (e.g. an exclusion criterion, a risk factor, or a keyword about a "
                  "different focus)."),
}


class Verdict(BaseModel):
    verdict: Literal["yes", "no", "unclear"]
    reason: str


# ------------------------------------------------------------------ terms

def terms_from_cases() -> list[tuple[str, str]]:
    cases = json.loads((ROOT / "planner_cases.json").read_text())["cases"]
    found = []
    for c in cases:
        for e in c.get("alternatives") or [c.get("expect", {})]:
            for f in e.get("filters", []):
                for kind in ("drug", "condition"):
                    v = f.get(kind)
                    v = next((x for x in v if x), None) if isinstance(v, list) else v
                    if v and (kind, v) not in found:
                        found.append((kind, v))
    return found


# ------------------------------------------------------------------ API

def strict(kind: str, term: str) -> dict:
    return {"filter.advanced": area_group(STRICT_FIELDS[kind], term)}


def excluded(kind: str, term: str) -> dict:
    return {BROAD_PARAMS[kind]: sanitize(term), "filter.advanced": "NOT " + area_group(STRICT_FIELDS[kind], term)}


async def all_ids(client: CTGovClient, params: dict) -> tuple[int, list[str]]:
    r = await client.fetch(ApiQuery(label=None, filters=Filters(), params={**params, "fields": "NCTId"}), ID_CAP)
    return r.total_count, [s["protocolSection"]["identificationModule"]["nctId"] for s in r.studies]


async def records(client: CTGovClient, ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 20):
        data, _ = await client._get_page({"filter.ids": ",".join(ids[i:i + 20]), "fields": RECORD_FIELDS,
                                          "pageSize": "20"})
        out += data.get("studies", [])
    return out


def describe(study: dict) -> str:
    p = study.get("protocolSection", {})
    ident, arms = p.get("identificationModule", {}), p.get("armsInterventionsModule", {})
    short = lambda s, n: (s or "")[:n]
    lines = [f"Title: {ident.get('briefTitle')}", f"Official title: {ident.get('officialTitle')}",
             f"Study type: {p.get('designModule', {}).get('studyType')}",
             f"Conditions: {', '.join(p.get('conditionsModule', {}).get('conditions', []))}",
             f"Keywords: {', '.join(p.get('conditionsModule', {}).get('keywords', []))}",
             f"Summary: {short(p.get('descriptionModule', {}).get('briefSummary'), 700)}", "Interventions:"]
    for iv in arms.get("interventions", [])[:12]:
        lines.append(f"- [{iv.get('type')}] {iv.get('name')} (other names: {', '.join(iv.get('otherNames', []))}) "
                     f"{short(iv.get('description'), 200)}")
    lines.append("Arms:")
    for a in arms.get("armGroups", [])[:10]:
        lines.append(f"- [{a.get('type')}] {a.get('label')}: {short(a.get('description'), 200)} "
                     f"(interventions: {', '.join(a.get('interventionNames', []))})")
    return "\n".join(lines)


async def judge(llm: AsyncOpenAI, kind: str, term: str, study: dict, sem: asyncio.Semaphore) -> Verdict:
    async with sem:
        try:
            r = await llm.chat.completions.parse(
                model=JUDGE_MODEL, temperature=0, response_format=Verdict,
                messages=[{"role": "system", "content": "You label clinical trial registry records for an "
                                                        "evaluation. Be literal and use only the record."},
                          {"role": "user", "content": QUESTION[kind].format(term=term)
                           + "\n\n<record>\n" + describe(study) + "\n</record>"}])
            return r.choices[0].message.parsed or Verdict(verdict="unclear", reason="no output")
        except Exception as exc:
            return Verdict(verdict="unclear", reason=f"judge error: {exc}")


# ------------------------------------------------------------------ estimate

def estimate(n_s: int, labels_s: list[str], n_e: int, labels_e: list[str], rng: random.Random) -> dict:
    ys = [l == "yes" for l in labels_s if l != "unclear"]
    ye = [l == "yes" for l in labels_e if l != "unclear"]
    p_s = sum(ys) / len(ys) if ys else 0.0
    p_e = sum(ye) / len(ye) if ye else 0.0

    def recall(ps, pe):
        rel_s, rel_e = n_s * ps, n_e * pe
        return rel_s / (rel_s + rel_e) if rel_s + rel_e else 1.0

    boots = []
    for _ in range(2000):   # bootstrap both samples for a recall interval
        bs = [rng.choice(ys) for _ in ys] if ys else []
        be = [rng.choice(ye) for _ in ye] if ye else []
        boots.append(recall(sum(bs) / len(bs) if bs else 0.0, sum(be) / len(be) if be else 0.0))
    boots.sort()
    return {"precision": p_s, "precision_ci": wilson(sum(ys), len(ys)), "excluded_relevant_rate": p_e,
            "recall": recall(p_s, p_e), "recall_ci": (boots[50], boots[1949]),
            "est_relevant_found": n_s * p_s, "est_relevant_missed": n_e * p_e}


async def eval_term(kind, term, client, llm, per_side, sem, rng) -> dict:
    n_s, ids_s = await all_ids(client, strict(kind, term))
    n_e, ids_e = await all_ids(client, excluded(kind, term))
    sample_s = rng.sample(ids_s, min(per_side, len(ids_s)))
    sample_e = rng.sample(ids_e, min(per_side, len(ids_e)))
    rec_s, rec_e = await records(client, sample_s), await records(client, sample_e)
    v_s = await asyncio.gather(*(judge(llm, kind, term, s, sem) for s in rec_s))
    v_e = await asyncio.gather(*(judge(llm, kind, term, s, sem) for s in rec_e))
    rows = [{"kind": kind, "term": term, "side": side, "nct_id": s["protocolSection"]["identificationModule"]["nctId"],
             "title": s["protocolSection"]["identificationModule"].get("briefTitle"),
             "verdict": v.verdict, "reason": v.reason}
            for side, recs, vs in (("strict", rec_s, v_s), ("excluded", rec_e, v_e)) for s, v in zip(recs, vs)]
    est = estimate(n_s, [v.verdict for v in v_s], n_e, [v.verdict for v in v_e], rng)
    print(f"  {kind}:{term}: strict {n_s:,}, excluded {n_e:,}, precision {est['precision']:.0%}, "
          f"recall {est['recall']:.0%}", flush=True)
    return {"kind": kind, "term": term, "strict": n_s, "excluded": n_e, **est, "rows": rows}


def report(results: list[dict]) -> str:
    lines = ["## Retrieval (which trials a filter matches)", "",
             "| filter | app matches | left out by app | precision | recall vs broad search |",
             "|---|---|---|---|---|"]
    for r in results:
        lo, hi = r["precision_ci"]
        rlo, rhi = r["recall_ci"]
        lines.append(f"| {r['kind']}: {r['term']} | {r['strict']:,} | {r['excluded']:,} | "
                     f"{r['precision']:.0%} ({lo:.0%}-{hi:.0%}) | {r['recall']:.0%} ({rlo:.0%}-{rhi:.0%}) |")
    for kind in ("drug", "condition"):
        sub = [r for r in results if r["kind"] == kind]
        if not sub:
            continue
        rows = [x for r in sub for x in r["rows"]]
        strict_rows = [x for x in rows if x["side"] == "strict" and x["verdict"] != "unclear"]
        found = sum(r["est_relevant_found"] for r in sub)
        missed = sum(r["est_relevant_missed"] for r in sub)
        lines += ["", f"**{kind}s overall**: precision {pct(sum(x['verdict'] == 'yes' for x in strict_rows), len(strict_rows))} "
                      f"of sampled matches; estimated recall {found / (found + missed):.0%} "
                      f"(~{found:,.0f} relevant trials found, ~{missed:,.0f} missed, summed over terms)."]
    unclear = sum(x["verdict"] == "unclear" for r in results for x in r["rows"])
    lines.append(f"\nJudge answered 'unclear' {unclear} times (left out of the rates).")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-side", type=int, default=30)
    parser.add_argument("--terms", nargs="*", help="kind:term, e.g. drug:semaglutide")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    terms = [tuple(t.split(":", 1)) for t in args.terms] if args.terms else terms_from_cases()
    rng = random.Random(args.seed)
    llm = AsyncOpenAI(api_key=settings.openai_api_key, timeout=60)
    sem = asyncio.Semaphore(12)
    async with make_client() as client:
        results = [await eval_term(k, t, client, llm, args.per_side, sem, rng) for k, t in terms]
    text = report(results)
    path = save("retrieval", {"judge_model": JUDGE_MODEL, "per_side": args.per_side, "terms": results,
                              "summary": text})
    with (RESULTS / "retrieval_judgements.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["kind", "term", "side", "nct_id", "title", "verdict", "reason"])
        w.writeheader()
        for r in results:
            w.writerows(r["rows"])
    print(text)
    print(f"\nfull results: {path}\njudgements to audit: {RESULTS / 'retrieval_judgements.csv'}")


if __name__ == "__main__":
    asyncio.run(main())
