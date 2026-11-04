"""FastAPI app: the query endpoint plus the static frontend.

  uvicorn app.main:app --reload        then open http://127.0.0.1:8000

POST /api/query  question (+ optional hard filters) -> {status, visualization, meta}
POST /api/plan   a plan (e.g. edited in the UI) -> same response, without the LLM step
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.config import settings
from app.ctgov.client import CTGovError, make_client
from app.pipeline import run_plan
from app.planner.guardrail import GuardrailError
from app.planner.planner import PlannerError
from app.planner.preprocess import preprocess
from app.schemas.common import Intent
from app.schemas.plan import QueryPlan, find_plan_errors
from app.schemas.request import QueryRequest
from app.visualization.spec import QueryResponse, Source, build_response

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class PlanRequest(BaseModel):
    plan: QueryPlan
    max_trials: int = Field(10000, ge=1, le=20000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.llm = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)
    async with make_client() as ctgov:
        app.state.ctgov = ctgov
        yield
    await app.state.llm.close()


app = FastAPI(title="ClinicalTrials.gov visualizer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/query", response_model=QueryResponse, response_model_exclude_none=True)
async def query(req: QueryRequest) -> QueryResponse:
    t0 = time.perf_counter()
    try:
        plan = await preprocess(req, app.state.llm)
    except (GuardrailError, PlannerError) as exc:
        raise HTTPException(502, f"Could not interpret the question: {exc}") from exc
    planned = time.perf_counter() - t0
    if plan.intent == Intent.UNSUPPORTED:
        return build_response(plan, None, query=req.query, timings={"plan_s": planned})
    resp = await _run(plan, req.max_trials, query=req.query)
    resp.meta.timings["plan_s"] = planned
    return resp


@app.post("/api/plan", response_model=QueryResponse, response_model_exclude_none=True)
async def plan_only(body: PlanRequest) -> QueryResponse:
    errors = find_plan_errors(body.plan)
    if errors:
        raise HTTPException(422, "Invalid plan: " + " ".join(errors))
    if body.plan.intent == Intent.UNSUPPORTED:
        return build_response(body.plan, None)
    return await _run(body.plan, body.max_trials)


async def _run(plan: QueryPlan, max_trials: int, query: str | None = None) -> QueryResponse:
    t0 = time.perf_counter()
    try:
        out = await run_plan(plan, app.state.ctgov, max_trials=max_trials,
                             max_count_requests=settings.ctgov_exact_count_max_requests)
    except CTGovError as exc:
        raise HTTPException(502, f"ClinicalTrials.gov request failed: {exc}") from exc
    except ValueError as exc:   # e.g. a filter value that is empty after sanitizing
        raise HTTPException(422, str(exc)) from exc
    sources = [Source(label=r.query.label, request_url=r.request_url, matched=r.total_count,
                      downloaded=len(r.studies), broad_matched=r.broad_total_count) for r in out.fetches]
    return build_response(plan, out.result, query=query, sources=sources,
                          timings={"data_s": time.perf_counter() - t0})
