"""The eval scoring itself: a bug here would silently report wrong accuracy."""
from types import SimpleNamespace

from evals.eval_counts import _phase_keys, gold_plan, recount_params
from evals.eval_planner import best_alternative, series_match
from evals.eval_retrieval import estimate
from app.schemas.plan import CompareGroup, Filters, QueryPlan


def plan(**kw) -> QueryPlan:
    return QueryPlan(intent="chart", assumptions=[], **{"filters": Filters(), **kw})


def test_series_match_ignores_where_shared_filters_live_and_group_order():
    p = plan(chart="time_series", group_by="start_year", filters=Filters(phases=["PHASE3"]),
             compare=[CompareGroup(label="b", filters=Filters(sponsor="Novartis")),
                      CompareGroup(label="a", filters=Filters(sponsor="Pfizer", phases=["PHASE3"]))])
    expected = [{"sponsor": "pfizer", "phases": ["PHASE3"]}, {"sponsor": "Novartis", "phases": ["PHASE3"]}]
    from evals.eval_planner import effective_filters
    assert series_match(expected, effective_filters(p))


def test_extra_filter_is_wrong_and_text_alternatives_are_allowed():
    from evals.eval_planner import effective_filters
    p = plan(chart="bar_chart", group_by="phase", filters=Filters(condition="COPD", country="Japan"))
    assert not series_match([{"condition": "copd"}], effective_filters(p))
    p2 = plan(chart="bar_chart", group_by="phase", filters=Filters(condition="Chronic obstructive pulmonary disease"))
    assert series_match([{"condition": ["copd", "chronic obstructive pulmonary disease"]}], effective_filters(p2))


def test_alternatives_pass_if_any_matches():
    case = {"alternatives": [{"intent": "unsupported"},
                             {"intent": "chart", "chart": "time_series", "filters": [{"drug": "x"}]}]}
    unsupported = QueryPlan(intent="unsupported", reason="r", filters=Filters(), assumptions=[])
    assert all(best_alternative(case, unsupported).values())


def test_phase_labels_parse_back_to_keys():
    assert _phase_keys("Phase 1/2") == ["PHASE1", "PHASE2"]
    assert _phase_keys("Early Phase 1 / Phase 1") == ["EARLY_PHASE1", "PHASE1"]
    assert _phase_keys("N/A") == []


def test_recount_adds_bar_condition_to_series_query():
    base = {"filter.advanced": "(AREA[Condition](asthma))", "filter.overallStatus": "COMPLETED,RECRUITING"}
    row = SimpleNamespace(x="Phase 2", bin_start=None, bin_end=None)
    p = recount_params("phase", row, base)
    assert p["filter.advanced"].startswith("(AREA[Condition](asthma)) AND AREA[Phase]PHASE2 AND NOT")
    status = recount_params("status", SimpleNamespace(x="Recruiting"), base)
    assert status["filter.overallStatus"] == "RECRUITING"
    assert recount_params("status", SimpleNamespace(x="Withdrawn"), base) is None


def test_gold_plan_builds_compare_groups_from_expected_series():
    case = {"expect": {"intent": "chart", "chart": "grouped_bar_chart", "group_by": "phase",
                       "filters": [{"condition": "asthma"}, {"condition": ["copd", "x"]}]}}
    p = gold_plan(case)
    assert [g.filters.condition for g in p.compare] == ["asthma", "copd"]


def test_recall_estimate_scales_samples_to_population():
    import random
    est = estimate(900, ["yes"] * 10, 100, ["yes"] * 5 + ["no"] * 5, random.Random(0))
    assert est["precision"] == 1.0
    assert abs(est["recall"] - 900 / 950) < 1e-9
