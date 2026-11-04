# Accuracy

Measured 2026-11-04. Every number below can be reproduced with the scripts in `evals/`
(results land in `evals/results/`). The test questions are held out: they use drugs and
conditions not used during development, and nothing was tuned to them.

A wrong answer can come from three places, so each is measured separately:

| Stage | Question | Script |
|---|---|---|
| Planner | Was the question turned into the right plan? | `python -m evals.eval_planner` |
| Retrieval | Does a filter match the right trials? | `python -m evals.eval_retrieval` |
| Chart numbers | Is each bar the number the registry gives? | `python -m evals.eval_counts` |

## Summary

| What | Result |
|---|---|
| Planner: runs that produced the expected plan | **98%** (117/120; 40 questions x 3 runs) |
| Planner: runs with wrong filters | **0** of 99 |
| Bars from coded registry fields (phase, status, year, enrollment, sponsor class, study type, country) | **100% exact** (311/311 bars covering 40,000+ trial counts) |
| Lead sponsor bars | 90% exact (18/20) |
| Drug bars vs. filtering on that drug name | 0/25 exact, **26% low** overall; app almost never adds a wrong trial |
| Condition bars vs. filtering on that condition | 0/15 exact, **50% low** overall; app never adds a wrong trial |
| Sample-based bars (download cut at 10,000) | 17% low (1 chart; already labelled as a sample) |
| Drug filter: precision / recall | **99%** / **75%** |
| Condition filter: precision / recall | **93%** / **91%** |

In one line: the planner and every count of a coded field are close to exact. The weak
spots are drug and condition *names*, both when filtering and when they are the bars.

## 1. Planner

`evals/planner_cases.json`: 40 questions covering every chart type, compare groups, year
ranges, hard filters from the form, a conflicting form field, and 6 questions that must be
refused. Each ran 3 times through the real guardrail + planner (gpt-4.1-mini + gpt-4.1).

| Field | Accuracy |
|---|---|
| intent | 100% (120/120) |
| filters (all series, extra filters count as wrong) | 100% (99/99) |
| group_by | 100% (75/75) |
| chart | 97% (96/99) |
| sort, limit, numeric fields, network ends | 100% |

- The only failure (3/3 runs): "Compare how many phase 3 trials Pfizer, Novartis and Roche
  started each year since 2015" became a grouped bar chart by year instead of a line chart.
  Same numbers, worse chart.
- 29/40 questions produced the identical plan on all 3 runs. The differences were in
  unscored fields (labels, sort, limit), not filters.
- All 6 unanswerable questions were refused (efficacy, personal advice, off-topic, prompt
  injection, adverse events, participant age).

## 2. Chart numbers

The expected plan for each count chart (27 charts, 382 bars or points) was run through the
real pipeline. Then every bar was recounted with its own API search: the series query AND
the bar's value. The recount queries are written independently of `app/aggregation`.

| Bars | Count | Exactly equal | Error |
|---|---|---|---|
| Coded fields, full download | 290 | 100% | 0 |
| Coded fields, exact API counts (download truncated) | 7 | 100% | 0 |
| Coded fields, counted from a sample | 11 | 9% | 17% low |
| Free-text fields (drug, condition, sponsor, country) | 74 | 43% | 21% low |

By dimension (full downloads):

| Dimension | Bars | Exact | Overall error |
|---|---|---|---|
| phase | 60 | 100% | 0% |
| start_year | 174 | 100% | 0% |
| status | 39 | 100% | 0% |
| country | 14 | 100% | 0% |
| enrollment bins | 9 | 100% | 0% |
| sponsor_class, study_type | 8 | 100% | 0% |
| lead_sponsor | 20 | 90% | 8% low |
| intervention (drug) | 25 | 0% | 26% low |
| condition | 15 | 0% | 50% low |

For the free-text bars the trial lists were compared too. Across 40 drug and condition bars,
21 trials were in the app's bar but not in the recount, and 2,450 were in the recount but not
in the bar. So these bars are reliable lower bounds, and the gap is trials they miss.
The causes:

- **Drug inside a combination name.** "EPOCH-R", "Oblimersen-rituximab-fludarabine" are not
  counted as rituximab. Even in a chart of rituximab trials, the Rituximab bar is 1,954 of 2,262.
- **Salt forms split.** "Vincristine" (190) and "Vincristine Sulfate" (99) are separate bars;
  filtering on vincristine gives 365.
- **Conditions are exact strings.** The "Stroke" bar (57) excludes "Ischemic Stroke" and
  "Cerebrovascular Accident"; filtering on stroke gives 173.
- **Sponsor name variants.** "Novartis" and "Novartis Pharmaceuticals" are separate bars.

The one sample-based chart (depression trials by intervention type: 12,095 matched, 10,000
downloaded) undercounts by 17%, matching the 17% of trials not downloaded. The chart already
says it is a sample.

## 3. Retrieval

For each of the 32 drugs and conditions in the test questions: S = trials the app matches
(structured fields only), E = trials the API's broad search finds but the app leaves out.
30 random trials from each side were labelled by an LLM judge (gpt-4.1, temperature 0)
reading the record. Precision = relevant share of S; recall = relevant in S / relevant in S
and E, scaled to population sizes. Recall is relative to the broad search, so trials that
never mention the term anywhere are not counted. All ~1,900 judgements are in
`evals/results/retrieval_judgements.csv`.

| | Precision | Recall |
|---|---|---|
| Drugs (12) | 99% (356/359) | 75% |
| Conditions (20) | 93% (550/592) | 91% |

Weakest terms:

| Filter | Recall | Cause |
|---|---|---|
| condition: parkinson's disease | 43% | **Bug.** The apostrophe stops the API from expanding to MeSH synonyms: "parkinson's disease" matches 1,703 trials, "parkinson disease" 4,913. Affects any possessive condition name. |
| drug: car-t | 52% | Not a drug name: trials name the product (e.g. tisagenlecleucel), so the term itself rarely appears. |
| drug: aspirin | 68% | Aspirin is often background therapy described only in arm text. |
| drug: adalimumab | 74% | Biosimilar codes and generic arm names. |
| condition: sepsis, copd | 80-81% | Often written as "septic shock" or spelled out in titles only. |

Condition precision is a conservative estimate. Most "no" verdicts were trials that list the
condition but enrol healthy volunteers (e.g. a bioavailability study listing heart failure),
which a user may or may not want counted.

## Caveats

- The judge is an LLM. I read its "no" verdicts for the app's matches and found them
  reasonable but sometimes strict. Audit the CSV before relying on single-term numbers.
- 30 trials per side gives wide intervals for a single term (shown in the results JSON);
  the pooled drug and condition numbers are tighter.
- The planner set is 40 questions; its 95% interval for "runs correct" is 93-99%.
- Counts change daily as the registry updates; rerun the scripts for current numbers.
