# clinicalviz

Ask a question about ClinicalTrials.gov, get a chart. Every number is counted by code from registry records (never by the LLM) and links back to the trials and fields it came from.

## Demo

[`demo.mp4`](demo.mp4) (2 min): asking questions in the bundled UI, each chart type, and clicking a bar to see the trials and citations behind it.

## Run

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env       # .env wins over a key exported in the shell
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (UI) or http://127.0.0.1:8000/docs (OpenAPI).

Tests: `pytest`. Accuracy evals (live API and LLM, about 10-15 min each): `python -m evals.eval_planner`, `python -m evals.eval_counts`, `python -m evals.eval_retrieval`.

Optional settings (env or `.env`): `PLANNER_MODEL` (gpt-4.1), `GUARDRAIL_MODEL` (gpt-4.1-mini), `CTGOV_CACHE_ENABLED` (true), `CTGOV_REQUESTS_PER_MINUTE` (50), `CTGOV_EXACT_COUNT_MAX_REQUESTS` (150). See `app/config.py`.

## API

### `POST /api/query`

Request (`app/schemas/request.py`). Only `query` is required; the other fields are hard filters that override the text.

| field | type |
|---|---|
| `query` | string, 3-500 chars |
| `drug_name`, `condition`, `sponsor`, `country` | string |
| `trial_phase` | list of `EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4, NA` |
| `status` | list of API statuses, e.g. `RECRUITING`, `COMPLETED` |
| `start_year`, `end_year` | int |
| `max_trials` | int, default 10000 (cap on downloaded trials) |

Response (`app/visualization/spec.py`). Null fields are omitted.

```
{ "status": "ok" | "unsupported",
  "visualization": { "type", "title", "encoding", "data" },   // absent when unsupported
  "meta": { ... } }
```

`visualization.type`: `bar_chart`, `grouped_bar_chart`, `time_series`, `histogram`, `scatter_plot`, `network_graph`, `table`.

`visualization.encoding` maps record fields to channels. Each channel is `{field, type, title}` plus, where relevant, `sort` (category order), `domain` (series order), `unit`, `scale` (`linear`/`log`), `time_unit`.

| type | channels | one `data` record |
|---|---|---|
| bar / grouped bar | `x` (category), `y` = `trial_count`, `color` = `series` (grouped) | `{"<dimension>": "Phase 2", "trial_count": 78, "series"?, "nct_ids", "nct_ids_complete", "citations"}` |
| time_series | `x` = `start_year` (`time_unit: year`), `y`, `color`?, `partial` = `is_partial` (current year, incomplete) | `{"start_year": 2020, "trial_count": 41, "is_partial"?, ...}` |
| histogram | `x` = `bin` with `bin_start`/`bin_end` (end exclusive, null = open), `y` | `{"bin": "11-50", "bin_start": 11, "bin_end": 51, "trial_count": 9, ...}` |
| scatter_plot | `x`, `y` (numeric fields, with `unit` and `scale`), `color`?, `tooltip` | `{"nct_id", "title", "enrollment": 120, "duration_months": 30, "citations"}` (one per trial) |
| network_graph | `nodes`: `id`, `label`, `size` = `weight`, `color` = `type`, `highlight` = `is_query_match`; `edges`: `source`, `target`, `width` = `weight` | `data = {"nodes": [...], "edges": [...]}`; weight = trials containing the entity / both entities |
| table | `columns` | one trial: `nct_id`, `url`, `title`, `status`, `phase`, `start_date`, `lead_sponsor`, `conditions`, `interventions`, `countries` |

Citations: every datum has `nct_ids` (first 20; `trial_count` is the true total, `nct_ids_complete` says whether the list is cut) and up to 3 `citations` `{nct_id, field, excerpt}`, where `excerpt` is the exact API value. When all citations of a chart come from one API field, `field` is given once as `encoding.citations.field` (network: `encoding.nodes.citations`, `encoding.edges.citations`).

`meta`:

