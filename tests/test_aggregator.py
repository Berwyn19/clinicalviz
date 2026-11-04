"""Aggregator tests on small hand-built trials. No internet, no LLM."""
from datetime import date

from app.aggregation.aggregator import Group, aggregate
from app.schemas.common import ChartType, Dimension, Intent, NumericField, OverallStatus
from app.schemas.plan import Filters, NetworkSpec, QueryPlan
from app.schemas.trial import Intervention, Site, Trial

TODAY = date(2026, 11, 3)


def trial(nct, **kw):
    base = dict(nct_id=nct, title=f"Trial {nct}", status="COMPLETED", study_type="INTERVENTIONAL",
                phases=[], phase="NA", start_date=None, start_year=None, completion_date=None,
                duration_months=None, enrollment=None, lead_sponsor=None, lead_sponsor_key=None,
                lead_sponsor_display=None, sponsor_class=None)
    base.update(kw)
    return Trial(**base)


def drugs(*names):
    return [Intervention(name=n, type="DRUG", key=n.lower(), display=n, is_drug_like=True,
                         components=[n.lower()]) for n in names]


def sponsor(name):
    return dict(lead_sponsor=name, lead_sponsor_key=name.lower(), lead_sponsor_display=name)


def plan(**kw):
    kw.setdefault("intent", Intent.CHART)
    kw.setdefault("filters", Filters())
    return QueryPlan(assumptions=[], **kw)


def run(p, *groups):
    return aggregate(p, list(groups), today=TODAY)


def g(trials, label=None, filters=None):
    return Group(label=label, filters=filters or Filters(), trials=trials)


def y_of(result, x, series=None):
    return next(r.y for r in result.rows if r.x == x and r.series == series)


# ------------------------------------------------------------------ bar charts

def test_phases_in_natural_order_with_readable_labels():
    ts = [trial("A", phase="PHASE2"), trial("B", phase="PHASE1/PHASE2"), trial("C", phase="PHASE2"),
          trial("D", phase="NA"), trial("E", phase="EARLY_PHASE1")]
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.PHASE), g(ts))
    assert r.categories == ["Early Phase 1", "Phase 1/2", "Phase 2", "N/A"]
    assert y_of(r, "Phase 2") == 2 and y_of(r, "Phase 1/2") == 1


def test_citations_capped_but_every_trial_id_kept():
    ts = [trial(f"NCT{i}", phase="PHASE3", phases=["PHASE3"]) for i in range(8)]
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.PHASE), g(ts))
    row = r.rows[0]
    assert row.trial_count == 8 and len(row.nct_ids) == 8 and len(row.citations) == 5
    assert row.citations[0].excerpt == "PHASE3"
    assert row.citations[0].field == "protocolSection.designModule.phases"


def test_multi_valued_country_counts_each_trial_once_per_country():
    ts = [trial("A", sites=[Site(country="United States"), Site(country="Germany")]),
          trial("B", sites=[Site(country="United States")])]
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.COUNTRY), g(ts))
    assert y_of(r, "United States") == 2 and y_of(r, "Germany") == 1
    assert any("counted once for each" in n for n in r.notes)


def test_country_uses_site_status_when_status_filtered():
    ts = [trial("A", status="RECRUITING", sites=[Site(country="United States", status="RECRUITING"),
                                                 Site(country="Germany", status="COMPLETED")]),
          trial("B", status="RECRUITING", sites=[Site(country="France")])]  # no site status -> trial's
    f = Filters(statuses=[OverallStatus.RECRUITING])
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.COUNTRY, filters=f), g(ts, filters=f))
    assert set(r.categories) == {"United States", "France"}


def test_default_top_n_and_explicit_limit():
    ts = [trial(f"T{i}", **sponsor(f"Sponsor {i:02d}")) for i in range(20)]
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.LEAD_SPONSOR), g(ts))
    assert len(r.categories) == 15 and r.stats["values_not_shown"] == 5
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.LEAD_SPONSOR, limit=3), g(ts))
    assert len(r.categories) == 3


def test_unknown_bar_only_when_missing_share_is_large():
    ts = [trial(f"T{i}", **sponsor("Acme")) for i in range(9)] + [trial("X")]  # 10% missing
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.LEAD_SPONSOR), g(ts))
    assert "Unknown" in r.categories
    ts = [trial(f"T{i}", **sponsor("Acme")) for i in range(99)] + [trial("X")]  # 1% missing
    r = run(plan(chart=ChartType.BAR_CHART, group_by=Dimension.LEAD_SPONSOR), g(ts))
    assert "Unknown" not in r.categories and any("no lead_sponsor value" in n for n in r.notes)


def test_grouped_bar_fills_zero_and_notes_overlap():
    shared = trial("S", phase="PHASE2")
    a = g([trial("A1", phase="PHASE1"), shared], label="Drug A")
    b = g([shared], label="Drug B")
    r = run(plan(chart=ChartType.GROUPED_BAR_CHART, group_by=Dimension.PHASE), a, b)
    assert r.series == ["Drug A", "Drug B"]
    assert y_of(r, "Phase 1", "Drug B") == 0 and y_of(r, "Phase 2", "Drug B") == 1
    assert any("more than one compared group" in n for n in r.notes)


# ------------------------------------------------------------------ time series

