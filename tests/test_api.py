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
    assert body["status"] == "ok" and body["meta"]["plan"]["chart"] == "bar_chart"
    viz = body["visualization"]
    assert viz["type"] == "bar_chart" and viz["title"] == "Trials of pembrolizumab, by phase"
    assert viz["encoding"]["x"]["field"] == "phase" and viz["encoding"]["y"]["field"] == "trial_count"
    assert viz["data"][0]["phase"] == "Phase 2" and viz["data"][0]["trial_count"] == 2
    assert viz["data"][0]["citations"][0] == {"nct_id": "NCT0", "excerpt": "PHASE2"}
    assert "bin_start" not in viz["data"][0]          # nulls are dropped
    meta = body["meta"]
    assert meta["filters"] == {"drug": "pembrolizumab"} and meta["count_source"] == "downloaded"
    assert meta["sources"][0]["matched"] == 2 and meta["sources"][0]["broad_matched"] == 5


def test_off_topic_question_is_unsupported_not_an_error():
    tc = client(FakeLLM(PHASE_PLAN, relevant=False))
    body = tc.post("/api/query", json={"query": "best pizza in town?"}).json()
    assert body["status"] == "unsupported" and body["meta"]["reason"] == "off topic"
    assert "visualization" not in body


def test_edited_plan_is_validated():
    tc = client()
    bad = {"intent": "chart", "chart": "bar_chart", "filters": {}, "assumptions": []}   # no group_by
    resp = tc.post("/api/plan", json={"plan": bad})
    assert resp.status_code == 422 and "bar_chart needs group_by" in resp.json()["detail"]
    ok = tc.post("/api/plan", json={"plan": PHASE_PLAN.model_dump(mode="json")})
    assert ok.status_code == 200 and ok.json()["visualization"]["type"] == "bar_chart"


def test_trial_ids_are_capped_but_count_stays_true():
    from app.aggregation.models import AggregateResult, Datum
    from app.visualization.spec import MAX_IDS_PER_DATUM, build_response
    ids = [f"NCT{i:08d}" for i in range(50)]
    r = AggregateResult(kind="rows", categories=["Phase 2"], stats={"matched": 50, "downloaded": 50},
                        rows=[Datum(x="Phase 2", y=50, trial_count=50, nct_ids=ids, citations=[])])
    datum = build_response(PHASE_PLAN, r).visualization.data[0]
    assert datum["trial_count"] == 50 and len(datum["nct_ids"]) == MAX_IDS_PER_DATUM
    assert datum["nct_ids_complete"] is False


def test_shared_citation_field_is_stated_once():
    viz = client(FakeLLM(PHASE_PLAN)).post("/api/query", json={"query": "pembrolizumab by phase"}).json()["visualization"]
    assert viz["encoding"]["citations"]["field"] == "protocolSection.designModule.phases"
    assert "field" not in viz["data"][0]["citations"][0]
