"""Request fields must win over compare groups (no LLM: a fake client returns canned plans)."""
from types import SimpleNamespace

import pytest

from app.ctgov.query_builder import build_queries
from app.planner.planner import PlannerError, find_compare_conflicts, make_plan
from app.schemas.common import ChartType, Dimension, Intent, Phase
from app.schemas.plan import CompareGroup, Filters, QueryPlan
from app.schemas.request import QueryRequest


def compare_plan(*groups: tuple[str, Filters], shared: Filters | None = None) -> QueryPlan:
    return QueryPlan(intent=Intent.CHART, chart=ChartType.GROUPED_BAR_CHART, group_by=Dimension.PHASE,
                     filters=shared or Filters(), assumptions=[],
                     compare=[CompareGroup(label=label, filters=f) for label, f in groups])


class FakeLLM:
    """Stands in for AsyncOpenAI: returns the given plans in order and records the messages."""

    def __init__(self, *plans: QueryPlan):
        self.plans, self.calls = list(plans), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    async def _parse(self, model, messages, response_format):
        self.calls.append(list(messages))
        plan = self.plans.pop(0)
        msg = SimpleNamespace(refusal=None, content=plan.model_dump_json(), parsed=plan)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


REQ = QueryRequest(query="compare phases across drugs", drug_name="Pembrolizumab")
CONFLICTING = compare_plan(("Nivolumab", Filters(drug="nivolumab")), ("Atezolizumab", Filters(drug="atezolizumab")))
VALID = compare_plan(("Phase 2", Filters(phases=[Phase.PHASE2])), ("Phase 3", Filters(phases=[Phase.PHASE3])))


def test_group_replacing_a_request_field_is_a_conflict():
    errors = find_compare_conflicts(CONFLICTING, REQ)
    assert len(errors) == 2 and "fixes drug='Pembrolizumab'" in errors[0]


def test_groups_may_narrow_request_fields():
    req = QueryRequest(query="phase 2 vs 3", trial_phase=[Phase.PHASE2, Phase.PHASE3], start_year=2015)
    plan = compare_plan(("P2", Filters(phases=[Phase.PHASE2], start_year_min=2018)),
                        ("P3", Filters(phases=[Phase.PHASE3])))
    assert find_compare_conflicts(plan, req) == []
    wider = compare_plan(("P1", Filters(phases=[Phase.PHASE1])), ("P2", Filters(phases=[Phase.PHASE2])))
    assert find_compare_conflicts(wider, req)


def test_same_text_value_in_a_group_is_fine():
    plan = compare_plan(("A", Filters(drug="pembrolizumab", condition="melanoma")),
                        ("B", Filters(drug="PEMBROLIZUMAB", condition="nsclc")))
    assert find_compare_conflicts(plan, REQ) == []


@pytest.mark.asyncio
async def test_planner_retries_and_every_query_keeps_the_request_drug():
    llm = FakeLLM(CONFLICTING, VALID)
    plan = await make_plan(REQ, llm, model="fake")
    assert len(llm.calls) == 2
    assert "fixes drug='Pembrolizumab'" in llm.calls[1][-1]["content"]   # the error was fed back
    queries = build_queries(plan)
    assert all("AREA[InterventionName](pembrolizumab)" in q.params["filter.advanced"] for q in queries)


@pytest.mark.asyncio
async def test_planner_gives_up_if_conflict_persists():
    with pytest.raises(PlannerError):
        await make_plan(REQ, FakeLLM(CONFLICTING, CONFLICTING, CONFLICTING), model="fake")
