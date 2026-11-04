"use strict";

// ------------------------------------------------------------------ helpers

const $ = (sel) => document.querySelector(sel);
const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : String(n));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pretty = (code) => String(code).replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function trialLink(nct) {
  return el("a", { href: `https://clinicaltrials.gov/study/${encodeURIComponent(nct)}`,
                   target: "_blank", rel: "noopener", text: nct });
}

function tokens() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    surface: v("--surface"), primary: v("--text-primary"), secondary: v("--text-secondary"),
    muted: v("--muted"), grid: v("--grid"), axis: v("--axis"), border: v("--border"),
    series: [1, 2, 3, 4, 5].map((i) => v(`--series-${i}`)),
  };
}

const EXAMPLES = [
  "How has the number of pembrolizumab trials changed over time?",
  "Which countries have the most recruiting melanoma trials?",
  "Compare phases of nivolumab and atezolizumab trials",
  "Which drugs are most often combined with pembrolizumab?",
  "What is the distribution of enrollment sizes in asthma trials?",
  "Show recruiting phase 3 breast cancer trials",
];

// ------------------------------------------------------------------ state

let current = null;   // last successful response
let chart = null;     // ECharts instance

// ------------------------------------------------------------------ form

async function initForm() {
  for (const q of EXAMPLES) {
    const b = el("button", { type: "button", text: q });
    b.addEventListener("click", () => { $("#q").value = q; $("#ask").requestSubmit(); });
    $("#examples").append(b);
  }
  // Phase / status choices come from the API schema, so the UI never drifts from the backend.
  try {
    const spec = await (await fetch("/openapi.json")).json();
    const enumOf = (name) => spec.components.schemas[name]?.enum || [];
    fillChecks("#phases", "trial_phase", enumOf("Phase"));
    fillChecks("#statuses", "status", enumOf("OverallStatus"));
  } catch { /* filters stay empty; the question still works */ }

  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
  $("#rerun").addEventListener("click", rerun);
  $("#toggle-table").addEventListener("click", toggleTable);
  window.addEventListener("resize", () => chart && chart.resize());
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => current && render(current));
}

function fillChecks(sel, name, values) {
  for (const v of values) {
    $(sel).append(el("label", {}, el("input", { type: "checkbox", name, value: v }), pretty(v)));
  }
}

function requestBody() {
  const f = new FormData($("#ask"));
  const body = { query: f.get("query") };
  for (const k of ["drug_name", "condition", "sponsor", "country"]) {
    const v = (f.get(k) || "").trim();
    if (v) body[k] = v;
  }
  for (const k of ["start_year", "end_year", "max_trials"]) {
    if (f.get(k)) body[k] = Number(f.get(k));
  }
  for (const k of ["trial_phase", "status"]) {
    const vs = f.getAll(k);
    if (vs.length) body[k] = vs;
  }
  return body;
}

async function ask() {
  await call("/api/query", requestBody(), $("#q").value.trim());
}

async function rerun() {
  let plan;
  try { plan = JSON.parse($("#plan-text").value); } catch (err) {
    return setStatus(`The plan is not valid JSON: ${err.message}`, true);
  }
  const maxTrials = Number(new FormData($("#ask")).get("max_trials")) || 10000;
  await call("/api/plan", { plan, max_trials: maxTrials }, current?.question || "Edited plan");
}

async function call(url, body, question) {
  const started = performance.now();
  const tick = setInterval(() => setStatus(
    `${url === "/api/query" ? "Reading the question and fetching trials" : "Fetching trials"}... ` +
    `${Math.round((performance.now() - started) / 1000)}s`), 500);
  $("#go").disabled = $("#rerun").disabled = true;
  $("#output").classList.add("loading");   // keep the previous result visible while loading
  try {
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify(body) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(errorText(data, resp.status));
    data.question = question;
    current = data;
    render(data);
    const t = data.timings || {};
    setStatus(`Done in ${((performance.now() - started) / 1000).toFixed(1)}s` +
      (t.plan_s ? ` (interpreting ${t.plan_s.toFixed(1)}s, data ${(t.data_s || 0).toFixed(1)}s)` : ""));
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    clearInterval(tick);
    $("#go").disabled = $("#rerun").disabled = false;
    $("#output").classList.remove("loading");
  }
}

