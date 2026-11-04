"""Plan-rule tests. No LLM or network needed."""
from app.schemas.common import ChartType, Dimension, Intent, NumericField
from app.schemas.plan import CompareGroup, Filters, NetworkSpec, QueryPlan, find_plan_errors


def plan(**kw):
    base = dict(intent=Intent.CHART, filters=Filters(drug="pembrolizumab"), assumptions=[])
    return QueryPlan(**{**base, **kw})


def test_valid_time_series():
    assert find_plan_errors(plan(chart=ChartType.TIME_SERIES, group_by=Dimension.START_YEAR)) == []


def test_time_series_needs_start_year():
    assert find_plan_errors(plan(chart=ChartType.TIME_SERIES, group_by=Dimension.PHASE))


def test_grouped_bar_needs_two_groups():
    one = [CompareGroup(label="A", filters=Filters(drug="a"))]
    assert find_plan_errors(plan(chart=ChartType.GROUPED_BAR_CHART, group_by=Dimension.PHASE, compare=one))


def test_scatter_fields_must_differ():
    p = plan(chart=ChartType.SCATTER_PLOT, x_field=NumericField.ENROLLMENT, y_field=NumericField.ENROLLMENT)
    assert find_plan_errors(p)


def test_network_rejects_bad_node_type():
    p = plan(chart=ChartType.NETWORK_GRAPH,
             network=NetworkSpec(source=Dimension.PHASE, target=Dimension.INTERVENTION))
    assert find_plan_errors(p)


def test_bad_year_range():
    p = plan(chart=ChartType.BAR_CHART, group_by=Dimension.PHASE,
             filters=Filters(start_year_min=2020, start_year_max=2015))
    assert find_plan_errors(p)


def test_unsupported_needs_reason():
    assert find_plan_errors(QueryPlan(intent=Intent.UNSUPPORTED, filters=Filters(), assumptions=[]))