"""Normalized trials + plan -> chart data with citations.

One function per transform. None of them checks which dimension it is working
on: dimension-specific logic lives in dimensions.py.

Counting rules (also reported in `notes`):
- A trial counts at most once per value. With multi-value dimensions (countries,
  drugs, conditions) one trial can count for several values, so totals can exceed
  the number of trials.
- Each datum lists every NCT ID behind it, plus up to CITATION_LIMIT full citations.

Coverage: if the API matched more trials than were downloaded, the downloaded trials
are only a sample. Then either the group carries exact API counts (single-valued
dimensions, see app/ctgov/counts.py) and those are used as the numbers, or the result
says plainly that it describes a sample. Either way it is in `notes` and `stats`.
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations, product

from app.processing.normalizer import make_key
from app.schemas.common import ChartType, Dimension, Intent
from app.schemas.plan import Filters, QueryPlan
from app.schemas.trial import Trial

from .dimensions import (
    DIMENSIONS, FILTER_FOR_DIMENSION, NUMERICS, Context, DimensionSpec, Evidence, NumericSpec, year_range,
)
from .models import AggregateResult, Citation, Datum, Edge, ExactCounts, Node, TableRow

CITATION_LIMIT = 5
DEFAULT_TOP_N = 15            # for dimensions without a fixed order (sponsors, countries, ...)
UNKNOWN_SHARE = 0.05          # show an "Unknown" bar only above this share of trials
NETWORK_MAX_NODES = 30
NETWORK_MIN_EDGE_WEIGHT = 2
NETWORK_LINKS_PER_NODE = 3    # keep a link only if it is among the strongest of one of its two nodes
SCATTER_MAX_POINTS = 5000
TABLE_DEFAULT_LIMIT = 50


@dataclass
class Group:
    """Trials for one series: the whole result, or one compare group."""
    label: str | None
    filters: Filters
    trials: list[Trial]
    total_count: int | None = None          # trials the API matched; None = every match was downloaded
    exact: ExactCounts | None = None        # exact per-bucket counts from the API (truncated groups only)
    drug_aliases: dict[str, str] = field(default_factory=dict)  # from the normalizer, for query matching

    @property
    def truncated(self) -> bool:
        return self.total_count is not None and self.total_count > len(self.trials)

    @property
    def matched(self) -> int:
        return self.total_count if self.total_count is not None else len(self.trials)


@dataclass
class _Bucket:
    label: str
    nct_ids: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    exact: int | None = None    # exact API count; nct_ids is then only the downloaded sample
    spellings: Counter = field(default_factory=Counter)   # display names seen for this key

    def add(self, nct_id: str, field_path: str, excerpt: str, label: str | None = None) -> None:
        self.nct_ids.append(nct_id)
        if label:
            self.spellings[label] += 1
        if len(self.citations) < CITATION_LIMIT:
            self.citations.append(Citation(nct_id=nct_id, field=field_path, excerpt=excerpt))

    @property
    def display(self) -> str:
        """The most common spelling across trials (not whichever trial came first);
        all-lowercase names get a capital first letter ('rituximab' -> 'Rituximab')."""
        if not self.spellings:
            return self.label
        name = max(self.spellings.items(), key=lambda kv: (kv[1], kv[0] != kv[0].lower(), kv[0]))[0]
        return name[:1].upper() + name[1:] if name == name.lower() else name

    @property
    def count(self) -> int:
        return self.exact if self.exact is not None else len(self.nct_ids)

    @property
    def ids_complete(self) -> bool:
        return self.exact is None or self.exact == len(self.nct_ids)


def _ctx(filters: Filters) -> Context:
    return Context(statuses=frozenset(s.value for s in filters.statuses or []))


def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    seen, out = set(), []
    for ev in evidence:
        if ev.key not in seen:
            seen.add(ev.key)
            out.append(ev)
    return out


def _datum(x, bucket: _Bucket | None, series, **extra) -> Datum:
    b = bucket or _Bucket(label=str(x))
    return Datum(x=x, y=b.count, series=series, trial_count=b.count, nct_ids=b.nct_ids,
                 nct_ids_complete=b.ids_complete, citations=b.citations, **extra)


def _overlap_note(groups: list[Group]) -> list[str]:
    if len(groups) < 2:
        return []
    seen = Counter(nct for g in groups for nct in {t.nct_id for t in g.trials})
    shared = sum(1 for n in seen.values() if n > 1)
    return [f"{shared} trials belong to more than one compared group and are counted in each."] if shared else []


# ------------------------------------------------------------------ bar / grouped bar

def group_count(plan: QueryPlan, groups: list[Group]) -> AggregateResult:
    dim = plan.group_by
    spec: DimensionSpec = DIMENSIONS[dim]
    per_series: dict[str | None, dict[str, _Bucket]] = {}
    missing: dict[str | None, list[str]] = {}

    for g in groups:
        buckets, ctx = {}, _ctx(g.filters)
        missing[g.label] = []
        for t in g.trials:
            evidence = _dedupe(spec.extract(t, ctx))
            if not evidence:
                missing[g.label].append(t.nct_id)
            for ev in evidence:
                buckets.setdefault(ev.key, _Bucket(ev.label)).add(t.nct_id, ev.field, ev.excerpt, ev.label)
        if g.exact:
            buckets = _apply_exact(buckets, g.exact, spec)
        per_series[g.label] = buckets

    totals = Counter()
    for buckets in per_series.values():
        for k, b in buckets.items():
            totals[k] += b.count

    keys = _order_keys(totals, spec, plan.sort)
    limit = plan.limit or (None if spec.order else DEFAULT_TOP_N)
    hidden = max(0, len(keys) - limit) if limit else 0
    keys = keys[:limit] if limit else keys

    labels = {k: b.display for buckets in per_series.values() for k, b in buckets.items()}
    rows = [_datum(labels[k], per_series[g.label].get(k), g.label) for k in keys for g in groups]

    notes = _overlap_note(groups)
    n_trials = sum(g.exact.total if g.exact else len(g.trials) for g in groups)
    # Groups counted from downloaded records know exactly which trials have no value.
    counted_here = [g for g in groups if not g.exact]
    n_missing = sum(len(missing[g.label]) for g in counted_here)
    n_here = sum(len(g.trials) for g in counted_here)
    if n_missing and n_here and n_missing / n_here > UNKNOWN_SHARE:
        for g in counted_here:
            b = _Bucket("Unknown")
            for nct in missing[g.label]:
                b.add(nct, f"(no {dim.value} value)", "")
            rows.append(_datum("Unknown", b, g.label))
        keys = keys + ["__unknown__"]
        labels["__unknown__"] = "Unknown"
    elif n_missing:
        notes.append(f"{n_missing} trials have no {dim.value} value and are not shown.")
    # Groups with exact API counts only know how many trials fell in none of the counted values.
    not_counted = sum(g.exact.not_counted for g in groups if g.exact)
    if not_counted:
        notes.append(f"{not_counted} trials are not in the bars shown: they have no {dim.value} value, "
                     "or a rare one that was not in the downloaded sample.")
        n_missing += not_counted
    if hidden:
        notes.append(f"Showing the top {limit} of {limit + hidden} {dim.value} values; {hidden} more not shown.")
    if spec.multi_valued:
        notes.append(f"A trial can have several {dim.value} values and is counted once for each, "
                     "so bar totals can exceed the number of trials.")
    if dim == Dimension.COUNTRY and any(g.filters.statuses for g in groups):
        notes.append("Countries are counted by site status: a trial counts for a country only if a site "
                     "there matches the status filter.")

    return AggregateResult(
        kind="rows", rows=rows, categories=[labels[k] for k in keys],
        series=[g.label for g in groups if g.label], notes=notes,
        stats={"trials": n_trials, "missing_value": n_missing, "values_not_shown": hidden},
    )


def _apply_exact(sample: dict, exact: ExactCounts, spec: DimensionSpec) -> dict:
    """Exact API counts become the bucket numbers; the sample keeps supplying IDs and citations."""
    out = {}
    for key, n in exact.counts.items():
        if n:
            b = sample.get(key) or _Bucket(spec.label_of(key))
            b.exact = n
            out[key] = b
    return out


def _order_keys(totals: Counter, spec: DimensionSpec, sort: str | None) -> list[str]:
    if spec.order:  # natural order (e.g. phases) always wins
        known = [k for k in spec.order if k in totals]
        return known + sorted(k for k in totals if k not in spec.order)
    if sort == "key_asc":
        return sorted(totals)
    if sort == "value_asc":
        return [k for k, _ in sorted(totals.items(), key=lambda kv: (kv[1], kv[0]))]
    return [k for k, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]


# ------------------------------------------------------------------ time series

def time_series(plan: QueryPlan, groups: list[Group], today: date) -> AggregateResult:
    spec = DIMENSIONS[Dimension.START_YEAR]
    per_series: dict[str | None, dict[int, _Bucket]] = {}
    no_year, years_seen = 0, set()
    for g in groups:
        buckets, ctx = {}, _ctx(g.filters)
        for t in g.trials:
            evidence = spec.extract(t, ctx)
            if not evidence:
                if not g.exact:          # exact groups report their own not-counted total
                    no_year += 1
                continue
            ev, year = evidence[0], int(evidence[0].key)
            years_seen.add(year)
            buckets.setdefault(year, _Bucket(ev.label)).add(t.nct_id, ev.field, ev.excerpt)
        if g.exact:
            buckets = {int(k): b for k, b in _apply_exact({str(y): b for y, b in buckets.items()}, g.exact, spec).items()}
        per_series[g.label] = buckets

    notes = _overlap_note(groups)
    current = today.year
    exact_years = sorted({int(k) for g in groups if g.exact for k in g.exact.counts})
    if exact_years:      # the years the API was asked to count (chosen with the same year_range rule)
        lo, hi = exact_years[0], exact_years[-1]
    elif years_seen:
        lo, hi = year_range(plan.filters.start_year_min, plan.filters.start_year_max,
                            min(years_seen), max(years_seen), current)
    else:
        return AggregateResult(kind="rows", notes=notes + ["No trials have a start date."],
                               stats={"missing_value": no_year})

    before = after = 0
    for g in groups:
        if not g.exact:
            before += sum(b.count for y, b in per_series[g.label].items() if y < lo)
            after += sum(b.count for y, b in per_series[g.label].items() if y > hi)
    not_counted = sum(g.exact.not_counted for g in groups if g.exact)

    rows = [
        _datum(year, per_series[g.label].get(year), g.label, is_partial=(year == current))
        for year in range(lo, hi + 1) for g in groups
    ]
    if no_year:
        notes.append(f"{no_year} trials have no start date and are not shown.")
    if before:
        notes.append(f"{before} trials that started before {lo} are not shown.")
    if after:
        notes.append(f"{after} trials planned to start after {hi} are not shown.")
    if not_counted:
        notes.append(f"{not_counted} trials have no start date or start outside {lo}-{hi} and are not shown.")
    if lo <= current <= hi:
        notes.append(f"{current} is still in progress, so its count is incomplete.")
    notes.append("Years with no trials are shown as 0.")

    return AggregateResult(
        kind="rows", rows=rows, categories=list(range(lo, hi + 1)),
        series=[g.label for g in groups if g.label], notes=notes,
        stats={"trials": sum(g.exact.total if g.exact else len(g.trials) for g in groups),
               "missing_value": no_year, "before_not_shown": before, "future_not_shown": after,
               "outside_or_missing_not_shown": not_counted,
               "partial_year": current if lo <= current <= hi else None},
    )


# ------------------------------------------------------------------ histogram

def histogram(plan: QueryPlan, groups: list[Group]) -> AggregateResult:
    spec: NumericSpec = NUMERICS[plan.numeric_field]
    values: dict[str | None, list[tuple[float, str, Evidence]]] = {}
    missing = 0
    for g in groups:
        values[g.label] = []
        for t in g.trials:
            got = spec.extract(t)
            if got is None:
                missing += 1
            else:
                values[g.label].append((got[0], t.nct_id, got[1]))

    all_values = [v for vs in values.values() for v, _, _ in vs]
    if not all_values:
        return AggregateResult(kind="rows", notes=[f"No trials have a {plan.numeric_field.value} value."])

    bins = spec.bins or [(v, v + 1, str(int(v))) for v in range(int(min(all_values)), int(max(all_values)) + 1)]
    # Drop empty bins after the last non-empty one, so the axis doesn't run on.
    last = max(i for i, (lo, hi, _) in enumerate(bins) if any(lo <= v and (hi is None or v < hi) for v in all_values))
    bins = bins[: last + 1]

    rows = []
    for lo, hi, label in bins:
        for g in groups:
            b = _Bucket(label)
            for v, nct, ev in values[g.label]:
                if lo <= v and (hi is None or v < hi):
                    b.add(nct, ev.field, ev.excerpt)
            rows.append(_datum(label, b, g.label, bin_start=lo, bin_end=hi))

    notes = _overlap_note(groups)
    if missing:
        notes.append(f"{missing} trials have no {plan.numeric_field.value} value and are not shown.")
    return AggregateResult(
        kind="rows", rows=rows, categories=[label for _, _, label in bins],
        series=[g.label for g in groups if g.label], notes=notes,
        stats={"trials_with_value": len(all_values), "missing_value": missing, "unit": spec.unit,
               "min": min(all_values), "median": statistics.median(all_values), "max": max(all_values)},
    )


# ------------------------------------------------------------------ scatter

def scatter(plan: QueryPlan, groups: list[Group]) -> AggregateResult:
    xs, ys = NUMERICS[plan.x_field], NUMERICS[plan.y_field]
    rows, missing = [], 0
    for g in groups:
        for t in g.trials:
            gx, gy = xs.extract(t), ys.extract(t)
            if gx is None or gy is None:
                missing += 1
                continue
            rows.append(Datum(
                x=gx[0], y=gy[0], series=g.label, trial_count=1, nct_ids=[t.nct_id], label=t.title,
                citations=[Citation(nct_id=t.nct_id, field=gx[1].field, excerpt=gx[1].excerpt),
                           Citation(nct_id=t.nct_id, field=gy[1].field, excerpt=gy[1].excerpt)],
            ))
    notes = _overlap_note(groups)
    if missing:
        notes.append(f"{missing} trials are missing {plan.x_field.value} or {plan.y_field.value} and are not shown.")
    if len(rows) > SCATTER_MAX_POINTS:
        notes.append(f"Showing {SCATTER_MAX_POINTS} of {len(rows)} points.")
        rows = rows[:SCATTER_MAX_POINTS]
    return AggregateResult(
        kind="rows", rows=rows, series=[g.label for g in groups if g.label], notes=notes,
        stats={"points": len(rows), "missing_value": missing,
               "x_unit": xs.unit, "y_unit": ys.unit, "x_scale": xs.scale, "y_scale": ys.scale},
    )


# ------------------------------------------------------------------ network

def network(plan: QueryPlan, groups: list[Group]) -> AggregateResult:
    src_dim, tgt_dim = plan.network.source, plan.network.target
    src, tgt = DIMENSIONS[src_dim], DIMENSIONS[tgt_dim]
    same = src_dim == tgt_dim

    # Merge compare groups: a network has no series.
    trials, seen = [], set()
    for g in groups:
        for t in g.trials:
            if t.nct_id not in seen:
                seen.add(t.nct_id)
                trials.append((t, _ctx(g.filters)))

    nodes: dict[str, _Bucket] = {}
    node_type: dict[str, str] = {}
    edges: dict[tuple[str, str], _Bucket] = {}
    for t, ctx in trials:
        a_side = {f"{src_dim.value}:{ev.key}": ev for ev in _dedupe(src.extract(t, ctx))}
        b_side = a_side if same else {f"{tgt_dim.value}:{ev.key}": ev for ev in _dedupe(tgt.extract(t, ctx))}
        sides = [(a_side, src_dim)] if same else [(a_side, src_dim), (b_side, tgt_dim)]
        for side, dim in sides:
            for node_id, ev in side.items():
                nodes.setdefault(node_id, _Bucket(ev.label)).add(t.nct_id, ev.field, ev.excerpt, ev.label)
                node_type[node_id] = dim.value
        pairs = combinations(sorted(a_side), 2) if same else product(a_side, b_side)
        for a, b in pairs:
            ea, eb = a_side[a], b_side[b]
            edges.setdefault((a, b), _Bucket("")).add(
                t.nct_id, f"{ea.field} & {eb.field}", f"{ea.excerpt} | {eb.excerpt}")

    # Keep the biggest nodes (split evenly between the two sides for sponsor<->drug style networks).
    max_nodes = plan.limit or NETWORK_MAX_NODES
    ranked = sorted(nodes, key=lambda n: (-nodes[n].count, n))
    if same:
        kept = set(ranked[:max_nodes])
    else:
        per_side = max_nodes // 2
        kept = {n for d in (src_dim, tgt_dim) for n in [x for x in ranked if node_type[x] == d.value][:per_side]}

    min_weight = NETWORK_MIN_EDGE_WEIGHT
    kept_edges = [(k, b) for k, b in edges.items() if k[0] in kept and k[1] in kept]
    if not any(b.count >= min_weight for _, b in kept_edges):
        min_weight = 1  # small result: show every link rather than nothing
    kept_edges = [(k, b) for k, b in kept_edges if b.count >= min_weight]
    kept_edges = _strongest_links(kept_edges, NETWORK_LINKS_PER_NODE)
    connected = {n for (a, b), _ in kept_edges for n in (a, b)}

    aliases = {k: v for g in groups for k, v in g.drug_aliases.items()}
    query_keys = {}
    for dim in {src_dim, tgt_dim}:
        name = FILTER_FOR_DIMENSION.get(dim)
        values = {getattr(g.filters, name) for g in groups} if name else set()
        keys = {make_key(v) for v in values if v}
        if dim == Dimension.INTERVENTION:   # 'Keytruda' in the question matches the 'pembrolizumab' node
            keys = {aliases.get(k, k) for k in keys}
        query_keys[dim.value] = keys

    def is_match(node_id: str) -> bool:
        kind, key = node_id.split(":", 1)
        return any(f" {q} " in f" {key} " for q in query_keys.get(kind, ()))

    out_nodes = [
        Node(id=n, label=nodes[n].display, type=node_type[n], weight=nodes[n].count, is_query_match=is_match(n),
             nct_ids=nodes[n].nct_ids, citations=nodes[n].citations)
        for n in ranked if n in kept and n in connected
    ]
    out_edges = [
        Edge(source=a, target=b, weight=bk.count, nct_ids=bk.nct_ids, citations=bk.citations)
        for (a, b), bk in sorted(kept_edges, key=lambda kb: (-kb[1].count, kb[0]))
    ]

    notes = [f"Showing {len(out_nodes)} of {len(nodes)} entities and {len(out_edges)} of {len(edges)} links: "
             f"each entity's {NETWORK_LINKS_PER_NODE} strongest links, shared by at least {min_weight} "
             f"trial{'s' if min_weight > 1 else ''}."]
    if len(groups) > 1:
        notes.append("Compared groups were merged into one network.")
    if any(n.is_query_match for n in out_nodes):
        notes.append("The outlined node is what the question filtered on; it connects to most other "
                     "nodes because every trial shown includes it.")
    return AggregateResult(
        kind="network", nodes=out_nodes, edges=out_edges, notes=notes,
        stats={"trials": len(trials), "entities_total": len(nodes), "links_total": len(edges),
               "min_edge_weight": min_weight},
    )


def _strongest_links(edges: list, per_node: int) -> list:
    """Keep a link if it is among the `per_node` heaviest links of either endpoint.

    Common entities co-occur with almost everything, so a plain weight cutoff gives a
    hairball; this keeps each entity's main partners and drops incidental links.
    """
    ranked = sorted(edges, key=lambda kb: (-kb[1].count, kb[0]))
    taken: Counter = Counter()
    keep = set()
    for (a, b), _ in ranked:
        if taken[a] < per_node or taken[b] < per_node:
            keep.add((a, b))
        taken[a] += 1
        taken[b] += 1
    return [(k, bk) for k, bk in edges if k in keep]


# ------------------------------------------------------------------ table

def table(plan: QueryPlan, groups: list[Group]) -> AggregateResult:
    rows = []
    for g in groups:
        for t in g.trials:
            rows.append(TableRow(
                nct_id=t.nct_id, url=f"https://clinicaltrials.gov/study/{t.nct_id}", title=t.title,
                status=t.status, phase=DIMENSIONS[Dimension.PHASE].extract(t, Context())[0].label,
                start_date=t.start_date, lead_sponsor=t.lead_sponsor, conditions=t.conditions,
                interventions=[i.display for i in t.interventions], countries=sorted(t.countries),
                series=g.label,
            ))
    rows.sort(key=lambda r: r.start_date or "", reverse=True)  # newest first; no date last
    limit = plan.limit or TABLE_DEFAULT_LIMIT
    matched = sum(g.matched for g in groups)
    notes = [f"Showing the {min(limit, len(rows))} trials with the latest start dates "
             f"(including planned ones) of {matched} matching."] if matched > limit else []
    return AggregateResult(kind="table", table=rows[:limit], notes=notes,
                           series=[g.label for g in groups if g.label], stats={"trials": matched})


# ------------------------------------------------------------------ dispatch

def _coverage(plan: QueryPlan, groups: list[Group]) -> tuple[list[str], dict]:
    """Say how much of the matching data the result is built on."""
    notes = []
    for g in groups:
        if not g.truncated:
            continue
        who = f"[{g.label}] " if g.label else ""
        if g.exact:
            notes.append(f"{who}Counts are exact: ClinicalTrials.gov counted all {g.matched:,} matching trials. "
                         f"Trial IDs and citations list only the {len(g.trials):,} downloaded trials "
                         "with the latest start dates.")
        elif plan.intent == Intent.TRIAL_LIST or plan.chart == ChartType.TABLE:
            continue  # the table note already says it shows the latest trials of all matches
        else:
            notes.append(f"{who}Based on a sample: only the {len(g.trials):,} trials with the latest start dates "
                         f"out of {g.matched:,} matching were downloaded, so these numbers describe that "
                         "sample, not all matching trials.")
    stats = {
        "matched": sum(g.matched for g in groups),
        "downloaded": sum(len(g.trials) for g in groups),
        "sampled": any(g.truncated and not g.exact for g in groups),
        "exact_counts": any(g.exact is not None for g in groups),
    }
    return notes, stats


def aggregate(plan: QueryPlan, groups: list[Group], today: date | None = None) -> AggregateResult:
    result = _aggregate(plan, groups, today)
    notes, stats = _coverage(plan, groups)
    return result.model_copy(update={"notes": notes + result.notes, "stats": {**result.stats, **stats}})


def _aggregate(plan: QueryPlan, groups: list[Group], today: date | None = None) -> AggregateResult:
    if plan.intent == Intent.TRIAL_LIST or plan.chart == ChartType.TABLE:
        return table(plan, groups)
    chart = plan.chart
    if chart in (ChartType.BAR_CHART, ChartType.GROUPED_BAR_CHART):
        return group_count(plan, groups)
    if chart == ChartType.TIME_SERIES:
        return time_series(plan, groups, today or date.today())
    if chart == ChartType.HISTOGRAM:
        return histogram(plan, groups)
    if chart == ChartType.SCATTER_PLOT:
        return scatter(plan, groups)
    if chart == ChartType.NETWORK_GRAPH:
        return network(plan, groups)
    raise ValueError(f"no aggregation for chart type {chart}")