function errorText(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map((d) => `${(d.loc || []).filter((x) => x !== "body").join(".")}: ${d.msg}`).join("; ");
  }
  return `Request failed (HTTP ${status})`;
}

function setStatus(text, isError = false) {
  $("#status").textContent = text;
  $("#status").classList.toggle("error", isError);
}

// ------------------------------------------------------------------ render

function render(data) {
  $("#output").hidden = false;
  $("#title").textContent = data.question;
  renderPlan(data.plan);
  renderSources(data.sources || []);
  resetEvidence();
  $("#table-view").hidden = true;
  $("#toggle-table").textContent = "Show data table";

  const unsupported = data.status === "unsupported";
  $("#unsupported").hidden = !unsupported;
  $("#chart").hidden = unsupported;
  $("#toggle-table").hidden = unsupported;
  if (chart) { chart.dispose(); chart = null; }
  if (typeof echarts === "undefined" && data.result?.kind !== "table" && !unsupported) {
    throw new Error("The chart library could not be loaded (cdn.jsdelivr.net). Check the internet connection; " +
                    "the data table below still works.");
  }
  if (unsupported) {
    $("#subtitle").textContent = "This question can't be answered from registry data.";
    $("#unsupported").textContent = data.plan.reason || "No reason given.";
    $("#notes").replaceChildren();
    return;
  }

  const r = data.result;
  const stats = r.stats || {};
  const what = { rows: chartName(data.plan), network: "Network", table: "Trial list" }[r.kind];
  $("#subtitle").textContent = `${what} · ${fmt(stats.matched ?? 0)} matching trials` +
    (stats.sampled ? ` (sample of ${fmt(stats.downloaded)})` : "");
  $("#notes").replaceChildren(...(r.notes || []).map((n) => el("li", { text: n })));

  if (r.kind === "table") {
    $("#chart").hidden = true;
    $("#toggle-table").hidden = true;
    $("#table-view").hidden = false;
    $("#table-view").replaceChildren(trialTable(r.table));
    return;
  }
  if ((r.kind === "rows" && !r.rows.length) || (r.kind === "network" && !r.nodes.length)) {
    $("#chart").hidden = true;
    $("#toggle-table").hidden = true;
    return;
  }
  chart = echarts.init($("#chart"), null, { renderer: "svg" });
  const option = r.kind === "network" ? networkOption(r) : rowsOption(r, data.plan);
  chart.setOption(option);
  chart.on("click", (p) => showEvidence(r, p));
  $("#chart").setAttribute("aria-label", `${what} for: ${data.question}`);
}

function chartName(plan) {
  return { bar_chart: "Bar chart", grouped_bar_chart: "Grouped bar chart", time_series: "Trend over time",
           histogram: "Histogram", scatter_plot: "Scatter plot" }[plan.chart] || "Chart";
}

// ------------------------------------------------------------------ chart options

function base(t) {
  return {
    animationDuration: 300,
    textStyle: { fontFamily: getComputedStyle(document.body).fontFamily, color: t.secondary },
    tooltip: {
      backgroundColor: t.surface, borderColor: t.border, borderWidth: 1,
      textStyle: { color: t.primary, fontSize: 13 }, extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,.12);",
    },
    grid: { left: 8, right: 24, top: 36, bottom: 8, containLabel: true },
  };
}

function axisStyle(t) {
  return {
    axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false },
    axisLabel: { color: t.muted, hideOverlap: true },
    splitLine: { lineStyle: { color: t.grid, width: 1 } },
  };
}

function tipRow(color, name, value, line = false) {
  const key = line
    ? `<span style="display:inline-block;width:12px;height:2px;background:${color};vertical-align:middle;margin-right:6px"></span>`
    : `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px"></span>`;
  return `<div style="display:flex;justify-content:space-between;gap:16px">` +
    `<span>${key}<span style="opacity:.75">${esc(name)}</span></span><strong>${esc(value)}</strong></div>`;
}