| field | meaning |
|---|---|
| `query`, `plan` | the question and how it was read (validated LLM plan, `app/schemas/plan.py`) |
| `filters`, `series_filters` | filters on all series; per compare group |
| `assumptions` | how ambiguous parts were interpreted |
| `notes` | caveats: missing values, hidden values, overlap between groups, sampling |
| `count_source` | `downloaded` (every match counted), `api_counts` (download capped, exact per-value API counts), `sample` (download capped, numbers describe the sample) |
| `sort`, `limit`, `time_granularity` | ordering and grouping choices |
| `stats` | `matched`, `downloaded`, missing and hidden counts, etc. |
| `sources[]` | per series: API `request_url`, `matched`, `downloaded`, `broad_matched` |
| `reason` | why the question is unsupported |
| `timings` | seconds for planning and data |

Errors: 422 invalid input or plan, 502 LLM or ClinicalTrials.gov failure.

### `POST /api/plan`

`{"plan": <QueryPlan>, "max_trials": 10000}`. Runs a plan without the LLM (the UI's "edit the plan and re-run"). Same response.

## Example runs

Full JSON outputs from the running system are in `examples/`:

| file | query | result |
|---|---|---|
| `1_time_series.json` | "How has the number of trials for this drug changed over time?" + `drug_name: Pembrolizumab` | time series by start year |
| `2_grouped_bar.json` | "Compare the phases of semaglutide and tirzepatide trials" | grouped bar, 650 vs 252 trials |
| `3_bar_countries.json` | "Which 10 countries have the most recruiting psoriasis trials?" | bar: United States 46, China 30, France 16, ... |
| `4_network.json` | "Show a network of drugs that are combined with rituximab" | drug-drug network, 30 nodes, 81 links (each drug's 3 strongest) |
| `5_unsupported.json` | "Is adalimumab better than etanercept for rheumatoid arthritis?" | `unsupported`: needs medical judgment |

Example 2, shortened (one record, one source):

```json
{
  "status": "ok",
  "visualization": {
    "type": "grouped_bar_chart",
    "title": "Semaglutide vs Tirzepatide trials, by phase",
    "encoding": {
      "x": {"field": "phase", "type": "nominal", "title": "Phase",
            "sort": ["Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "N/A"]},
      "y": {"field": "trial_count", "type": "quantitative", "title": "Trials"},
      "color": {"field": "series", "type": "nominal", "title": "Group", "domain": ["Semaglutide", "Tirzepatide"]},
      "citations": {"field": "protocolSection.designModule.phases"}
    },
    "data": [
      {"phase": "Early Phase 1", "trial_count": 5, "series": "Semaglutide",
       "nct_ids": ["NCT07627191", "NCT07727473", "..."], "nct_ids_complete": true,
       "citations": [{"nct_id": "NCT07627191", "excerpt": "EARLY_PHASE1"}]}
    ]
  },
  "meta": {
    "source": "clinicaltrials.gov",
    "query": "Compare the phases of semaglutide and tirzepatide trials",
    "filters": {},
    "series_filters": {"Semaglutide": {"drug": "semaglutide"}, "Tirzepatide": {"drug": "tirzepatide"}},
    "notes": ["40 trials belong to more than one compared group and are counted in each."],
    "count_source": "downloaded",
    "stats": {"trials": 902, "matched": 902, "downloaded": 902, "missing_value": 0, "values_not_shown": 0},
    "sources": [{"label": "Semaglutide", "matched": 650, "downloaded": 650, "broad_matched": 796,
                 "request_url": "https://clinicaltrials.gov/api/v2/studies?filter.advanced=..."}],
    "timings": {"plan_s": 3.9, "data_s": 0.1}
  }
}
```

## Design

```
question -> guardrail + planner (LLM) -> QueryPlan -> API queries -> fetch -> normalize -> aggregate -> visualization spec
```

| path | role |
|---|---|
| `app/main.py` | FastAPI endpoints |
| `app/schemas/` | request, plan, and shared enums |
| `app/planner/` | guardrail, planner, prompts, plan validation |
| `app/ctgov/` | query builder, API client (retry, rate limit, cache), exact counts |
| `app/processing/` | normalization of raw API records (drug names, synonyms, dates) |
| `app/aggregation/` | chart-agnostic counting with citations |
| `app/visualization/` | visualization spec builder |
| `app/pipeline.py` | plan -> fetch -> normalize -> aggregate |
| `app/static/` | optional UI |
| `evals/`, `docs/accuracy.md` | accuracy evaluation |

- **The LLM only writes a plan, never numbers.** The plan is a strict schema (structured outputs, enums only), checked by rules; errors go back to the LLM for up to 3 attempts. Code does all counting, so every number is reproducible and cited.
- **Strict matching over broad search.** A drug filter only matches InterventionName / OtherName / MeshTerm, not titles or descriptions. Tradeoff: higher precision (99%), lower recall (75%). The broad-search total is still reported in `sources` so the gap is visible.
- **Exact counts when the download is capped.** For single-value fields (phase, status, year, sponsor class, study type), bars come from per-value API counts instead of the downloaded sample. Other charts say clearly that they are based on a sample of the newest trials.
- **General rules, no per-drug hardcoding.** Drug synonyms are learned from each result set (other names, parentheses, MeSH terms), not from word lists. This loses some accuracy but works for any drug.
- **Form fields beat the text.** Structured request fields override the LLM, and compare groups may not contradict them.
- **Spec built from aggregates in one place** (`app/visualization/spec.py`). The aggregator stays chart-agnostic; the spec builder names fields, writes titles and encodings, and decides response size (ID and citation caps). The bundled UI renders only from this spec.
- **Caching and rate limiting.** File cache keyed by request params, a shared 50 req/min limiter, and retries with backoff.

## Accuracy

Measured on held-out questions; details and method in [docs/accuracy.md](docs/accuracy.md), scripts in `evals/`.

| | result |
|---|---|
| Question -> correct plan | 98% of 120 runs; 0 wrong filters |
| Bars from coded fields (phase, status, year, country, ...) | 100% exact (311 bars recounted independently) |
| Drug filter precision / recall | 99% / 75% |
| Condition filter precision / recall | 93% / 91% |

## Limitations and next steps

- **Drug and condition bars undercount** (by about 26% and 50% vs filtering on that name): drugs inside combination names ("EPOCH-R"), salt forms split ("Vincristine" vs "Vincristine Sulfate"), and conditions matched as exact text ("Stroke" excludes "Ischemic Stroke"). Next: map names to MeSH / RxNorm concepts.
- **Drug recall is 75%**: trials naming a drug only in arm descriptions or under a generic label are missed. Next: an optional LLM relevance check on broad-only matches.
- **Sample-based charts** (multi-value fields over 10,000 matches) are labelled as samples but undercount. Next: background full downloads or exact per-value counts.
- **Salt forms are separate drugs** ("Fludarabine" vs "Fludarabine Phosphate"), in bars and networks alike. Fixing it needs concept mapping, not a suffix word list.
- **Network links are ranked by raw co-occurrence** (each entity's 3 strongest links), so very common drugs still dominate. Next: rank by lift (observed vs expected co-occurrence) with a minimum support.
- **Sponsor names are not merged** ("Novartis" vs "Novartis Pharmaceuticals").
- **No adverse events, eligibility, or results data**: these questions are refused.
- **Next:** async job queue for large downloads, and more eval questions.

## Tools, validation, and authorship

**Tools:** Claude Code (Claude) wrote the code. OpenAI gpt-4.1 / gpt-4.1-mini run the planner and guardrail, and judge retrieval in the evals. Playwright drove the browser checks.

**Designed by me:** the overall system design (the LLM only plans and code does all counting; the pipeline stages; strict vs. broad matching; exact API counts for capped downloads; general rules over per-drug tuning) and the schemas: request, query plan, and visualization response.

**Generated by AI:** the implementation, from those designs. I reviewed it, directed fixes, and kept it to the designs above.

**Validation:**
- 110 tests (`pytest`) with a fake LLM and a fake ClinicalTrials.gov API, covering query building, normalization, aggregation, exact counts, plan rules, and the endpoints.
- Every chart type was run end to end in a real browser.
- Held-out accuracy evals (`evals/`, [docs/accuracy.md](docs/accuracy.md)): planner vs. expected plans (98%), every chart bar recounted with an independent API query (coded fields 100% exact), and filter precision/recall judged by an LLM on random samples. The evals found a real bug (Parkinson's recall 43% from the apostrophe), which was fixed.
