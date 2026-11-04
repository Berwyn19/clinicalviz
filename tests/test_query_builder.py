import pytest

from app.ctgov.query_builder import (
    area_group, broad_params, build_queries, sanitize, strict_params,
)
from app.schemas.common import ChartType, Dimension, Intent, OverallStatus, Phase
from app.schemas.plan import CompareGroup, Filters, QueryPlan

DRUG_GROUP = ("(AREA[InterventionName](pembrolizumab) OR AREA[InterventionOtherName](pembrolizumab)"
              " OR AREA[InterventionMeshTerm](pembrolizumab))")


def test_strict_params_drug_only():
    assert strict_params(Filters(drug="Pembrolizumab")) == {"filter.advanced": DRUG_GROUP}


def test_strict_params_combines_everything_with_and():
    params = strict_params(Filters(
        drug="pembrolizumab", condition="lung cancer", country="Germany",
        statuses=[OverallStatus.RECRUITING], phases=[Phase.PHASE2, Phase.PHASE3],
        start_year_min=2015,
    ))
    assert params["filter.advanced"] == " AND ".join([
        DRUG_GROUP,
        "(AREA[Condition](lung cancer) OR AREA[ConditionMeshTerm](lung cancer))",
        "(AREA[LocationCountry](germany))",
        "AREA[Phase](PHASE2 OR PHASE3)",
        "AREA[StartDate]RANGE[2015-01-01,MAX]",
    ])
    assert params["filter.overallStatus"] == "RECRUITING"
    assert not any(k.startswith("query.") for k in params)  # strict search uses no broad params


def test_broad_params_use_query_params_and_same_structure():
    params = broad_params(Filters(drug="pembrolizumab", phases=[Phase.PHASE3]))
    assert params == {"query.intr": "pembrolizumab", "filter.advanced": "AREA[Phase](PHASE3)"}


def test_year_range_only_max():
    assert strict_params(Filters(start_year_max=2020))["filter.advanced"] == \
        "AREA[StartDate]RANGE[MIN,2020-12-31]"


@pytest.mark.parametrize("raw, expected", [
    ("Pembrolizumab", "pembrolizumab"),
    ("lung cancer) OR AREA[Phase](PHASE1", "lung cancer or area phase phase1"),  # injection attempt
    ('"MK-3475"', "mk-3475"),
    ("Merck Sharp & Dohme", "merck sharp dohme"),
    ("Korea, Republic of", "korea, republic of"),
    ("Parkinson's disease", "parkinson disease"),      # possessive blocks the API's MeSH expansion
    ("Crohn\u2019s Disease", "crohn disease"),
    ("O'Brien", "o brien"),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_rejects_empty():
    with pytest.raises(ValueError):
        sanitize("[]()")


def test_area_group_cannot_be_broken_out_of():
    group = area_group(["Condition"], "x) OR AREA[Phase](PHASE1")
    assert group.count("AREA[") == 1  # still exactly one field search


def test_compare_makes_one_query_per_group_and_group_wins():
    plan = QueryPlan(
        intent=Intent.CHART, chart=ChartType.GROUPED_BAR_CHART, group_by=Dimension.PHASE,
        filters=Filters(drug="shared-drug", condition="lung cancer"),
        compare=[CompareGroup(label="A", filters=Filters(drug="drug-a")),
                 CompareGroup(label="B", filters=Filters(drug="drug-b"))],
        assumptions=[],
    )
    queries = build_queries(plan)
    assert [q.label for q in queries] == ["A", "B"]
    assert "AREA[InterventionName](drug-a)" in queries[0].params["filter.advanced"]
    assert "AREA[InterventionName](drug-b)" in queries[1].params["filter.advanced"]
    assert all("AREA[Condition](lung cancer)" in q.params["filter.advanced"] for q in queries)
    assert [q.broad_params["query.intr"] for q in queries] == ["drug-a", "drug-b"]


def test_no_broad_count_without_text_filters():
    plan = QueryPlan(intent=Intent.CHART, chart=ChartType.BAR_CHART, group_by=Dimension.COUNTRY,
                     filters=Filters(phases=[Phase.PHASE3]), assumptions=[])
    assert build_queries(plan)[0].broad_params is None


def test_fields_follow_the_plan():
    plan = QueryPlan(intent=Intent.CHART, chart=ChartType.BAR_CHART,
                     group_by=Dimension.COUNTRY, filters=Filters(condition="asthma"), assumptions=[])
    fields = build_queries(plan)[0].params["fields"].split(",")
    assert {"LocationCountry", "LocationStatus", "Condition", "ConditionMeshTerm"} <= set(fields)
    assert "InterventionName" not in fields  # not needed for this plan