/** rows -> one series per compare group, aligned to result.categories */
function seriesTable(r) {
  const names = r.series.length ? r.series : [null];
  const index = new Map(r.rows.map((row, i) => [`${row.x}\u0000${row.series}`, i]));
  return { names, at: (x, s) => index.get(`${x}\u0000${s}`) };
}

function rowsOption(r, plan) {
  const t = tokens();
  if (plan.chart === "scatter_plot") return scatterOption(r, t);
  if (plan.chart === "time_series") return lineOption(r, t);

  const { names, at } = seriesTable(r);
  const cats = r.categories.map(String);
  const histogram = plan.chart === "histogram";
  const horizontal = !histogram && (cats.length > 8 || cats.some((c) => c.length > 14));
  const single = names.length === 1;

  const series = names.map((name, si) => ({
    type: "bar", name: name ?? "Trials",
    barMaxWidth: 24, barGap: "8%", barCategoryGap: histogram ? "6%" : "30%",
    itemStyle: { color: t.series[si], borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0] },
    emphasis: { itemStyle: { opacity: 0.8 } },
    label: { show: single && cats.length <= 25, position: horizontal ? "right" : "top",
             color: t.secondary, fontSize: 12, formatter: (p) => fmt(p.value) },
    data: cats.map((c) => {
      const i = at(c, name);
      return { value: i === undefined ? 0 : r.rows[i].y, rowIndex: i };
    }),
  }));
  const catAxis = { type: "category", data: cats, ...axisStyle(t), splitLine: { show: false },
                    axisLabel: { color: t.secondary, width: 180, overflow: "truncate", interval: 0, hideOverlap: !horizontal } };
  const valAxis = { type: "value", ...axisStyle(t), name: "Trials", nameLocation: "middle",
                    nameGap: horizontal ? 28 : 44, nameTextStyle: { color: t.muted },
                    axisLabel: { color: t.muted, formatter: (v) => compact(v) } };
  if (horizontal) catAxis.inverse = true;   // biggest first, top to bottom
  if (histogram) {
    Object.assign(catAxis, { name: r.stats.unit || "", nameLocation: "middle", nameGap: 30,
                             nameTextStyle: { color: t.muted } });
    catAxis.axisLabel = { color: t.secondary, interval: 0, fontSize: 11, hideOverlap: false };
  }

  const height = horizontal ? Math.max(320, cats.length * (single ? 28 : 16 * names.length + 12) + 60) : 440;
  $("#chart").style.height = `${height}px`;

  return {
    ...base(t),
    grid: { ...base(t).grid, left: horizontal ? 8 : 24, top: single ? 24 : 44, bottom: horizontal ? 32 : 24 },
    legend: single ? undefined : { top: 0, left: 0, icon: "rect", itemWidth: 10, itemHeight: 10,
                                   textStyle: { color: t.secondary } },
    tooltip: { ...base(t).tooltip, trigger: "item",
               formatter: (p) => `<div style="margin-bottom:4px">${esc(p.name)}</div>` +
                 tipRow(p.color, p.seriesName, `${fmt(p.value)} trials`) },
    xAxis: horizontal ? valAxis : catAxis,
    yAxis: horizontal ? catAxis : valAxis,
    series,
  };
}

