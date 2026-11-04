# clinicalviz

Ask a question about ClinicalTrials.gov and get a chart. Every number is counted from registry records and links back to the trials and fields it came from.

## Setup

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env
```

## Run

```
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

- `POST /api/query`: a question (plus optional filters) returns the plan, chart data, and sources.
- `POST /api/plan`: runs a plan directly, without the LLM. The page uses this for "Edit the plan and re-run".
- API docs: http://127.0.0.1:8000/docs

## Tests and scripts

```
pytest
python -m scripts.run_pipeline "How has the number of pembrolizumab trials changed over time?"
```

## Pipeline

question -> guardrail + planner (LLM, `app/planner`) -> API queries (`app/ctgov/query_builder.py`) -> fetch (`app/ctgov/client.py`) -> normalize (`app/processing`) -> exact counts if truncated (`app/ctgov/counts.py`) -> aggregate (`app/aggregation`) -> frontend (`app/static`)
