/* ════════════════════════════════════════════════════════════════
   Sentinel · Live Observability — controller
   ────────────────────────────────────────────────────────────────
   Modules (single-file by design — no build step):
     ▸ state         — pure store + selectors
     ▸ sse           — EventSource wiring + dispatch
     ▸ intake        — left rail (alert cards)
     ▸ graph         — SVG workflow graph
     ▸ payload       — payload + transformations tab
     ▸ scoring       — gauge + breakdown + narrative tab
     ▸ architecture  — service topology tab
     ▸ inspector     — right rail (tool I/O + audit)
     ▸ timeline      — bottom event stream
     ▸ kpis          — top-bar counters
   ════════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  // ── Constants ───────────────────────────────────────────────
  const TOOLS = [
    "screening_api_lookup",
    "core_banking_get_customer",
    "get_adverse_media",
    "get_company_registry",
    "get_ubo_chain",
    "case_management_prior_cases",
    "close_alert",
  ];

  const TOOL_LABEL = {
    screening_api_lookup:        "screening_api_lookup",
    core_banking_get_customer:   "core_banking_get_customer",
    get_adverse_media:           "get_adverse_media",
    get_company_registry:        "get_company_registry",
    get_ubo_chain:               "get_ubo_chain",
    case_management_prior_cases: "case_management_prior_cases",
    close_alert:                 "close_alert",
  };

  const TOOL_DESCRIPTIONS = {
    screening_api_lookup:        "Fetches the alert from DynamoDB and queries the 66k-row sanctions.db for the matched entity.",
    core_banking_get_customer:   "Pulls full KYC + last 10 transactions; counts large (>5L) and international flows.",
    get_adverse_media:           "Scans adverse_media_records for negative press matching the person_name.",
    get_company_registry:        "Looks for corporate ties — matches either company_name or director person_name.",
    get_ubo_chain:               "Walks the Ultimate Beneficial Owner chain for the sanctioned entity.",
    case_management_prior_cases: "Fuzzy lookup against prior_cases.json (difflib ≥ 0.80).",
    close_alert:                 "ALWAYS BLOCKED by the hook layer — only humans may dispose under PMLA 2002 / RBI KYC Master Direction 2025.",
  };

  // ── Layout coordinates for the workflow graph ───────────────
  // SVG viewBox 1300×780, all nodes substantially larger and the
  // left tool column no longer clips off the viewBox edge.
  // Phase / root / verdict: w 300–340 × h 104 · Tools: 280 × 88 · Rules: 300 × 80.
  // Tools arranged as a 3×2 grid; close_alert sits below, centered, to
  // visually separate the always-blocked tool from the evidence tools.
  const GRAPH_NODES = {
    orchestrator: { x: 650,  y:  70, w: 340, h: 104, label: "HybridOrchestrator", sub: "agent.py · 3-phase pipeline", kind: "root" },
    phase1:       { x: 220,  y: 220, w: 300, h: 104, label: "Phase 1",            sub: "LLM tool decisioning",        kind: "phase", num: "1" },
    phase2:       { x: 740,  y: 220, w: 300, h: 104, label: "Phase 2",            sub: "Rule engine (deterministic)", kind: "phase", num: "2" },
    phase3:       { x: 1150, y: 220, w: 300, h: 104, label: "Phase 3",            sub: "LLM narrative",               kind: "phase", num: "3" },
    // Phase 1 tool nodes — 3 rows × 2 columns
    screening_api_lookup:        { x: 150, y: 360, w: 280, h: 88, label: "screening_api_lookup",        sub: "alert + 66k sanctions.db", kind: "tool", phase: 1 },
    core_banking_get_customer:   { x: 440, y: 360, w: 280, h: 88, label: "core_banking_get_customer",   sub: "KYC + last 10 txns",       kind: "tool", phase: 1 },
    get_adverse_media:           { x: 150, y: 460, w: 280, h: 88, label: "get_adverse_media",           sub: "negative press",            kind: "tool", phase: 1 },
    get_company_registry:        { x: 440, y: 460, w: 280, h: 88, label: "get_company_registry",        sub: "corporate ties",            kind: "tool", phase: 1 },
    get_ubo_chain:               { x: 150, y: 560, w: 280, h: 88, label: "get_ubo_chain",               sub: "UBO walk",                  kind: "tool", phase: 1 },
    case_management_prior_cases: { x: 440, y: 560, w: 280, h: 88, label: "case_management_prior_cases", sub: "prior_cases.json",          kind: "tool", phase: 1 },
    // Centered as a banner under the tool grid.
    // Grid bounding box spans x∈[10, 580] (col L center 150, col R center 440, w=280).
    // x=295 = midpoint → close_alert with w=520 spans 35–555, leaving equal 25-unit
    // margins to both grid edges. Reads as one centered row below the 3×2 grid
    // rather than another tile floating between the two columns.
    close_alert:                 { x: 295, y: 692, w: 520, h: 88, label: "close_alert",                 sub: "ALWAYS BLOCKED · regulatory safeguard", kind: "tool", phase: 1, blocked: true },
    // Phase 2 sub-nodes (centered column)
    p2_context:    { x: 740, y: 376, w: 300, h: 80, label: "context_score",     sub: "+adverse · ubo · txn · reg", kind: "rule" },
    p2_confidence: { x: 740, y: 470, w: 300, h: 80, label: "confidence_adjust", sub: "+escalation · −clearance",   kind: "rule" },
    p2_final:      { x: 740, y: 564, w: 300, h: 80, label: "final_risk_score",  sub: "base + context + adj",        kind: "rule" },
    p2_verdict:    { x: 740, y: 680, w: 340, h: 104, label: "verdict",           sub: "≥0.85 TM · ≥0.65 UC",        kind: "verdict" },
    // Phase 3
    p3_narrative:  { x: 1150, y: 460, w: 300, h: 120, label: "narrative",       sub: "Groq · plain English",        kind: "narrative" },
  };

  const GRAPH_EDGES = [
    // root → phases
    ["orchestrator", "phase1"],
    ["orchestrator", "phase2"],
    ["orchestrator", "phase3"],
    // phase1 → tools
    ["phase1", "screening_api_lookup"],
    ["phase1", "core_banking_get_customer"],
    ["phase1", "get_adverse_media"],
    ["phase1", "get_company_registry"],
    ["phase1", "get_ubo_chain"],
    ["phase1", "case_management_prior_cases"],
    ["phase1", "close_alert"],
    // phase2 → rule sub-nodes
    ["phase2", "p2_context"],
    ["phase2", "p2_confidence"],
    ["phase2", "p2_final"],
    ["p2_final", "p2_verdict"],
    // phase3 → narrative
    ["phase3", "p3_narrative"],
  ];

  // ── State ───────────────────────────────────────────────────
  const state = {
    alerts: [],                     // [{alert_id, customer_name, ...}]
    total: 0,
    selectedAlertId: null,
    activeTab: "graph",

    es: null,
    streamState: "idle",            // idle | running | done | error
    paused: false,

    // KPIs
    processed: 0,
    toolsFired: 0,
    blocks: 0,
    verdictCounts: { TRUE_MATCH: 0, UNCERTAIN: 0, FALSE_POSITIVE: 0 },
    alertDurations: [],

    // Per-alert tracking
    runs: new Map(),                // alert_id -> { events: [], tools: {tool: state}, scores, narrative, worksheet, startedAt, finishedAt }
    selectedNodeId: null,
    selectedTool: null,

    // Timeline
    timelineFilter: "all",
    timelineSearch: "",
    timelineRows: [],
  };

  // ── Timeline event icons (SOC-style) ────────────────────────
  const TL_ICONS = {
    alert_start:       `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9"  x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    alert_complete:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`,
    alert_error:       `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9"  x2="9" y2="15"/><line x1="9"  y1="9"  x2="15" y2="15"/></svg>`,
    phase_start:       `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`,
    phase_complete:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
    tool_call_start:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
    tool_call_complete:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
    tool_call_failed:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    close_attempt:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`,
    close_blocked:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`,
    verdict:           `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>`,
    batch_start:       `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    batch_complete:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>`,
    default:           `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/></svg>`,
  };

  // ── DOM cache ───────────────────────────────────────────────
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const els = {
    streamPill:      $("#stream-pill"),
    streamStatus:    $("#stream-status"),
    runBtn:          $("#run-btn"),
    intakeStream:    $("#intake-stream"),
    intakeCount:     $("#intake-count"),
    tabs:            $("#tabs"),
    views:           $$(".view"),

    // KPIs
    kpiProcessed: $("#kpi-processed"),
    kpiTotal:     $("#kpi-total"),
    kpiTools:     $("#kpi-tools"),
    kpiBlocks:    $("#kpi-blocks"),
    kpiLatency:   $("#kpi-latency"),
    kpiTM:        $("#kpi-tm"),
    kpiUC:        $("#kpi-uc"),
    kpiFP:        $("#kpi-fp"),
    trendProcessed: $("#trend-processed"),
    trendTools:     $("#trend-tools"),
    trendBlocks:    $("#trend-blocks"),
    trendLatency:   $("#trend-latency"),
    trendTM:        $("#trend-tm"),
    trendUC:        $("#trend-uc"),
    trendFP:        $("#trend-fp"),

    // Graph
    graphCurrent: $("#graph-current"),
    graphMeta:    $("#graph-meta"),
    nodesG:       $("#nodes"),
    edgesG:       $("#edges"),

    // Payload
    payloadCurrent: $("#payload-current"),
    payloadStages:  $("#payload-stages"),
    payloadRaw:     $("#payload-raw"),
    payloadParsed:  $("#payload-parsed"),
    payloadRawSize: $("#payload-raw-size"),
    payloadParsedSize: $("#payload-parsed-size"),

    // Scoring
    scoringCurrent:     $("#scoring-current"),
    scoringVerdictMeta: $("#scoring-verdict-meta"),
    gaugeFill:          $("#gauge-fill"),
    gaugeValue:         $("#gauge-value"),
    gaugeVerdict:       $("#gauge-verdict"),
    barBase:    $("#bar-base"),
    barContext: $("#bar-context"),
    barConf:    $("#bar-conf"),
    barFinal:   $("#bar-final"),
    valBase:    $("#val-base"),
    valContext: $("#val-context"),
    valConf:    $("#val-conf"),
    valFinal:   $("#val-final"),
    explainGrid:    $("#explain-grid"),
    narrativeBody:  $("#narrative-body"),
    narrativeModel: $("#narrative-model"),

    // Architecture
    archEdges:    $("#arch-edges"),
    archNodes:    $("#arch-nodes"),
    archServices: $("#arch-services"),
    archThroughput: $("#arch-throughput"),

    // Inspector
    inspector:    $("#inspector"),
    inspectorSub: $("#inspector-sub"),

    // Timeline
    timelineStream: $("#timeline-stream"),
    timelineSearch: $("#timeline-search"),
    timelinePause:  $("#timeline-pause"),
    timelineClear:  $("#timeline-clear"),
    timelineFilters: $("#timeline-filters"),
  };

  // ════════════════════════════════════════════════════════════
  // UTILITIES
  // ════════════════════════════════════════════════════════════

  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const fmt = (n, p = 3) => {
    if (n === null || n === undefined || n === "") return "—";
    const x = Number(n);
    return Number.isFinite(x) ? x.toFixed(p) : String(n);
  };

  const pctOf = (a) => {
    const raw = Number(a?.match_score ?? a?.confidence ?? 0);
    if (!Number.isFinite(raw)) return 0;
    const v = raw <= 1 ? raw * 100 : raw;
    return Math.max(0, Math.min(100, Math.round(v)));
  };

  const riskOf = (a) => {
    const p = pctOf(a);
    return p >= 75 ? "high" : p >= 50 ? "mid" : "low";
  };

  const now = () => {
    const d = new Date();
    return d.toLocaleTimeString("en-GB", { hour12: false }) + "." +
      String(d.getMilliseconds()).padStart(3, "0");
  };

  // Lightweight syntax highlighter for JSON strings
  const highlightJson = (str) => {
    if (!str) return "";
    return esc(str)
      .replace(/(&quot;[^&]*?&quot;)(\s*:)/g, '<span class="k">$1</span>$2')
      .replace(/: (&quot;[^&]*?&quot;)/g, ': <span class="s">$1</span>')
      .replace(/: (true|false|null)\b/g, ': <span class="b">$1</span>')
      .replace(/: (-?\d+\.?\d*)/g, ': <span class="n">$1</span>')
      .replace(/(\/\/.*)/g, '<span class="l">$1</span>');
  };

  // ════════════════════════════════════════════════════════════
  // INIT
  // ════════════════════════════════════════════════════════════

  document.addEventListener("DOMContentLoaded", async () => {
    bindUI();
    buildGraph();
    buildArchitecture();
    setStream("idle", "idle");
    await loadAlerts();
  });

  function bindUI() {
    els.runBtn.addEventListener("click", startBatch);

    // Tabs
    els.tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      const t = btn.dataset.tab;
      if (!t) return;
      state.activeTab = t;
      $$(".tab", els.tabs).forEach((b) => b.classList.toggle("active", b === btn));
      els.views.forEach((v) => v.classList.toggle("view-active", v.id === `view-${t}`));
    });

    // Timeline controls
    els.timelinePause.addEventListener("click", () => {
      state.paused = !state.paused;
      els.timelinePause.classList.toggle("paused", state.paused);
    });
    els.timelineClear.addEventListener("click", () => {
      state.timelineRows = [];
      els.timelineStream.innerHTML = "";
    });
    els.timelineSearch.addEventListener("input", (e) => {
      state.timelineSearch = e.target.value.trim().toLowerCase();
      applyTimelineFilter();
    });
    els.timelineFilters.addEventListener("click", (e) => {
      const b = e.target.closest(".filter-chip");
      if (!b) return;
      $$(".filter-chip", els.timelineFilters).forEach((x) => x.classList.toggle("active", x === b));
      state.timelineFilter = b.dataset.filter;
      applyTimelineFilter();
    });
  }

  // ════════════════════════════════════════════════════════════
  // STREAM STATE PILL
  // ════════════════════════════════════════════════════════════

  function setStream(stateName, text) {
    state.streamState = stateName;
    els.streamPill.dataset.state = stateName;
    els.streamStatus.textContent = text;
  }

  // ════════════════════════════════════════════════════════════
  // ALERT INTAKE (left rail)
  // ════════════════════════════════════════════════════════════

  async function loadAlerts() {
    try {
      const r = await fetch("/api/alerts");
      const data = await r.json();
      if (data && data.error) throw new Error(data.error);
      state.alerts = Array.isArray(data) ? data : [];
      state.total = state.alerts.length;
      els.kpiTotal.textContent = String(state.total);
      els.intakeCount.textContent = `${state.total} alerts`;
      renderIntake();
      setStream("idle", state.total ? "ready" : "no alerts");
    } catch (e) {
      console.error(e);
      els.intakeStream.innerHTML = `<div class="intake-empty mono">load failed: ${esc(e.message)}</div>`;
      setStream("error", "error");
    }
  }

  function renderIntake() {
    if (!state.alerts.length) {
      els.intakeStream.innerHTML = `<div class="intake-empty mono">No PENDING alerts.<br>Run seed_data.py first.</div>`;
      return;
    }
    els.intakeStream.innerHTML = "";
    state.alerts.forEach((a) => {
      const card = document.createElement("div");
      card.className = "intake-card";
      card.dataset.alertId = a.alert_id;
      const risk = riskOf(a);
      const score = pctOf(a);
      card.innerHTML = `
        <div class="intake-row">
          <span class="intake-id">${esc((a.alert_id || "").slice(0, 18))}</span>
          <span class="intake-risk-pill risk-${risk}">${risk.toUpperCase()}</span>
        </div>
        <div class="intake-name">${esc(a.customer_name || "Unknown")}</div>
        <div class="intake-entity">→ ${esc(a.matched_entity || "—")}</div>
        <div class="intake-meta">
          <span class="intake-meta-cell">${esc(a.customer_id || "—")}</span>
          <span class="intake-meta-cell">${esc(a.source_list || a.nationality || "")}</span>
          <span class="intake-meta-cell" style="margin-left:auto">${score}%</span>
        </div>
        <div class="intake-score-bar"><div class="intake-score-fill" style="width:${score}%"></div></div>
      `;
      card.addEventListener("click", () => selectAlert(a.alert_id));
      els.intakeStream.appendChild(card);
    });
  }

  function selectAlert(alertId) {
    state.selectedAlertId = alertId;
    $$(".intake-card", els.intakeStream).forEach((c) =>
      c.classList.toggle("selected", c.dataset.alertId === alertId)
    );
    refreshGraphForAlert(alertId);
    refreshPayloadForAlert(alertId);
    refreshScoringForAlert(alertId);
    pushInspectorAlert(alertId);
  }

  function findAlert(alertId) {
    return state.alerts.find((a) => a.alert_id === alertId);
  }

  function ensureRun(alertId) {
    if (!state.runs.has(alertId)) {
      state.runs.set(alertId, {
        events: [],
        tools: Object.fromEntries(TOOLS.map((t) => [t, { state: "pending" }])),
        scores: null,
        narrative: "",
        worksheet: null,
        startedAt: null,
        finishedAt: null,
        phases: { 1: "pending", 2: "pending", 3: "pending" },
      });
    }
    return state.runs.get(alertId);
  }

  // ════════════════════════════════════════════════════════════
  // KPIs
  // ════════════════════════════════════════════════════════════

  // ── Animated counter ─────────────────────────────────────
  function tweenNumber(el, to, opts = {}) {
    if (!el) return;
    const from = parseFloat(el.dataset.val || "0") || 0;
    if (from === to) return;
    el.dataset.val = String(to);
    pulseValue(el);
    const start = performance.now();
    const dur = opts.duration || 420;
    const decimals = opts.decimals || 0;
    function tick(now) {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      const v = from + (to - from) * eased;
      el.textContent = decimals ? v.toFixed(decimals) : String(Math.round(v));
      if (t < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  function pulseValue(el) {
    if (!el) return;
    el.classList.remove("pulse");
    void el.offsetWidth; // restart animation
    el.classList.add("pulse");
  }
  function setTrend(el, pct, color) {
    if (!el) return;
    el.style.width = Math.max(0, Math.min(100, pct)) + "%";
    if (color) {
      el.classList.remove("green", "amber", "red");
      if (color !== "blue") el.classList.add(color);
    }
  }

  function updateKPIs() {
    tweenNumber(els.kpiProcessed, state.processed);
    els.kpiTotal.textContent = String(state.total);
    tweenNumber(els.kpiTools, state.toolsFired);
    tweenNumber(els.kpiBlocks, state.blocks);
    tweenNumber(els.kpiTM, state.verdictCounts.TRUE_MATCH);
    tweenNumber(els.kpiUC, state.verdictCounts.UNCERTAIN);
    tweenNumber(els.kpiFP, state.verdictCounts.FALSE_POSITIVE);

    if (state.alertDurations.length) {
      const avg = state.alertDurations.reduce((a,b)=>a+b,0) / state.alertDurations.length;
      const sec = avg / 1000;
      tweenNumber(els.kpiLatency, sec, { decimals: 1 });
      setTrend(els.trendLatency, Math.min(100, (sec / 15) * 100));
    } else {
      els.kpiLatency.textContent = "—";
      setTrend(els.trendLatency, 0);
    }

    // Trend bars (each scaled differently)
    setTrend(els.trendProcessed, state.total ? (state.processed / state.total) * 100 : 0);
    setTrend(els.trendTools,     Math.min(100, (state.toolsFired / Math.max(state.total * 8, 1)) * 100));
    setTrend(els.trendBlocks,    Math.min(100, (state.blocks     / Math.max(state.total * 3, 1)) * 100));
    setTrend(els.trendTM,        state.total ? (state.verdictCounts.TRUE_MATCH      / state.total) * 100 : 0);
    setTrend(els.trendUC,        state.total ? (state.verdictCounts.UNCERTAIN       / state.total) * 100 : 0);
    setTrend(els.trendFP,        state.total ? (state.verdictCounts.FALSE_POSITIVE  / state.total) * 100 : 0);
  }

  // ════════════════════════════════════════════════════════════
  // SSE — open the stream, dispatch events
  // ════════════════════════════════════════════════════════════

  function startBatch() {
    if (state.es) return;
    if (!state.alerts.length) return;

    // Reset
    state.processed = 0; state.toolsFired = 0; state.blocks = 0;
    state.verdictCounts = { TRUE_MATCH: 0, UNCERTAIN: 0, FALSE_POSITIVE: 0 };
    state.alertDurations = [];
    state.runs.clear();
    updateKPIs();

    els.runBtn.disabled = true;
    els.runBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg> <span>Running…</span>`;
    setStream("running", "connecting…");

    // Reset intake cards
    $$(".intake-card", els.intakeStream).forEach((c) => {
      c.classList.remove("processing", "complete-tm", "complete-uc", "complete-fp", "selected");
    });

    state.es = new EventSource("/api/run-batch");
    state.es.onmessage = (m) => {
      try { handleEvent(JSON.parse(m.data)); }
      catch (e) { console.error("bad SSE payload", e); }
    };
    state.es.onerror = () => {
      if (!state.es) return;
      if (state.es.readyState === EventSource.CLOSED) finishBatch("connection closed");
      else finishBatch("connection lost");
    };
  }

  function finishBatch(status) {
    if (state.es) { state.es.close(); state.es = null; }
    els.runBtn.disabled = false;
    els.runBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg> <span>Run Batch</span>`;
    setStream(state.streamState === "error" ? "error" : "done", status || "complete");
  }

  function handleEvent(ev) {
    pushTimeline(ev);
    switch (ev.type) {
      case "batch_start":
        if (ev.count != null) {
          state.total = ev.count;
          updateKPIs();
        }
        setStream("running", `processing 0 / ${state.total}`);
        break;
      case "alert_start":      onAlertStart(ev); break;
      case "phase_1_start":    onPhaseStart(ev, 1); break;
      case "phase_1_complete": onPhaseComplete(ev, 1); break;
      case "phase_2_start":    onPhaseStart(ev, 2); break;
      case "phase_2_complete": onPhase2Complete(ev); break;
      case "phase_3_start":    onPhaseStart(ev, 3); break;
      case "phase_3_complete": onPhase3Complete(ev); break;
      case "tool_call_start":  onToolStart(ev); break;
      case "tool_call_complete": onToolComplete(ev); break;
      case "close_attempt":    onCloseAttempt(ev); break;
      case "close_blocked":    onCloseBlocked(ev); break;
      case "alert_complete":   onAlertComplete(ev); break;
      case "alert_error":      onAlertError(ev); break;
      case "batch_complete":
        setStream("done", "batch complete");
        finishBatch("batch complete");
        break;
      case "batch_error":
        setStream("error", `batch error: ${ev.error || "?"}`);
        finishBatch();
        break;
    }
  }

  function onAlertStart(ev) {
    const run = ensureRun(ev.alert_id);
    run.startedAt = performance.now();
    selectAlert(ev.alert_id);
    const card = $$(".intake-card", els.intakeStream).find((c) => c.dataset.alertId === ev.alert_id);
    if (card) card.classList.add("processing");
    setStream("running", `processing ${state.processed + 1} / ${state.total}`);
  }

  function onPhaseStart(ev, n) {
    const run = ensureRun(ev.alert_id);
    run.phases[n] = "active";
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
    updatePayloadStage(ev.alert_id);
  }

  function onPhaseComplete(ev, n) {
    const run = ensureRun(ev.alert_id);
    run.phases[n] = "done";
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
    updatePayloadStage(ev.alert_id);
  }

  function onPhase2Complete(ev) {
    const run = ensureRun(ev.alert_id);
    run.phases[2] = "done";
    run.scores = {
      base:   ev.base_score,
      final:  ev.final_score,
      verdict: ev.verdict,
      confidence_pct: ev.confidence_pct,
    };
    if (ev.alert_id === state.selectedAlertId) {
      refreshGraphForAlert(ev.alert_id);
      refreshScoringForAlert(ev.alert_id);
    }
    updatePayloadStage(ev.alert_id);
  }

  function onPhase3Complete(ev) {
    const run = ensureRun(ev.alert_id);
    run.phases[3] = "done";
    run.narrative = ev.narrative || "";
    if (ev.alert_id === state.selectedAlertId) {
      refreshGraphForAlert(ev.alert_id);
      els.narrativeBody.textContent = run.narrative.trim() || "(empty)";
      els.narrativeBody.classList.toggle("empty", !run.narrative.trim());
    }
    updatePayloadStage(ev.alert_id);
  }

  function onToolStart(ev) {
    if (!ev.tool) return;
    const run = ensureRun(ev.alert_id);
    run.tools[ev.tool] = { state: "active", startedAt: performance.now(), source: ev.source };
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
  }

  function onToolComplete(ev) {
    if (!ev.tool) return;
    const run = ensureRun(ev.alert_id);
    const prior = run.tools[ev.tool] || {};
    const dur = prior.startedAt ? performance.now() - prior.startedAt : null;
    run.tools[ev.tool] = {
      ...prior,
      state: ev.blocked ? "blocked" : (ev.ok === false ? "blocked" : "done"),
      duration: dur,
      source: ev.source,
      ok: ev.ok,
      blocked: ev.blocked,
    };
    state.toolsFired++;
    updateKPIs();
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
  }

  function onCloseAttempt(ev) {
    const run = ensureRun(ev.alert_id);
    run.tools.close_alert = { ...(run.tools.close_alert || {}), state: "active" };
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
  }

  function onCloseBlocked(ev) {
    state.blocks++;
    const run = ensureRun(ev.alert_id);
    run.tools.close_alert = { state: "blocked", reason: ev.reason, disposition: ev.disposition };
    updateKPIs();
    if (ev.alert_id === state.selectedAlertId) refreshGraphForAlert(ev.alert_id);
  }

  function onAlertComplete(ev) {
    const run = ensureRun(ev.alert_id);
    run.worksheet = ev.worksheet || null;
    run.finishedAt = performance.now();
    if (run.startedAt) state.alertDurations.push(run.finishedAt - run.startedAt);

    const verdict = (run.worksheet?.recommendation || "").toUpperCase();
    if (verdict === "TRUE_MATCH" || verdict === "UNCERTAIN" || verdict === "FALSE_POSITIVE") {
      state.verdictCounts[verdict] = (state.verdictCounts[verdict] || 0) + 1;
    }
    state.processed++;
    updateKPIs();

    const klass =
      verdict === "TRUE_MATCH"     ? "complete-tm" :
      verdict === "UNCERTAIN"      ? "complete-uc" :
      verdict === "FALSE_POSITIVE" ? "complete-fp" : "complete-fp";
    const card = $$(".intake-card", els.intakeStream).find((c) => c.dataset.alertId === ev.alert_id);
    if (card) {
      card.classList.remove("processing");
      card.classList.add(klass);
    }
    if (ev.alert_id === state.selectedAlertId) {
      refreshGraphForAlert(ev.alert_id);
      refreshPayloadForAlert(ev.alert_id);
      refreshScoringForAlert(ev.alert_id);
    }
    setStream("running", `processed ${state.processed} / ${state.total}`);
  }

  function onAlertError(ev) {
    const run = ensureRun(ev.alert_id);
    run.error = ev.error;
    state.processed++;
    updateKPIs();
    const card = $$(".intake-card", els.intakeStream).find((c) => c.dataset.alertId === ev.alert_id);
    if (card) card.classList.remove("processing");
  }

  // ════════════════════════════════════════════════════════════
  // WORKFLOW GRAPH (SVG)
  // ════════════════════════════════════════════════════════════

  function buildGraph() {
    // Build edges first so nodes overlap them
    const fragE = document.createDocumentFragment();
    GRAPH_EDGES.forEach(([a, b], i) => {
      const path = elSVG("path");
      path.classList.add("gedge");
      path.dataset.from = a;
      path.dataset.to = b;
      path.dataset.state = "pending";
      path.setAttribute("d", edgePath(a, b));
      fragE.appendChild(path);
    });
    els.edgesG.appendChild(fragE);

    // Nodes
    const fragN = document.createDocumentFragment();
    Object.entries(GRAPH_NODES).forEach(([id, n]) => {
      const g = elSVG("g");
      g.classList.add("gnode");
      g.dataset.id = id;
      g.dataset.state = "pending";
      if (n.blocked) g.dataset.staticBlock = "true";
      g.setAttribute("transform", `translate(${n.x - n.w/2}, ${n.y - n.h/2})`);

      const rect = elSVG("rect");
      rect.classList.add("node-bg");
      rect.setAttribute("width", n.w);
      rect.setAttribute("height", n.h);
      rect.setAttribute("rx", 10);
      g.appendChild(rect);

      // Banner tools (close_alert) get a lock icon on the left and a
      // policy badge on the right — labels shift to make room.
      const labelX = n.blocked ? 78 : 22;
      const subX   = n.blocked ? 78 : 22;

      const label = elSVG("text");
      label.classList.add("node-label");
      label.setAttribute("x", labelX);
      label.setAttribute("y", 38);
      label.textContent = n.label;
      g.appendChild(label);

      const sub = elSVG("text");
      sub.classList.add("node-sub");
      sub.setAttribute("x", subX);
      sub.setAttribute("y", n.h - 18);
      sub.textContent = n.sub;
      g.appendChild(sub);

      if (n.blocked) {
        // ── Lock icon (left) ─────────────────────────────────
        const lockG = elSVG("g");
        lockG.classList.add("lock-icon");
        lockG.setAttribute("transform", `translate(28, ${(n.h - 26) / 2})`);
        const shackle = elSVG("path");
        shackle.setAttribute("d", "M5 12 V8 a5 5 0 0 1 10 0 V12");
        shackle.setAttribute("fill", "none");
        shackle.setAttribute("stroke-width", "2.5");
        shackle.setAttribute("stroke-linecap", "round");
        lockG.appendChild(shackle);
        const body = elSVG("rect");
        body.setAttribute("x", "2");
        body.setAttribute("y", "12");
        body.setAttribute("width", "16");
        body.setAttribute("height", "14");
        body.setAttribute("rx", "2.5");
        lockG.appendChild(body);
        const keyhole = elSVG("circle");
        keyhole.setAttribute("cx", "10");
        keyhole.setAttribute("cy", "19");
        keyhole.setAttribute("r", "1.5");
        keyhole.classList.add("lock-keyhole");
        lockG.appendChild(keyhole);
        g.appendChild(lockG);

        // ── Policy badge (right) ─────────────────────────────
        const tagW = 168, tagH = 26;
        const tagG = elSVG("g");
        tagG.classList.add("policy-tag");
        tagG.setAttribute("transform", `translate(${n.w - tagW - 22}, ${(n.h - tagH) / 2})`);
        const tagRect = elSVG("rect");
        tagRect.setAttribute("width", tagW);
        tagRect.setAttribute("height", tagH);
        tagRect.setAttribute("rx", tagH / 2);
        tagG.appendChild(tagRect);
        const tagText = elSVG("text");
        tagText.setAttribute("x", tagW / 2);
        tagText.setAttribute("y", tagH / 2 + 4);
        tagText.setAttribute("text-anchor", "middle");
        tagText.textContent = "POLICY BLOCK · PMLA / RBI";
        tagG.appendChild(tagText);
        g.appendChild(tagG);

        // ── Diagonal hatch overlay (subtle "do not enter" pattern) ──
        // Inserted RIGHT AFTER node-bg so it paints over the base fill
        // but under labels / icons / policy tag.
        const hatch = elSVG("rect");
        hatch.classList.add("hatch-overlay");
        hatch.setAttribute("width", n.w);
        hatch.setAttribute("height", n.h);
        hatch.setAttribute("rx", 10);
        hatch.setAttribute("fill", "url(#hatch-block)");
        hatch.setAttribute("pointer-events", "none");
        g.insertBefore(hatch, label);
      }

      if (n.kind === "phase") {
        const cap = elSVG("rect");
        cap.classList.add("phase-cap");
        cap.setAttribute("x", n.w - 52);
        cap.setAttribute("y", 16);
        cap.setAttribute("width", 36);
        cap.setAttribute("height", 36);
        cap.setAttribute("rx", 9);
        g.appendChild(cap);
        const num = elSVG("text");
        num.classList.add("node-label");
        num.setAttribute("x", n.w - 34);
        num.setAttribute("y", 41);
        num.setAttribute("text-anchor", "middle");
        num.setAttribute("font-weight", "700");
        num.setAttribute("font-size", "19");
        num.textContent = n.num;
        g.appendChild(num);
      }

      // duration badge (bottom-right corner)
      const dur = elSVG("text");
      dur.classList.add("duration-badge");
      dur.setAttribute("x", n.w - 14);
      dur.setAttribute("y", n.h - 12);
      dur.setAttribute("text-anchor", "end");
      g.appendChild(dur);

      g.addEventListener("click", () => onGraphNodeClick(id));
      fragN.appendChild(g);
    });
    els.nodesG.appendChild(fragN);
  }

  function elSVG(tag) { return document.createElementNS("http://www.w3.org/2000/svg", tag); }

  function edgePath(fromId, toId) {
    const a = GRAPH_NODES[fromId];
    const b = GRAPH_NODES[toId];
    if (!a || !b) return "";
    const sx = a.x, sy = a.y + a.h/2;
    const tx = b.x, ty = b.y - b.h/2;
    const dy = ty - sy;
    const cy = sy + dy / 2;
    return `M ${sx} ${sy} C ${sx} ${cy} ${tx} ${cy} ${tx} ${ty}`;
  }

  function refreshGraphForAlert(alertId) {
    const run = state.runs.get(alertId);
    const a = findAlert(alertId);
    els.graphCurrent.textContent = a
      ? `${alertId} · ${a.customer_name || ""}`
      : (alertId || "no alert selected");

    if (run) {
      const completedTools = Object.values(run.tools).filter((t) => t.state === "done").length;
      els.graphMeta.textContent = `tools ${completedTools}/${TOOLS.length} · phase ${
        run.phases[3] === "done" ? "3✓" :
        run.phases[2] === "done" ? "3" :
        run.phases[1] === "done" ? "2" : "1"
      }`;
    } else {
      els.graphMeta.textContent = "—";
    }

    // Update node states
    $$(".gnode", els.nodesG).forEach((n) => {
      const id = n.dataset.id;
      const node = GRAPH_NODES[id];
      let s = "pending";
      let label = "";

      if (id === "orchestrator" && run) s = "active";
      if (id.startsWith("phase")) {
        const num = parseInt(id.slice(-1), 10);
        s = run?.phases?.[num] || "pending";
      }
      if (node.kind === "tool" && run) {
        const t = run.tools[id];
        if (t) {
          s = t.state;
          if (t.duration) label = `${(t.duration).toFixed(0)}ms`;
        }
      }
      if (id.startsWith("p2_") && run) {
        if (run.scores) s = "done";
        else if (run.phases[2] === "active") s = "active";
      }
      if (id === "p3_narrative" && run) {
        if (run.narrative) s = "done";
        else if (run.phases[3] === "active") s = "active";
      }

      n.dataset.state = s;
      n.classList.toggle("selected", state.selectedNodeId === id);

      const durEl = n.querySelector(".duration-badge");
      if (durEl) durEl.textContent = label;
    });

    // Update edge states
    $$(".gedge", els.edgesG).forEach((e) => {
      const from = e.dataset.from, to = e.dataset.to;
      const fromN = els.nodesG.querySelector(`.gnode[data-id="${from}"]`);
      const toN   = els.nodesG.querySelector(`.gnode[data-id="${to}"]`);
      const fs = fromN?.dataset.state, ts = toN?.dataset.state;
      let s = "pending";
      if (fs === "done" && (ts === "active" || ts === "done" || ts === "blocked")) s = ts === "active" ? "active" : "done";
      else if (fs === "active" && ts !== "pending") s = "active";
      else if (fs === "done" && ts === "pending") s = "pending";
      e.dataset.state = s;
    });
  }

  function onGraphNodeClick(id) {
    state.selectedNodeId = id;
    $$(".gnode", els.nodesG).forEach((n) =>
      n.classList.toggle("selected", n.dataset.id === id)
    );

    const node = GRAPH_NODES[id];
    if (!node) return;

    if (node.kind === "tool") {
      showInspectorTool(state.selectedAlertId, id);
    } else if (node.kind === "phase") {
      showInspectorPhase(state.selectedAlertId, parseInt(id.slice(-1), 10));
    } else if (node.kind === "rule" || node.kind === "verdict") {
      showInspectorScoring(state.selectedAlertId);
    } else if (node.kind === "narrative") {
      showInspectorNarrative(state.selectedAlertId);
    } else if (node.kind === "root") {
      pushInspectorAlert(state.selectedAlertId);
    }
  }

  // ════════════════════════════════════════════════════════════
  // PAYLOAD VIEW
  // ════════════════════════════════════════════════════════════

  const STAGES = [
    { id: "in",   label: "Incoming" },
    { id: "norm", label: "Normalized" },
    { id: "orch", label: "Orchestrator" },
    { id: "tools",label: "Tool calls" },
    { id: "rules",label: "Rule scoring" },
    { id: "narr", label: "Narrative" },
    { id: "disp", label: "Disposition" },
  ];

  function renderPayloadStages() {
    els.payloadStages.innerHTML = "";
    STAGES.forEach((s, i) => {
      const chip = document.createElement("div");
      chip.className = "stage-chip";
      chip.dataset.stage = s.id;
      chip.dataset.state = "pending";
      chip.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg><span>${esc(s.label)}</span>`;
      els.payloadStages.appendChild(chip);
      if (i < STAGES.length - 1) {
        const arrow = document.createElement("div");
        arrow.className = "stage-arrow";
        arrow.textContent = "›";
        els.payloadStages.appendChild(arrow);
      }
    });
  }
  renderPayloadStages();

  function updatePayloadStage(alertId) {
    if (alertId !== state.selectedAlertId) return;
    const run = state.runs.get(alertId);
    if (!run) return;
    const map = {
      in:    !!findAlert(alertId),
      norm:  !!run.startedAt,
      orch:  !!run.startedAt,
      tools: run.phases[1] === "done" || Object.values(run.tools).some(t => t.state === "done"),
      rules: !!run.scores,
      narr:  !!run.narrative,
      disp:  !!run.worksheet,
    };
    $$(".stage-chip", els.payloadStages).forEach((chip) => {
      const done = map[chip.dataset.stage];
      const active =
        (chip.dataset.stage === "tools" && run.phases[1] === "active") ||
        (chip.dataset.stage === "rules" && run.phases[2] === "active") ||
        (chip.dataset.stage === "narr"  && run.phases[3] === "active");
      chip.dataset.state = done ? "done" : (active ? "active" : "pending");
    });
  }

  function refreshPayloadForAlert(alertId) {
    const a = findAlert(alertId);
    const run = state.runs.get(alertId);
    els.payloadCurrent.textContent = a ? `${alertId} · ${a.customer_name || ""}` : (alertId || "no alert selected");

    if (a) {
      const raw = JSON.stringify(a, null, 2);
      els.payloadRaw.innerHTML = highlightJson(raw);
      els.payloadRawSize.textContent = `${raw.length}B`;
    } else {
      els.payloadRaw.textContent = "// no alert";
      els.payloadRawSize.textContent = "—";
    }

    if (run?.worksheet) {
      const parsed = JSON.stringify(run.worksheet, null, 2);
      els.payloadParsed.innerHTML = highlightJson(parsed);
      els.payloadParsedSize.textContent = `${parsed.length}B`;
    } else {
      els.payloadParsed.textContent = "// runs after orchestrator completes";
      els.payloadParsedSize.textContent = "—";
    }

    updatePayloadStage(alertId);
  }

  // ════════════════════════════════════════════════════════════
  // RULE ENGINE / SCORING
  // ════════════════════════════════════════════════════════════

  function refreshScoringForAlert(alertId) {
    const a = findAlert(alertId);
    const run = state.runs.get(alertId);
    els.scoringCurrent.textContent = a ? `${alertId} · ${a.customer_name || ""}` : (alertId || "no alert selected");

    if (run?.scores) {
      const s = run.scores;
      els.scoringVerdictMeta.textContent = `confidence ${s.confidence_pct}% · ${s.verdict}`;

      const base = Number(s.base) || 0;
      const final = Number(s.final) || 0;
      const ws = run.worksheet || {};
      const context = Number(ws.context_score) || 0;
      const conf = Number(ws.confidence_adjustment) || 0;

      els.barBase.style.width    = Math.min(100, Math.max(0, base * 100)) + "%";
      els.barContext.style.width = Math.min(100, Math.abs(context) * 100 / 0.7) + "%";
      els.barConf.style.width    = Math.min(100, Math.abs(conf) * 100 / 0.4) + "%";
      els.barFinal.style.width   = Math.min(100, Math.max(0, final * 100)) + "%";

      els.valBase.textContent    = fmt(base);
      els.valContext.textContent = (context >= 0 ? "+" : "") + fmt(context);
      els.valConf.textContent    = (conf >= 0 ? "+" : "") + fmt(conf);
      els.valFinal.textContent   = fmt(final);

      // Gauge — arc length 251 for half circle
      const offset = 251 - 251 * Math.max(0, Math.min(1, final));
      els.gaugeFill.setAttribute("stroke-dashoffset", String(offset));
      els.gaugeValue.textContent = fmt(final, 2);
      els.gaugeVerdict.textContent = s.verdict || "—";
      els.gaugeVerdict.dataset.verdict = s.verdict || "";

      renderExplainCards(ws);
    } else {
      els.scoringVerdictMeta.textContent = "awaiting verdict";
      els.gaugeFill.setAttribute("stroke-dashoffset", "251");
      els.gaugeValue.textContent = "—";
      els.gaugeVerdict.textContent = "—";
      els.gaugeVerdict.dataset.verdict = "";
      [els.barBase, els.barContext, els.barConf, els.barFinal].forEach((b) => b.style.width = "0%");
      [els.valBase, els.valContext, els.valConf, els.valFinal].forEach((v) => v.textContent = v === els.valFinal ? "0.000" : "+0.000");
      els.explainGrid.innerHTML = "";
    }

    if (run?.narrative) {
      els.narrativeBody.textContent = run.narrative;
      els.narrativeBody.classList.remove("empty");
      els.narrativeModel.textContent = "Groq · llama-3.1-8b-instant";
    } else {
      els.narrativeBody.textContent = "No narrative yet. Run a batch and select an alert.";
      els.narrativeBody.classList.add("empty");
      els.narrativeModel.textContent = "—";
    }
  }

  function renderExplainCards(ws) {
    // Reconstruct per-factor contributions from the worksheet counts
    const factors = [
      { name: "Adverse media", n: ws.adverse_media_count ?? 0, per: 0.05, cap: 0.20, kind: "pos" },
      { name: "UBO chain",     n: ws.ubo_chain_found ? 1 : 0,  per: 0.10, cap: 0.10, kind: "pos", binary: true },
      { name: "Large txns",    n: ws.transactions?.large_count ?? 0, per: 0.05, cap: 0.15, kind: "pos" },
      { name: "Intl txns",     n: ws.transactions?.international_count ?? 0, per: 0.05, cap: 0.15, kind: "pos" },
      { name: "Registry hits", n: ws.registry_match_count ?? 0, per: 0.05, cap: 0.10, kind: "pos" },
      { name: "Prior escalations", n: ws.prior_cases?.prior_escalations ?? 0, per: 0.10, cap: 0.20, kind: "pos" },
      { name: "Prior clearances",  n: ws.prior_cases?.prior_clearances ?? 0,  per: 0.05, cap: 0.20, kind: "neg" },
    ];
    els.explainGrid.innerHTML = factors.map((f) => {
      const raw = f.binary ? (f.n ? f.per : 0) : Math.min(f.n * f.per, f.cap);
      const contrib = f.kind === "neg" ? -raw : raw;
      const cls = contrib > 0 ? "pos" : contrib < 0 ? "neg" : "neu";
      const sign = contrib > 0 ? "+" : contrib < 0 ? "−" : "";
      const reason = f.binary
        ? (f.n ? "UBO chain found — corroborates the match." : "No UBO chain — neutral.")
        : f.kind === "neg"
          ? (f.n ? `${f.n} prior clearance(s) — historical false-positive pattern.` : "No prior clearances.")
          : `${f.n} record(s) × ${f.per.toFixed(2)} (cap ${f.cap.toFixed(2)})`;
      return `
        <div class="explain-card">
          <div class="explain-card-head">
            <span class="explain-factor">${esc(f.name)}</span>
            <span class="explain-contrib ${cls}">${sign}${Math.abs(contrib).toFixed(3)}</span>
          </div>
          <div class="explain-reason">${esc(reason)}</div>
        </div>`;
    }).join("");
  }

  // ════════════════════════════════════════════════════════════
  // ARCHITECTURE VIEW
  // ════════════════════════════════════════════════════════════

  const ARCH_NODES = [
    { id: "browser",    label: "Browser",         sub: "Observability UI",     x:  80,  y: 60,  port: ":7000" },
    { id: "workflow",   label: "workflow_ui.py",  sub: "FastAPI · SSE",        x: 320,  y: 60,  port: ":7000" },
    { id: "intake",     label: "alert_intake.py", sub: "FastAPI · alerts API", x: 320,  y: 200, port: ":8005" },
    { id: "orchestrator", label: "HybridOrchestrator", sub: "agent.py · 3 phases", x: 580, y: 60, port: "lib" },
    { id: "hooks",      label: "HookManager",     sub: "pre/post · SHA-256",   x: 580,  y: 200, port: "lib" },
    { id: "tools",      label: "Tools",           sub: "7 functions",          x: 580,  y: 340, port: "lib" },
    { id: "groq",       label: "Groq",            sub: "llama-3.1-8b-instant", x: 840,  y: 60,  port: "api" },
    { id: "dynamo",     label: "DynamoDB Local",  sub: "6 tables",             x: 840,  y: 200, port: ":8001" },
    { id: "sqlite",     label: "sanctions.db",    sub: "SQLite · 66k rows",    x: 840,  y: 340, port: "fs" },
    { id: "audit",      label: "audit_log.jsonl", sub: "Append-only · hashed", x: 840,  y: 460, port: "fs" },
  ];

  const ARCH_EDGES = [
    ["browser", "workflow"],
    ["workflow", "intake"],
    ["workflow", "orchestrator"],
    ["intake", "dynamo"],
    ["orchestrator", "hooks"],
    ["hooks", "tools"],
    ["orchestrator", "groq"],
    ["tools", "dynamo"],
    ["tools", "sqlite"],
    ["hooks", "audit"],
  ];

  function buildArchitecture() {
    const W = 200, H = 56;
    const fragE = document.createDocumentFragment();
    ARCH_EDGES.forEach(([a, b]) => {
      const na = ARCH_NODES.find(n => n.id === a);
      const nb = ARCH_NODES.find(n => n.id === b);
      const path = elSVG("path");
      path.classList.add("arch-edge");
      const sx = na.x + W/2, sy = na.y + H/2;
      const tx = nb.x + W/2, ty = nb.y + H/2;
      const mx = (sx + tx) / 2;
      path.setAttribute("d", `M ${sx} ${sy} C ${mx} ${sy} ${mx} ${ty} ${tx} ${ty}`);
      fragE.appendChild(path);
    });
    els.archEdges.appendChild(fragE);

    const fragN = document.createDocumentFragment();
    ARCH_NODES.forEach((n) => {
      const g = elSVG("g");
      g.classList.add("arch-node");
      g.dataset.id = n.id;
      g.setAttribute("transform", `translate(${n.x},${n.y})`);

      const rect = elSVG("rect");
      rect.classList.add("arch-bg");
      rect.setAttribute("width", W);
      rect.setAttribute("height", H);
      rect.setAttribute("rx", 10);
      g.appendChild(rect);

      const label = elSVG("text");
      label.classList.add("arch-label");
      label.setAttribute("x", 14);
      label.setAttribute("y", 24);
      label.textContent = n.label;
      g.appendChild(label);

      const sub = elSVG("text");
      sub.classList.add("arch-sub");
      sub.setAttribute("x", 14);
      sub.setAttribute("y", 42);
      sub.textContent = n.sub;
      g.appendChild(sub);
      fragN.appendChild(g);
    });
    els.archNodes.appendChild(fragN);

    // Service status cards below SVG
    els.archServices.innerHTML = ARCH_NODES.map((n) => `
      <div class="arch-service-card">
        <div class="arch-service-row">
          <span class="arch-service-dot up"></span>
          <span class="arch-service-name">${esc(n.label)}</span>
          <span class="arch-service-port">${esc(n.port)}</span>
        </div>
        <div class="arch-service-desc">${esc(n.sub)}</div>
      </div>
    `).join("");
  }

  // Animate architecture edges based on stream state
  setInterval(() => {
    const live = state.streamState === "running";
    $$(".arch-edge", els.archEdges).forEach((e) => e.classList.toggle("active", live));
    if (live) els.archThroughput.textContent = `${state.toolsFired} tools · ${state.processed}/${state.total} done`;
    else      els.archThroughput.textContent = `${state.toolsFired} tools fired · idle`;
  }, 600);

  // ════════════════════════════════════════════════════════════
  // INSPECTOR (right rail)
  // ════════════════════════════════════════════════════════════

  function inspectorEmpty(msg) {
    els.inspector.innerHTML = `
      <div class="inspector-empty">
        <svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" opacity="0.45"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
        <div class="inspector-empty-title">Tool Inspector</div>
        <div class="inspector-empty-sub">${esc(msg)}</div>
      </div>`;
    els.inspectorSub.textContent = "select a node";
  }

  // ── Inspector primitives ─────────────────────────────────
  const chevIcon = `<svg class="insp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;

  const ICON = {
    user:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
    target:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>`,
    list:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>`,
    shield:  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
    code:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
    pulse:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>`,
    log:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
    flag:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>`,
  };

  function card(title, body, opts = {}) {
    const open = opts.open !== false;
    const meta = opts.meta || "";
    const icon = opts.icon || ICON.list;
    return `
      <section class="insp-card" data-open="${open}">
        <header class="insp-card-head">
          <div class="insp-card-title">${icon}<span>${esc(title)}</span></div>
          <div class="insp-card-meta">${meta}${chevIcon}</div>
        </header>
        <div class="insp-card-body">${body}</div>
      </section>`;
  }

  function pushInspectorAlert(alertId) {
    const a = findAlert(alertId);
    if (!a) return inspectorEmpty("Click any tool node in the graph or an alert in the intake stream to see its inputs, outputs, audit hash and regulatory context.");
    const run = state.runs.get(alertId);
    els.inspectorSub.textContent = "alert";

    const score = pctOf(a);
    const risk = riskOf(a);
    const verdict = run?.scores?.verdict || "—";

    const hero = `
      <div class="insp-hero" data-kind="${verdictKind(verdict)}">
        <div class="insp-hero-top">
          <div class="insp-hero-eyebrow">Alert</div>
          <div class="insp-badge mut mono">${esc((alertId || "").slice(0, 16))}</div>
        </div>
        <div class="insp-hero-title">${esc(a.customer_name || "—")}</div>
        <div class="insp-hero-sub">→ ${esc(a.matched_entity || "—")}</div>
        <div class="insp-indicators" style="margin-top:14px;">
          <span class="insp-indicator ${risk === "high" ? "bad" : risk === "mid" ? "warn" : "ok"}">${score}% match</span>
          <span class="insp-indicator">${esc(a.source_list || "—")}</span>
          <span class="insp-indicator">${esc(a.nationality || "—")}</span>
          ${verdict !== "—" ? `<span class="insp-indicator ${verdict === "TRUE_MATCH" ? "bad" : verdict === "UNCERTAIN" ? "warn" : "ok"}">${esc(verdict)}</span>` : ""}
        </div>
      </div>`;

    const customerProfile = card("Customer Profile", `
      <dl class="insp-kv">
        <dt>customer_id</dt>  <dd class="strong">${esc(a.customer_id || "—")}</dd>
        <dt>name</dt>         <dd>${esc(a.customer_name || "—")}</dd>
        <dt>nationality</dt>  <dd>${esc(a.nationality || "—")}</dd>
        <dt>dob</dt>          <dd>${esc(a.dob || "—")}</dd>
      </dl>`, { icon: ICON.user });

    const matchDetails = card("Match Details", `
      <dl class="insp-kv">
        <dt>matched_entity</dt><dd class="strong">${esc(a.matched_entity || "—")}</dd>
        <dt>source_list</dt>   <dd>${esc(a.source_list || "—")}</dd>
        <dt>match_score</dt>   <dd class="strong">${fmt(a.match_score)} · ${score}%</dd>
        <dt>risk_band</dt>     <dd>${risk.toUpperCase()}</dd>
      </dl>`, { icon: ICON.target });

    const triggerMeta = card("Trigger Metadata", `
      <dl class="insp-kv">
        <dt>alert_id</dt>   <dd>${esc(a.alert_id || "—")}</dd>
        <dt>status</dt>     <dd>${esc(a.status || "—")}</dd>
        <dt>created_at</dt> <dd>${esc(a.created_at || "—")}</dd>
        <dt>sla_deadline</dt><dd>${esc(a.sla_deadline || "—")}</dd>
      </dl>`, { icon: ICON.flag, open: false });

    const verdictCard = run?.scores ? card("Verdict & Scoring", `
      <dl class="insp-kv">
        <dt>verdict</dt>           <dd class="strong">${esc(run.scores.verdict)}</dd>
        <dt>final_risk_score</dt>  <dd>${fmt(run.scores.final)}</dd>
        <dt>confidence</dt>        <dd>${run.scores.confidence_pct}%</dd>
        <dt>base_score</dt>        <dd>${fmt(run.scores.base)}</dd>
      </dl>`, { icon: ICON.shield, meta: `<span class="insp-badge ${verdictBadgeClass(run.scores.verdict)}">${esc(run.scores.verdict)}</span>` }) : "";

    els.inspector.innerHTML = hero + customerProfile + matchDetails + triggerMeta + verdictCard;
    bindCardToggles();
  }

  function verdictKind(v) {
    if (v === "TRUE_MATCH" || v === "UNCERTAIN") return "blocked";
    if (v === "FALSE_POSITIVE") return "ok";
    return "";
  }

  function bindCardToggles() {
    $$(".insp-card-head", els.inspector).forEach((h) => {
      h.addEventListener("click", () => {
        const card = h.closest(".insp-card");
        const open = card.getAttribute("data-open") !== "false";
        card.setAttribute("data-open", String(!open));
      });
    });
  }

  function verdictBadgeClass(v) {
    return v === "TRUE_MATCH" ? "err" : v === "UNCERTAIN" ? "err" : v === "FALSE_POSITIVE" ? "ok" : "";
  }

  async function showInspectorTool(alertId, toolId) {
    const run = state.runs.get(alertId);
    const tool = run?.tools?.[toolId];
    els.inspectorSub.textContent = "tool";
    const dur = tool?.duration ? `${tool.duration.toFixed(0)}ms` : "—";
    const isClose = toolId === "close_alert";
    const heroKind = tool?.state === "blocked" ? "blocked"
                   : tool?.state === "done"    ? "ok"
                   : "";
    const statusBadge =
      tool?.state === "done"    ? `<div class="insp-badge ok">OK · ${dur}</div>`
    : tool?.state === "blocked" ? `<div class="insp-badge blocked">BLOCKED</div>`
    : tool?.state === "active"  ? `<div class="insp-badge mut">RUNNING…</div>`
    :                              `<div class="insp-badge mut">PENDING</div>`;

    // Try to load audit log details
    let auditEntries = [];
    if (alertId) {
      try {
        const r = await fetch(`/api/audit/${encodeURIComponent(alertId)}`);
        if (r.ok) {
          const data = await r.json();
          auditEntries = data.filter((e) => e.tool === toolId);
        }
      } catch (_) {}
    }

    const lastEntry = auditEntries[auditEntries.length - 1];

    const hero = `
      <div class="insp-hero" data-kind="${heroKind}">
        <div class="insp-hero-top">
          <div class="insp-hero-eyebrow">${esc(isClose ? "Regulatory · BLOCKED" : "Tool")}</div>
          ${statusBadge}
        </div>
        <div class="insp-hero-title">${esc(toolId)}</div>
        <div class="insp-hero-sub">${esc(TOOL_DESCRIPTIONS[toolId] || "")}</div>
      </div>
    `;

    const citationCard = isClose ? `
      <section class="insp-card" data-open="true">
        <header class="insp-card-head">
          <div class="insp-card-title">${ICON.shield}<span>Regulatory block</span></div>
          <div class="insp-card-meta"><span class="insp-badge blocked">PMLA · RBI</span>${chevIcon}</div>
        </header>
        <div class="insp-card-body">
          <div class="insp-citation">
            <span class="insp-citation-title">PMLA 2002 / RBI KYC Master Direction 2025</span>
            Alert disposition requires human analyst sign-off. This tool is permanently blocked by the PreToolUse hook (<span class="mono">hooks.py:65</span>). Every attempt is audit-logged with a SHA-256 hash and the regulatory citation.
          </div>
        </div>
      </section>` : "";

    const auditCards = lastEntry ? `
      ${card("Input · arguments", `<pre class="insp-code">${esc(JSON.stringify(lastEntry.tool_input || {}, null, 2))}</pre>`, { icon: ICON.code, meta: `<span class="insp-badge mut">${auditEntries.length} call(s)</span>` })}
      ${card("Output summary", `<pre class="insp-code">${esc(JSON.stringify(lastEntry.tool_output_summary || (lastEntry.reason ? { reason: lastEntry.reason } : {}), null, 2))}</pre>`, { icon: ICON.pulse, meta: `<span class="insp-badge mut">compact</span>` })}
      ${card("Audit trail", `
        <dl class="insp-kv">
          <dt>ts</dt>     <dd>${esc(lastEntry.ts || "")}</dd>
          <dt>event</dt>  <dd>${esc(lastEntry.event || "")}</dd>
          <dt>sha256</dt> <dd class="strong">${esc((lastEntry.sha256 || "").slice(0, 32))}…</dd>
          <dt>alert_id</dt><dd>${esc(lastEntry.alert_id || "")}</dd>
        </dl>`, { icon: ICON.log, open: false, meta: `<span class="insp-badge mut">SHA-256</span>` })}
    ` : card("Audit trail", `<div class="insp-hero-sub">No audit entries yet for this tool on this alert.</div>`, { icon: ICON.log });

    els.inspector.innerHTML = hero + citationCard + auditCards;
    bindCardToggles();
  }

  function showInspectorPhase(alertId, num) {
    const run = state.runs.get(alertId);
    els.inspectorSub.textContent = `phase ${num}`;
    const labels = {
      1: { title: "Phase 1 · LLM evidence gathering", sub: "Groq llama-3.1-8b-instant decides which tools to call. Calls each evidence tool exactly once; defensive fill re-runs any tool the LLM skipped or hallucinated args for." },
      2: { title: "Phase 2 · Rule engine (deterministic)", sub: "Pure Python formula: final = base + context_score + confidence_adjust. Verdict bands TM ≥ 0.85, UC ≥ 0.65, FP otherwise." },
      3: { title: "Phase 3 · LLM narrative", sub: "Groq writes a plain-English explanation citing the actual values produced by the rule engine. ≤ 200 words." },
    };
    const meta = labels[num];
    const ps = run?.phases?.[num] || "pending";
    els.inspector.innerHTML = `
      <div class="insp-section">
        <div class="insp-head">
          <div class="insp-title">${esc(meta.title)}</div>
          <div class="insp-badge ${ps === "done" ? "ok" : ps === "active" ? "mut" : ""}">${ps.toUpperCase()}</div>
        </div>
        <div class="insp-toolsub">${esc(meta.sub)}</div>
      </div>
    `;
  }

  function showInspectorScoring(alertId) {
    const run = state.runs.get(alertId);
    els.inspectorSub.textContent = "rule engine";
    if (!run?.scores) return inspectorEmpty("Phase 2 hasn't produced a verdict yet for this alert.");
    const ws = run.worksheet || {};
    els.inspector.innerHTML = `
      <div class="insp-section">
        <div class="insp-head">
          <div class="insp-title">Rule engine</div>
          <div class="insp-badge ${verdictBadgeClass(run.scores.verdict)}">${esc(run.scores.verdict)}</div>
        </div>
        <dl class="insp-kv">
          <dt>base</dt>             <dd>${fmt(run.scores.base)}</dd>
          <dt>context_score</dt>    <dd>${fmt(ws.context_score)}</dd>
          <dt>confidence_adjust</dt><dd>${fmt(ws.confidence_adjustment)}</dd>
          <dt>final</dt>            <dd><strong>${fmt(run.scores.final)}</strong></dd>
          <dt>confidence_pct</dt>   <dd>${run.scores.confidence_pct}%</dd>
        </dl>
      </div>
    `;
  }

  function showInspectorNarrative(alertId) {
    const run = state.runs.get(alertId);
    els.inspectorSub.textContent = "narrative";
    if (!run?.narrative) return inspectorEmpty("Phase 3 hasn't generated a narrative yet.");
    els.inspector.innerHTML = `
      <div class="insp-section">
        <div class="insp-head">
          <div class="insp-title">LLM narrative</div>
          <div class="insp-badge mut">${esc((run.narrative || "").length)} chars</div>
        </div>
        <div class="insp-toolsub" style="white-space: pre-wrap; line-height: 1.6;">${esc(run.narrative)}</div>
      </div>
    `;
  }

  // ════════════════════════════════════════════════════════════
  // TIMELINE (bottom)
  // ════════════════════════════════════════════════════════════

  function pushTimeline(ev) {
    const ts = now();
    const { badge, category, html, icon, severity } = renderTimelineRow(ev);
    const row = {
      ts, badge, category, severity,
      text: stripHtml(html),
      html,
      el: null,
    };
    state.timelineRows.push(row);
    // Cap to 500 rows
    if (state.timelineRows.length > 500) {
      const drop = state.timelineRows.shift();
      if (drop.el && drop.el.parentNode) drop.el.parentNode.removeChild(drop.el);
    }

    const div = document.createElement("div");
    div.className = "tl-row";
    div.dataset.category = category;
    div.dataset.severity = severity;
    div.innerHTML = `
      <span class="tl-ts">${ts}</span>
      <span class="tl-icon ${badge.kind}">${icon}</span>
      <span class="tl-badge ${badge.kind}">${badge.label}</span>
      <span class="tl-msg">${html}</span>
    `;
    row.el = div;
    els.timelineStream.appendChild(div);

    if (!matchesFilter(row)) div.classList.add("hidden-by-filter");

    if (!state.paused) {
      els.timelineStream.scrollTop = els.timelineStream.scrollHeight;
    }
  }

  function tlIcon(evType) {
    // Map event type to a category icon
    if (evType === "alert_start")        return TL_ICONS.alert_start;
    if (evType === "alert_complete")     return TL_ICONS.alert_complete;
    if (evType === "alert_error")        return TL_ICONS.alert_error;
    if (evType.startsWith("phase_") && evType.endsWith("_start"))    return TL_ICONS.phase_start;
    if (evType.startsWith("phase_") && evType.endsWith("_complete")) return TL_ICONS.phase_complete;
    if (evType === "tool_call_start")    return TL_ICONS.tool_call_start;
    if (evType === "tool_call_complete") return TL_ICONS.tool_call_complete;
    if (evType === "close_attempt")      return TL_ICONS.close_attempt;
    if (evType === "close_blocked")      return TL_ICONS.close_blocked;
    if (evType === "batch_start")        return TL_ICONS.batch_start;
    if (evType === "batch_complete")     return TL_ICONS.batch_complete;
    return TL_ICONS.default;
  }

  function stripHtml(html) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    return tmp.textContent || "";
  }

  function renderTimelineRow(ev) {
    const id = ev.alert_id ? `<span class="k">${esc(ev.alert_id)}</span>` : "";
    const icon = tlIcon(ev.type || "");
    switch (ev.type) {
      case "batch_start":
        return { badge: { kind: "info", label: "BCH" }, category: "phase", severity: "info", icon,
          html: `Batch started — <span class="v">${ev.count}</span> alerts queued` };
      case "batch_complete":
        return { badge: { kind: "ok", label: "BCH" }, category: "phase", severity: "ok", icon,
          html: `Batch <span class="s">complete</span>` };
      case "batch_error":
        return { badge: { kind: "err", label: "BCH" }, category: "phase", severity: "err", icon,
          html: `Batch <span class="e">error</span>: ${esc(ev.error)}` };
      case "alert_start":
        return { badge: { kind: "info", label: "ALR" }, category: "phase", severity: "info", icon,
          html: `${id} <span class="m">received · orchestrator started</span>` };
      case "alert_complete":
        return { badge: { kind: "ok", label: "ALR" }, category: "verdict", severity: "ok", icon,
          html: `${id} <span class="s">complete</span> · verdict <span class="v">${esc(ev.worksheet?.recommendation || "?")}</span>` };
      case "alert_error":
        return { badge: { kind: "err", label: "ALR" }, category: "phase", severity: "err", icon,
          html: `${id} <span class="e">error</span>: ${esc(ev.error)}` };
      case "phase_1_start": return { badge: { kind: "info", label: "PH1" }, category: "phase", severity: "info", icon, html: `${id} <span class="m">Phase 1 · LLM evidence gathering started</span>` };
      case "phase_1_complete": return { badge: { kind: "ok", label: "PH1" }, category: "phase", severity: "ok", icon, html: `${id} <span class="m">Phase 1 complete · all evidence tools captured</span>` };
      case "phase_2_start": return { badge: { kind: "info", label: "PH2" }, category: "phase", severity: "info", icon, html: `${id} <span class="m">Phase 2 · rule engine started</span>` };
      case "phase_2_complete":
        return { badge: { kind: "ok", label: "PH2" }, category: "verdict", severity: "ok", icon: TL_ICONS.verdict,
          html: `${id} Rule engine scored <span class="v">${fmt(ev.final_score)}</span> → <span class="v">${esc(ev.verdict)}</span>` };
      case "phase_3_start": return { badge: { kind: "info", label: "PH3" }, category: "phase", severity: "info", icon, html: `${id} <span class="m">Phase 3 · narrative generation started</span>` };
      case "phase_3_complete": return { badge: { kind: "ok", label: "PH3" }, category: "phase", severity: "ok", icon, html: `${id} <span class="m">Narrative generated · ${(ev.narrative || "").length} chars</span>` };
      case "tool_call_start": return { badge: { kind: "info", label: "TOL" }, category: "tool", severity: "info", icon, html: `${id} Tool <span class="k">${esc(ev.tool)}</span> started <span class="m">(${esc(ev.source || "")})</span>` };
      case "tool_call_complete":
        return {
          badge: { kind: ev.blocked ? "err" : (ev.ok === false ? "warn" : "ok"), label: "TOL" },
          category: "tool",
          severity: ev.blocked ? "err" : (ev.ok === false ? "warn" : "ok"),
          icon: ev.blocked ? TL_ICONS.close_blocked : (ev.ok === false ? TL_ICONS.tool_call_failed : TL_ICONS.tool_call_complete),
          html: `${id} Tool <span class="k">${esc(ev.tool)}</span> ${ev.blocked ? '<span class="e">blocked</span>' : (ev.ok === false ? '<span class="e">failed</span>' : '<span class="s">completed</span>')}`
        };
      case "close_attempt": return { badge: { kind: "warn", label: "CLS" }, category: "block", severity: "warn", icon, html: `${id} close_alert(<span class="v">${esc(ev.disposition)}</span>) <span class="m">attempted</span>` };
      case "close_blocked": return { badge: { kind: "err", label: "BLK" }, category: "block", severity: "err", icon, html: `${id} <span class="e">close_alert blocked</span> · ${esc((ev.reason||"").slice(0, 60))}` };
      default: return { badge: { kind: "mut", label: "—" }, category: "other", severity: "mut", icon: TL_ICONS.default, html: `${esc(ev.type || "event")}` };
    }
  }

  function matchesFilter(row) {
    if (state.timelineFilter !== "all" && row.category !== state.timelineFilter) return false;
    if (state.timelineSearch && !row.text.toLowerCase().includes(state.timelineSearch)) return false;
    return true;
  }

  function applyTimelineFilter() {
    state.timelineRows.forEach((r) => {
      if (!r.el) return;
      r.el.classList.toggle("hidden-by-filter", !matchesFilter(r));
    });
  }

})();