function lineOption(r, t) {
  $("#chart").style.height = "440px";
  const { names, at } = seriesTable(r);
  const cats = r.categories.map(String);
  const single = names.length === 1;
  const partial = r.stats.partial_year != null ? String(r.stats.partial_year) : null;
  return {
    ...base(t),
    grid: { ...base(t).grid, left: 24, top: single ? 24 : 44, right: single ? 24 : 110 },
    legend: single ? undefined : { top: 0, left: 0, icon: "path://M0,4 L14,4 L14,6 L0,6 Z", itemWidth: 14,
                                   itemHeight: 10, textStyle: { color: t.secondary } },
    tooltip: {
      ...base(t).tooltip, trigger: "axis", axisPointer: { type: "line", lineStyle: { color: t.axis } },
      formatter: (ps) => `<div style="margin-bottom:4px">${esc(ps[0].axisValue)}` +
        `${ps[0].axisValue === partial ? " (year in progress)" : ""}</div>` +
        ps.map((p) => tipRow(p.color, p.seriesName, `${fmt(p.value)} trials`, true)).join(""),
    },
    xAxis: { type: "category", data: cats, boundaryGap: false, ...axisStyle(t), splitLine: { show: false } },
    yAxis: { type: "value", ...axisStyle(t), name: "Trials started", nameLocation: "middle", nameGap: 44,
             nameTextStyle: { color: t.muted }, axisLabel: { color: t.muted, formatter: (v) => compact(v) } },
    series: names.map((name, si) => ({
      type: "line", name: name ?? "Trials", color: t.series[si],
      lineStyle: { width: 2, cap: "round", join: "round" },
      symbol: "circle", symbolSize: 8, showSymbol: cats.length <= 30,
      itemStyle: { color: t.series[si], borderColor: t.surface, borderWidth: 2 },
      areaStyle: single ? { color: t.series[si], opacity: 0.1 } : undefined,
      endLabel: { show: !single && names.length <= 4, formatter: "{a}", color: t.secondary },
      emphasis: { focus: "series" },
      data: cats.map((c) => {
        const i = at(c, name);
        const item = { value: i === undefined ? 0 : r.rows[i].y, rowIndex: i };
        if (c === partial) item.symbol = "emptyCircle";   // incomplete year: hollow marker
        return item;
      }),
    })),
  };
}

function scatterOption(r, t) {
  $("#chart").style.height = "480px";
  const names = r.series.length ? r.series : [null];
  const logX = r.stats.x_scale === "log", logY = r.stats.y_scale === "log";
  const visible = (row) => (!logX || row.x > 0) && (!logY || row.y > 0);
  const hidden = r.rows.filter((row) => !visible(row)).length;
  if (hidden) {
    $("#notes").append(el("li", { text: `${fmt(hidden)} trials with a value of 0 can't be placed on the log scale ` +
                                        "and are not shown (they are in the data table)." }));
  }
  const axis = (log, unit, gap) => ({
    type: log ? "log" : "value", name: unit, nameLocation: "middle", nameGap: gap,
    nameTextStyle: { color: t.muted }, ...axisStyle(t),
    axisLabel: { color: t.muted, formatter: (v) => compact(v) }, scale: !log,
  });
  return {
    ...base(t),
    legend: names.length === 1 ? undefined : { top: 0, left: 0, textStyle: { color: t.secondary } },
    tooltip: { ...base(t).tooltip, trigger: "item",
               formatter: (p) => {
                 const row = r.rows[p.data.rowIndex];
                 return `<div style="max-width:320px;white-space:normal;margin-bottom:4px">${esc(row.label)}</div>` +
                   tipRow(p.color, r.stats.x_unit, fmt(row.x)) + tipRow(p.color, r.stats.y_unit, fmt(row.y)) +
                   `<div style="opacity:.7;margin-top:4px">${esc(row.nct_ids[0])}</div>`;
               } },
    grid: { ...base(t).grid, left: 24, bottom: 28, top: names.length === 1 ? 16 : 40 },
    xAxis: axis(logX, r.stats.x_unit, 28),
    yAxis: axis(logY, r.stats.y_unit, 44),
    series: names.map((name, si) => ({
      type: "scatter", name: name ?? "Trials", symbolSize: 8,
      itemStyle: { color: t.series[si], borderColor: t.surface, borderWidth: 2, opacity: 0.85 },
      emphasis: { scale: 1.6 },
      data: r.rows.map((row, i) => [row, i]).filter(([row]) => row.series === name && visible(row))
        .map(([row, i]) => ({ value: [row.x, row.y], rowIndex: i })),
    })),
  };
}

