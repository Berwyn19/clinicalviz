"""HTTP endpoints, with the LLM and ClinicalTrials.gov replaced by fakes (no network)."""
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.ctgov.client import CTGovClient, RateLimiter
from app.main import app
from app.schemas.plan import Filters, QueryPlan


def fake_ctgov(req: httpx.Request) -> httpx.Response:
    p = req.url.params
    if p.get("pageSize") == "1":
        return httpx.Response(200, json={"totalCount": 5, "studies": []})
    return httpx.Response(200, json={"totalCount": 2, "studies": [
        {"protocolSection": {"identificationModule": {"nctId": f"NCT{i}", "briefTitle": "t"},
                             "designModule": {"phases": ["PHASE2"]}}} for i in range(2)]})


class FakeLLM:
    def __init__(self, plan: QueryPlan, relevant=True):
        self.plan, self.relevant = plan, relevant
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    async def _parse(self, model, messages, response_format):
        parsed = (response_format(relevant=self.relevant, reason="off topic")
                  if response_format.__name__ == "GuardrailResult" else self.plan)
        msg = SimpleNamespace(refusal=None, content=parsed.model_dump_json(), parsed=parsed)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    async def close(self):
        pass


PHASE_PLAN = QueryPlan(intent="chart", chart="bar_chart", group_by="phase",
                       filters=Filters(drug="pembrolizumab"), assumptions=[])


def client(llm=None) -> TestClient:
    tc = TestClient(app)
    tc.__enter__()   # runs the lifespan, then swap in fakes
    app.state.ctgov = CTGovClient(http=httpx.AsyncClient(transport=httpx.MockTransport(fake_ctgov)),
                                  rate_limiter=RateLimiter(1000))
    if llm:
        app.state.llm = llm
    return tc


def test_index_and_static_files_are_served():
    tc = client()
    assert "Trial Explorer" in tc.get("/").text
    assert tc.get("/static/app.js").status_code == 200


def test_query_returns_plan_chart_data_and_sources():
    tc = client(FakeLLM(PHASE_PLAN))
    body = tc.post("/api/query", json={"query": "pembrolizumab trials by phase"}).json()
    assert body["status"] == "ok" and body["plan"]["chart"] == "bar_chart"
    assert body["result"]["rows"][0]["x"] == "Phase 2" and body["result"]["rows"][0]["y"] == 2
    assert body["sources"][0]["matched"] == 2 and body["sources"][0]["broad_matched"] == 5


def test_off_topic_question_is_unsupported_not_an_error():
    tc = client(FakeLLM(PHASE_PLAN, relevant=False))
    body = tc.post("/api/query", json={"query": "best pizza in town?"}).json()
    assert body["status"] == "unsupported" and body["plan"]["reason"] == "off topic"


def test_edited_plan_is_validated():
    tc = client()
    bad = {"intent": "chart", "chart": "bar_chart", "filters": {}, "assumptions": []}   # no group_by
    resp = tc.post("/api/plan", json={"plan": bad})
    assert resp.status_code == 422 and "bar_chart needs group_by" in resp.json()["detail"]
    ok = tc.post("/api/plan", json={"plan": PHASE_PLAN.model_dump(mode="json")})
    assert ok.status_code == 200 and ok.json()["result"]["kind"] == "rows"