def test_time_series_fills_gaps_flags_partial_year_and_cuts_future():
    ts = [trial("A", start_year=2020, start_date="2020-01"), trial("B", start_year=2022, start_date="2022-05"),
          trial("C", start_year=2026, start_date="2026-02"), trial("D", start_year=2027, start_date="2027-01"),
          trial("E")]
    r = run(plan(chart=ChartType.TIME_SERIES, group_by=Dimension.START_YEAR), g(ts))
    assert r.categories == [2020, 2021, 2022, 2023, 2024, 2025, 2026]
    assert y_of(r, 2021) == 0
    assert next(row for row in r.rows if row.x == 2026).is_partial
    assert r.stats["future_not_shown"] == 1 and r.stats["missing_value"] == 1


def test_time_series_starts_at_filter_year():
    ts = [trial("A", start_year=2020, start_date="2020")]
    f = Filters(start_year_min=2018)
    r = run(plan(chart=ChartType.TIME_SERIES, group_by=Dimension.START_YEAR, filters=f), g(ts, filters=f))
    assert r.categories[0] == 2018


# ------------------------------------------------------------------ histogram / scatter

def test_enrollment_histogram_uses_fixed_bins_and_trims_empty_tail():
    ts = [trial("A", enrollment=0), trial("B", enrollment=5), trial("C", enrollment=30),
          trial("D", enrollment=30), trial("E")]
    r = run(plan(chart=ChartType.HISTOGRAM, numeric_field=NumericField.ENROLLMENT), g(ts))
    assert r.categories == ["0", "1–10", "11–50"]
    assert y_of(r, "11–50") == 2
    assert r.stats["missing_value"] == 1 and r.stats["median"] == 17.5


def test_scatter_needs_both_values():
    ts = [trial("A", enrollment=100, duration_months=12.0), trial("B", enrollment=50),
          trial("C", enrollment=10, duration_months=3.5)]
    r = run(plan(chart=ChartType.SCATTER_PLOT, x_field=NumericField.ENROLLMENT,
                 y_field=NumericField.DURATION_MONTHS), g(ts))
    assert len(r.rows) == 2 and r.stats["missing_value"] == 1
    assert len(r.rows[0].citations) == 2 and r.rows[0].label == "Trial A"


# ------------------------------------------------------------------ network

def test_drug_cooccurrence_network():
    ts = [trial("1", interventions=drugs("A", "B", "C")), trial("2", interventions=drugs("A", "B")),
          trial("3", interventions=drugs("A", "C")), trial("4", interventions=drugs("D"))]
    f = Filters(drug="A")
    p = plan(chart=ChartType.NETWORK_GRAPH, filters=f,
             network=NetworkSpec(source=Dimension.INTERVENTION, target=Dimension.INTERVENTION))
    r = run(p, g(ts, filters=f))
    edges = {(e.source, e.target): e.weight for e in r.edges}
    assert edges == {("intervention:a", "intervention:b"): 2, ("intervention:a", "intervention:c"): 2}
    ids = {n.id for n in r.nodes}
    assert ids == {"intervention:a", "intervention:b", "intervention:c"}  # D has no links, dropped
    assert next(n for n in r.nodes if n.id == "intervention:a").is_query_match


def test_sponsor_drug_network_and_weight_fallback():
    ts = [trial("1", interventions=drugs("X"), **sponsor("Merck")),
          trial("2", interventions=drugs("Y"), **sponsor("Pfizer"))]
    p = plan(chart=ChartType.NETWORK_GRAPH,
             network=NetworkSpec(source=Dimension.LEAD_SPONSOR, target=Dimension.INTERVENTION))
    r = run(p, g(ts))
    assert {(e.source, e.target) for e in r.edges} == {("lead_sponsor:merck", "intervention:x"),
                                                       ("lead_sponsor:pfizer", "intervention:y")}
    assert r.stats["min_edge_weight"] == 1  # every link has weight 1, so the threshold dropped to 1


# ------------------------------------------------------------------ table

def test_trial_list_newest_first_with_limit():
    ts = [trial("Old", start_date="2010-01"), trial("New", start_date="2024-06"), trial("NoDate")]
    r = run(plan(intent=Intent.TRIAL_LIST, chart=ChartType.TABLE, limit=2), g(ts))
    assert [row.nct_id for row in r.table] == ["New", "Old"]
    assert r.table[0].url == "https://clinicaltrials.gov/study/New"

def test_network_keeps_only_each_nodes_strongest_links():
    from app.aggregation.aggregator import _Bucket, _strongest_links

    def bucket(n):
        b = _Bucket("")
        b.nct_ids = [f"NCT{i}" for i in range(n)]
        return b
    # hub H links to A-D; A-B is weak but it is among A's (and B's) top links
    edges = [(("H", "A"), bucket(9)), (("H", "B"), bucket(8)), (("H", "C"), bucket(7)),
             (("H", "D"), bucket(6)), (("A", "B"), bucket(2))]
    kept = {k for k, _ in _strongest_links(edges, per_node=1)}
    assert kept == {("H", "A"), ("H", "B"), ("H", "C"), ("H", "D")}   # A-B is nobody's strongest
    kept2 = {k for k, _ in _strongest_links(edges, per_node=2)}
    assert ("A", "B") in kept2


def test_bucket_label_is_most_common_spelling_capitalized():
    from app.aggregation.aggregator import _Bucket
    b = _Bucket("rituximab")
    for label in ["rituximab", "rituximab", "Rituximab"]:
        b.add("NCT1", "f", "e", label)
    assert b.display == "Rituximab"
    b2 = _Bucket("x")
    for label in ["Fludarabine Phosphate", "fludarabine phosphate", "Fludarabine Phosphate"]:
        b2.add("NCT1", "f", "e", label)
    assert b2.display == "Fludarabine Phosphate"