function networkOption(r) {
  const t = tokens();
  $("#chart").style.height = "560px";
  const types = [...new Set(r.nodes.map((n) => n.type))];
  const maxW = Math.max(...r.nodes.map((n) => n.weight));
  const maxE = Math.max(...r.edges.map((e) => e.weight), 1);
  const labelled = new Set([...r.nodes].sort((a, b) => b.weight - a.weight).slice(0, 14).map((n) => n.id));
  return {
    ...base(t),
    legend: types.length > 1 ? { top: 0, left: 0, data: types.map(pretty), textStyle: { color: t.secondary } } : undefined,
    tooltip: { ...base(t).tooltip, trigger: "item",
               formatter: (p) => p.dataType === "edge"
                 ? `<div style="margin-bottom:4px">${esc(r.nodes[p.data.s].label)} + ${esc(r.nodes[p.data.t].label)}</div>` +
                   tipRow(t.axis, "Trials with both", fmt(p.data.weight))
                 : `<div style="margin-bottom:4px">${esc(p.data.name)}</div>` +
                   tipRow(p.color, pretty(r.nodes[p.data.nodeIndex].type), `${fmt(p.data.value)} trials`) },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 900, edgeLength: [90, 240], gravity: 0.05, friction: 0.2 },
      labelLayout: { hideOverlap: true },
      categories: types.map((ty, i) => ({ name: pretty(ty), itemStyle: { color: t.series[i] } })),
      label: { color: t.primary, fontSize: 12, position: "right" },
      emphasis: { focus: "adjacency", lineStyle: { opacity: 0.9 } },
      lineStyle: { color: t.axis, opacity: 0.55, curveness: 0.08 },
      data: r.nodes.map((n, i) => ({
        id: n.id, name: n.label, value: n.weight, nodeIndex: i, category: types.indexOf(n.type),
        symbolSize: 10 + 34 * Math.sqrt(n.weight / maxW),
        label: { show: labelled.has(n.id) },
        itemStyle: n.is_query_match ? { borderColor: t.primary, borderWidth: 2 }
                                    : { borderColor: t.surface, borderWidth: 2 },
      })),
      links: r.edges.map((e, i) => {
        const s = r.nodes.findIndex((n) => n.id === e.source), tt = r.nodes.findIndex((n) => n.id === e.target);
        return { source: e.source, target: e.target, weight: e.weight, edgeIndex: i, s, t: tt,
                 lineStyle: { width: 1 + 5 * (e.weight / maxE) } };
      }),
    }],
  };
}

function compact(v) {
  return Math.abs(v) >= 10000 ? `${+(v / 1000).toFixed(1)}K` : fmt(v);
}

// ------------------------------------------------------------------ evidence (click)

function resetEvidence() {
  $("#evidence").replaceChildren(el("p", { class: "muted",
    text: "Click a bar, point or node to list its trials and the registry fields they were counted from." }));
}

function showEvidence(r, p) {
  let item, title;
  if (r.kind === "network") {
    if (p.dataType === "edge") {
      item = r.edges[p.data.edgeIndex];
      title = `${r.nodes[p.data.s].label} + ${r.nodes[p.data.t].label}`;
    } else {
      item = r.nodes[p.data.nodeIndex];
      title = item.label;
    }
  } else {
    if (p.data?.rowIndex === undefined) return;
    item = r.rows[p.data.rowIndex];
    title = item.label || (item.series ? `${item.x} · ${item.series}` : String(item.x));   // scatter: trial title
  }
  const count = item.trial_count ?? item.weight ?? item.nct_ids.length;
  const parts = [el("p", { class: "count" }, el("strong", { text: title })),
                 el("p", { class: "count", text: `${fmt(count)} trial${count === 1 ? "" : "s"}` +
                   (item.nct_ids_complete === false ? `. IDs below are the ${fmt(item.nct_ids.length)} downloaded ones.` : "") })];
  if (item.citations?.length) {
    parts.push(el("h4", { text: "Where the value comes from" }));
    parts.push(el("ul", { class: "cites" }, ...item.citations.map((c) =>
      el("li", {}, trialLink(c.nct_id), el("span", { class: "field", text: c.field }),
         el("q", { text: c.excerpt || "(empty)" })))));
  }
  if (item.nct_ids.length) {
    const shown = item.nct_ids.slice(0, 200);
    parts.push(el("h4", { text: `Trials (${fmt(item.nct_ids.length)})` }));
    parts.push(el("div", { class: "ids" }, ...shown.map(trialLink)));
    if (item.nct_ids.length > shown.length) {
      parts.push(el("p", { class: "muted", text: `and ${fmt(item.nct_ids.length - shown.length)} more` }));
    }
  }
  $("#evidence").replaceChildren(...parts);
}

// ------------------------------------------------------------------ table views

function toggleTable() {
  const view = $("#table-view");
  view.hidden = !view.hidden;
  $("#toggle-table").textContent = view.hidden ? "Show data table" : "Hide data table";
  if (!view.hidden) view.replaceChildren(dataTable(current.result, current.plan));
}

function table(headers, rows, numeric = new Set()) {
  return el("table", {},
    el("thead", {}, el("tr", {}, ...headers.map((h, i) => el("th", { class: numeric.has(i) ? "num" : "", text: h })))),
    el("tbody", {}, ...rows.map((cells) => el("tr", {}, ...cells.map((c, i) =>
      el("td", { class: numeric.has(i) ? "num" : "" }, c instanceof Node ? c : String(c ?? "")))))));
}

function dataTable(r, plan) {
  if (r.kind === "network") {
    return table(["Entity", "Type", "Trials"],
      r.nodes.map((n) => [n.label, pretty(n.type), fmt(n.weight)]), new Set([2]));
  }
  if (plan.chart === "scatter_plot") {
    return table(["Trial", "Title", r.stats.x_unit, r.stats.y_unit],
      r.rows.map((row) => [trialLink(row.nct_ids[0]), row.label, fmt(row.x), fmt(row.y)]), new Set([2, 3]));
  }
  const multi = r.series.length > 0;
  return table(multi ? ["Value", "Group", "Trials"] : ["Value", "Trials"],
    r.rows.map((row) => multi ? [row.x, row.series, fmt(row.y)] : [row.x, fmt(row.y)]),
    new Set([multi ? 2 : 1]));
}

function trialTable(rows) {
  return table(["Trial", "Title", "Status", "Phase", "Start", "Lead sponsor", "Conditions", "Interventions", "Countries"],
    rows.map((t) => [trialLink(t.nct_id), t.title, t.status ? pretty(t.status) : "", t.phase, t.start_date || "",
                     t.lead_sponsor || "", t.conditions.join(", "), t.interventions.join(", "),
                     t.countries.join(", ")]));
}

// ------------------------------------------------------------------ plan & sources

function renderPlan(plan) {
  const f = plan.filters || {};
  const filters = Object.entries(f).filter(([, v]) => v != null && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${pretty(k)}: ${Array.isArray(v) ? v.map(pretty).join(", ") : v}`);
  const rows = [
    ["Intent", pretty(plan.intent)],
    ["Chart", plan.chart ? pretty(plan.chart) : "-"],
    plan.group_by && ["Grouped by", pretty(plan.group_by)],
    plan.numeric_field && ["Number", pretty(plan.numeric_field)],
    plan.x_field && ["Axes", `${pretty(plan.x_field)} vs ${pretty(plan.y_field)}`],
    plan.network && ["Network", `${pretty(plan.network.source)} - ${pretty(plan.network.target)}`],
    ["Filters", filters.join(" · ") || "none"],
    plan.compare?.length && ["Compared groups", plan.compare.map((g) => g.label).join(", ")],
    plan.limit && ["Limit", String(plan.limit)],
  ].filter(Boolean);
  $("#plan-summary").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  $("#assumptions").replaceChildren(...(plan.assumptions || []).map((a) => el("li", { text: a })));
  $("#plan-text").value = JSON.stringify(plan, null, 2);
}

function renderSources(sources) {
  $("#sources").replaceChildren(...sources.map((s) => el("li", {},
    el("div", { text: `${s.label ? s.label + ": " : ""}${fmt(s.matched)} trials matched, ${fmt(s.downloaded)} downloaded` +
      (s.broad_matched != null ? `. A broader text search finds ${fmt(s.broad_matched)}; ` +
        `${fmt(s.broad_matched - s.matched)} only mention the term in titles or descriptions and were left out.` : "") }),
    el("a", { href: s.request_url, target: "_blank", rel: "noopener", text: "API request" }))));
  if (!sources.length) $("#sources").append(el("li", { class: "muted", text: "No data was fetched." }));
}

initForm();
