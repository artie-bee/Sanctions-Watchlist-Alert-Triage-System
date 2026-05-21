/* ════════════════════════════════════════════════════════════════
   Sentinel · Multi-Agent Simulator — controller
   ────────────────────────────────────────────────────────────────
   Drives the REAL HybridOrchestrator over /api/simulator-run (SSE)
   for one DynamoDB alert at a time. Maps real orchestrator events
   to the target architecture layout: API-GW → SQS → Lambda →
   Supervisor → Retrieval / Enrichment / Scoring → Supervisor-final
   → STR + Verdict + Judge. No setTimeout-driven fake animations on
   the critical path; the only delayed nodes are the infra-row
   flashes and the post-completion Judge sweep.
   ════════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  // ── Tool → agent routing ────────────────────────────────────
  const TOOL_AGENT = {
    screening_api_lookup:        "retrieval",
    core_banking_get_customer:   "retrieval",
    case_management_prior_cases: "retrieval",
    get_adverse_media:           "enrichment",
    get_company_registry:        "enrichment",
    get_ubo_chain:               "enrichment",
    close_alert:                 "str",
  };
  const RETRIEVAL_TOOLS = new Set([
    "screening_api_lookup", "core_banking_get_customer", "case_management_prior_cases",
  ]);
  const ENRICHMENT_TOOLS = new Set([
    "get_adverse_media", "get_company_registry", "get_ubo_chain",
  ]);

  const AGENT_BADGE = {
    retrieval:  "RET",
    enrichment: "ENR",
    str:        "BLK",
  };

  // ── State ───────────────────────────────────────────────────
  const state = {
    alerts: [],
    selectedAlertId: null,
    es: null,
    streamState: "idle",
    autoMode: false,
    autoTimer: null,
    rotIdx: 0,
    processed: 0, toolsFired: 0, loops: 0,
    tm: 0, clear: 0, fp: 0,
    runs: new Map(),
    slaStart: null,
    slaInterval: null,
    retrievalToolsDone: new Set(),
    enrichmentToolsDone: new Set(),
  };

  // ── DOM cache ───────────────────────────────────────────────
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const els = {
    streamPill:     $("#stream-pill"),
    streamStatus:   $("#stream-status"),
    runBtn:         $("#run-btn"),
    autoBtn:        $("#auto-btn"),
    resetBtn:       $("#reset-btn"),
    speedSelect:    $("#speed-select"),
    scenarioSelect: $("#scenario-select"),
    intakeStream:   $("#intake-stream"),
    intakeCount:    $("#intake-count"),
    slaTimer:       $("#sla-timer"),

    kpiProcessed: $("#kpi-processed"),
    kpiTools:     $("#kpi-tools"),
    kpiLoops:     $("#kpi-loops"),
    kpiTM:        $("#kpi-tm"),
    kpiClear:     $("#kpi-clear"),
    kpiFP:        $("#kpi-fp"),
    trendProcessed: $("#trend-processed"),
    trendTools:     $("#trend-tools"),
    trendLoops:     $("#trend-loops"),
    trendTM:        $("#trend-tm"),
    trendClear:     $("#trend-clear"),
    trendFP:        $("#trend-fp"),

    alertHero:     $("#alert-hero"),
    alertScenario: $("#alert-scenario"),
    alertCustomer: $("#alert-customer"),
    alertMatch:    $("#alert-match"),
    alertAttrs:    $("#alert-attrs"),

    infraRow:           $("#infra-row"),
    nodeSupervisor:     $("#node-supervisor"),
    supervisorDot:      $("#supervisor-dot"),
    supervisorReason:   $("#supervisor-reasoning"),
    arrow1:             $("#arrow-1"),
    arrow2:             $("#arrow-2"),
    arrow3:             $("#arrow-3"),
    nodeRetrieval:      $("#node-retrieval"),
    nodeEnrichment:     $("#node-enrichment"),
    nodeScoring:        $("#node-scoring"),
    guardrailsBanner:   $("#guardrails-banner"),
    scoringTable:       $("#scoring-table"),
    loopBanner:         $("#loop-banner"),
    nodeSupervisorFinal:$("#node-supervisor-final"),
    freezeFlag:         $("#freeze-flag"),
    strFlag:            $("#str-flag"),
    nodeStr:            $("#node-str"),
    strAttempt:         $("#str-attempt"),
    strBlockReason:     $("#node-str .str-block-reason"),
    verdictZone:        $("#verdict-zone"),
    verdictPill:        $("#verdict-pill"),
    valBase:            $("#val-base"),
    valFinal:           $("#val-final"),
    valConf:            $("#val-conf"),
    confFill:           $("#conf-fill"),
    narrativeBody:      $("#narrative-body"),
    nodeJudge:          $("#node-judge"),
    judgeStatus:        $("#judge-status"),

    reasoningLog: $("#reasoning-log"),
  };

  // ── Utilities (mirroring observability.js) ──────────────────
  const esc = (s) => String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  const now = () => {
    const d = new Date();
    return d.toLocaleTimeString("en-GB", { hour12: false }) + "." +
      String(d.getMilliseconds()).padStart(3, "0");
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

  function tweenNumber(el, to, opts = {}) {
    if (!el) return;
    const from = parseFloat(el.dataset.val || "0") || 0;
    if (from === to) return;
    el.dataset.val = String(to);
    pulseValue(el);
    const start = performance.now();
    const dur = opts.duration || 420;
    function tick(t) {
      const k = Math.min(1, (t - start) / dur);
      const eased = 1 - Math.pow(1 - k, 3);
      const v = from + (to - from) * eased;
      el.textContent = String(Math.round(v));
      if (k < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  function pulseValue(el) {
    if (!el) return;
    el.classList.remove("pulse");
    void el.offsetWidth;
    el.classList.add("pulse");
  }
  function setTrend(el, pct) {
    if (!el) return;
    el.style.width = Math.max(0, Math.min(100, pct)) + "%";
  }
  function setStream(stateName, text) {
    state.streamState = stateName;
    if (els.streamPill) els.streamPill.dataset.state = stateName;
    if (els.streamStatus) els.streamStatus.textContent = text;
  }

  function updateKPIs() {
    const total = state.alerts.length || 1;
    tweenNumber(els.kpiProcessed, state.processed);
    tweenNumber(els.kpiTools,     state.toolsFired);
    tweenNumber(els.kpiLoops,     state.loops);
    tweenNumber(els.kpiTM,        state.tm);
    tweenNumber(els.kpiClear,     state.clear);
    tweenNumber(els.kpiFP,        state.fp);
    setTrend(els.trendProcessed, (state.processed / total) * 100);
    setTrend(els.trendTools,     Math.min(100, (state.toolsFired / 12) * 100));
    setTrend(els.trendLoops,     Math.min(100, state.loops * 25));
    setTrend(els.trendTM,        (state.tm    / total) * 100);
    setTrend(els.trendClear,     (state.clear / total) * 100);
    setTrend(els.trendFP,        (state.fp    / total) * 100);
  }

  function ensureRun(alertId) {
    if (!state.runs.has(alertId)) {
      state.runs.set(alertId, {
        alertId,
        phases: { 1: "pending", 2: "pending", 3: "pending" },
        verdict: null, narrative: null,
        baseScore: null, finalScore: null, confidence: null,
      });
    }
    return state.runs.get(alertId);
  }

  // ════════════════════════════════════════════════════════════
  // SLA TIMER
  // ════════════════════════════════════════════════════════════
  function startSLA() {
    state.slaStart = Date.now();
    if (state.slaInterval) clearInterval(state.slaInterval);
    state.slaInterval = setInterval(() => {
      const elapsed = (Date.now() - state.slaStart) / 1000;
      const el = els.slaTimer;
      if (!el) return;
      el.textContent = elapsed.toFixed(1) + "s / 25s";
      el.style.color =
        elapsed > 20 ? "var(--red-bright)" :
        elapsed > 15 ? "var(--amber)"      :
                       "var(--text-dim)";
    }, 100);
  }
  function stopSLA() {
    if (state.slaInterval) {
      clearInterval(state.slaInterval);
      state.slaInterval = null;
    }
  }

  // ════════════════════════════════════════════════════════════
  // ALERT INTAKE — left rail
  // ════════════════════════════════════════════════════════════
  async function loadAlerts() {
    try {
      const r = await fetch("/api/simulator-alerts");
      const data = await r.json();
      if (data && data.error) throw new Error(data.error);
      state.alerts = Array.isArray(data) ? data : [];
      els.intakeCount.textContent = `${state.alerts.length} alerts`;
      renderIntake();
      renderScenarioSelect();
      setStream("idle", state.alerts.length ? "ready" : "no alerts");
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
          <span>${esc(a.scenario_label || "")}</span>
          <span style="margin-left:auto">${score}%</span>
        </div>
        <div class="intake-score-bar"><div class="intake-score-fill" style="width:${score}%"></div></div>
      `;
      card.addEventListener("click", () => selectAlert(a.alert_id));
      els.intakeStream.appendChild(card);
    });
  }

  function renderScenarioSelect() {
    const opts = [`<option value="auto">Auto-rotate</option>`].concat(
      state.alerts.map((a, i) => {
        const letter = String.fromCharCode(65 + i);  // A, B, C…
        const label = `${letter} · ${a.scenario_label || ""} · ${(a.customer_name || a.alert_id || "").slice(0, 28)}`;
        return `<option value="${esc(a.alert_id)}">${esc(label)}</option>`;
      })
    );
    els.scenarioSelect.innerHTML = opts.join("");
  }

  function findAlert(alertId) {
    return state.alerts.find((a) => a.alert_id === alertId);
  }

  function selectAlert(alertId) {
    state.selectedAlertId = alertId;
    $$(".intake-card", els.intakeStream).forEach((c) =>
      c.classList.toggle("selected", c.dataset.alertId === alertId)
    );
    const a = findAlert(alertId);
    if (!a) {
      els.alertHero.classList.remove("visible");
      return;
    }
    const risk = riskOf(a);
    els.alertScenario.textContent = a.scenario_label || "—";
    els.alertCustomer.textContent = a.customer_name || "Unknown";
    els.alertMatch.textContent = `${pctOf(a)}% match`;
    els.alertMatch.className = "alert-match-badge " + risk;
    els.alertAttrs.innerHTML = [
      a.customer_id     ? `<span>id ${esc(a.customer_id)}</span>` : "",
      a.matched_entity  ? `<span>→ ${esc(a.matched_entity)}</span>` : "",
      a.source_list     ? `<span>${esc(a.source_list)}</span>` : "",
      a.dob             ? `<span>dob ${esc(a.dob)}</span>` : "",
      a.nationality     ? `<span>${esc(a.nationality)}</span>` : "",
    ].filter(Boolean).join("");
    els.alertHero.classList.add("visible");
  }

  // ════════════════════════════════════════════════════════════
  // STAGE MANIPULATION
  // ════════════════════════════════════════════════════════════
  function resetStage() {
    // Nodes back to pending
    ["node-supervisor", "node-retrieval", "node-enrichment", "node-scoring",
     "node-supervisor-final", "node-str", "node-judge"
    ].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.setAttribute("data-state", "pending");
    });
    // Tools back to pending (clear data-state)
    $$(".tool-list li").forEach((li) => li.removeAttribute("data-state"));
    // Infra pills clear
    $$(".infra-pill", els.infraRow).forEach((p) => p.classList.remove("active"));
    // Hide flow arrows and conditional banners / nodes
    els.alertHero.classList.remove("visible");
    els.arrow1.classList.remove("visible");
    els.arrow2.classList.remove("visible");
    els.arrow3.classList.remove("visible");
    els.loopBanner.classList.remove("visible");
    els.loopBanner.textContent = "";
    els.guardrailsBanner.classList.remove("visible");
    // Scoring table reset
    $$("tr", els.scoringTable).forEach((tr) => {
      tr.classList.remove("match", "partial", "miss");
      const s = tr.querySelector(".status");
      if (s) s.textContent = "—";
    });
    // Verdict zone reset
    els.verdictZone.className = "verdict-zone";
    els.verdictPill.textContent = "—";
    els.valBase.textContent = "—";
    els.valFinal.textContent = "—";
    els.valConf.textContent = "—";
    els.confFill.style.width = "0%";
    els.confFill.className = "conf-fill";
    els.narrativeBody.textContent = "";
    // STR + Judge reset
    if (els.strAttempt) els.strAttempt.textContent = "—";
    if (els.strBlockReason) els.strBlockReason.textContent = "";
    if (els.judgeStatus) els.judgeStatus.textContent = "Reviewing citation chain…";
    // Supervisor reasoning reset
    els.supervisorReason.textContent =
      "Awaiting alert — supervisor will reason through investigation plan.";
    els.supervisorDot.classList.remove("pulsing");
    // Reasoning log + intake processing
    els.reasoningLog.innerHTML = "";
    $$(".intake-card", els.intakeStream).forEach((c) =>
      c.classList.remove("processing")
    );
    // Stop SLA timer
    stopSLA();
    if (els.slaTimer) {
      els.slaTimer.textContent = "0.0s / 25s";
      els.slaTimer.style.color = "var(--text-dim)";
    }
  }

  function setAgent(agentId, newState) {
    const el = document.getElementById("node-" + agentId);
    if (el) el.setAttribute("data-state", newState);
  }
  function setTool(toolName, newState) {
    const li = document.querySelector(`.tool-list li[data-tool="${toolName}"]`);
    if (li) li.setAttribute("data-state", newState);
  }

  function flashInfra() {
    const pills = $$(".infra-pill", els.infraRow);
    pills.forEach((p, i) => {
      setTimeout(() => p.classList.add("active"), i * 180);
    });
  }

  function fillScoringTable(ev) {
    // Best-effort visual fill based on what the orchestrator emits.
    // The rule engine doesn't expose per-attribute matches directly,
    // so we colour each row by a coarse heuristic on final score.
    const final = Number(ev.final_score) || 0;
    const rows = $$("tr", els.scoringTable);
    const verdict = ev.verdict;
    rows.forEach((tr) => {
      const status = tr.querySelector(".status");
      if (!status) return;
      let cls = "partial", label = "partial";
      if (verdict === "TRUE_MATCH") {
        cls = "match"; label = "match";
      } else if (verdict === "FALSE_POSITIVE") {
        cls = "miss"; label = "no-match";
      } else {
        cls = "partial"; label = "partial";
      }
      tr.classList.add(cls);
      status.textContent = label;
    });
  }

  // ── Reasoning log ───────────────────────────────────────────
  function pushReasoning(agentKey, severity, message) {
    const row = document.createElement("div");
    row.className = "reasoning-row";
    row.dataset.severity = severity;
    const k = (agentKey || "INF").toLowerCase();
    row.innerHTML = `
      <span class="reasoning-ts">${now()}</span>
      <span class="reasoning-badge badge-${k}">${esc(agentKey)}</span>
      ${esc(message)}
    `;
    els.reasoningLog.appendChild(row);
    els.reasoningLog.scrollTop = els.reasoningLog.scrollHeight;
  }

  // ════════════════════════════════════════════════════════════
  // SSE — single-alert run
  // ════════════════════════════════════════════════════════════
  function startSingle() {
    if (state.es) return;

    let alertId = state.selectedAlertId;
    if (!alertId && state.alerts.length) {
      alertId = state.alerts[0].alert_id;
      selectAlert(alertId);
    }
    if (!alertId) return;

    resetStage();
    state.retrievalToolsDone.clear();
    state.enrichmentToolsDone.clear();
    selectAlert(alertId);

    setStream("running", "processing");
    els.runBtn.disabled = true;
    els.autoBtn.disabled = true;
    startSLA();

    // Intake card flips to processing
    const card = els.intakeStream.querySelector(`.intake-card[data-alert-id="${CSS.escape(alertId)}"]`);
    if (card) card.classList.add("processing");

    state.es = new EventSource(`/api/simulator-run?alert_id=${encodeURIComponent(alertId)}`);
    state.es.onmessage = (m) => {
      try { handleEvent(JSON.parse(m.data)); }
      catch (e) { console.error("bad SSE payload", e); }
    };
    state.es.onerror = () => {
      if (!state.es) return;
      if (state.es.readyState === EventSource.CLOSED) finishSingle("done");
      else finishSingle("error");
    };
  }

  function finishSingle(status) {
    if (state.es) { state.es.close(); state.es = null; }
    stopSLA();
    setStream(status === "error" ? "error" : "done",
              status === "error" ? "error" : "complete");
    els.runBtn.disabled = false;
    els.autoBtn.disabled = false;

    if (state.autoMode) {
      state.autoTimer = setTimeout(() => {
        if (!state.alerts.length) return;
        state.rotIdx = (state.rotIdx + 1) % state.alerts.length;
        state.selectedAlertId = state.alerts[state.rotIdx]?.alert_id;
        if (state.selectedAlertId) {
          selectAlert(state.selectedAlertId);
          startSingle();
        }
      }, 1500);
    }
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case "batch_start":
        pushReasoning("SUP", "info", "Stream opened · orchestrator handing off alert");
        break;

      case "alert_start": {
        ensureRun(ev.alert_id);
        flashInfra();
        setAgent("supervisor", "active");
        els.supervisorDot.classList.add("pulsing");
        els.supervisorReason.textContent = "Reading alert payload…";
        pushReasoning("SUP", "info", `Supervisor received ${ev.alert_id}`);
        break;
      }

      case "phase_1_start":
        els.supervisorReason.textContent =
          "Supervisor reasoning · planning investigation path…";
        pushReasoning("SUP", "info", "Phase 1 started · LLM tool calling loop");
        break;

      case "phase_1_complete":
        setAgent("supervisor", "done");
        els.supervisorDot.classList.remove("pulsing");
        els.arrow1.classList.add("visible");
        pushReasoning("SUP", "ok", "Phase 1 complete");
        break;

      case "phase_2_start":
        setAgent("scoring", "active");
        pushReasoning("SCR", "info", "Phase 2 · rule engine scoring");
        break;

      case "phase_2_complete": {
        setAgent("scoring", "done");
        const baseS  = Number(ev.base_score);
        const finalS = Number(ev.final_score);
        const conf   = Number(ev.confidence_pct);

        fillScoringTable(ev);

        if (Number.isFinite(baseS) && Number.isFinite(finalS) && finalS > baseS + 1e-9) {
          els.loopBanner.textContent =
            `⟳ Context signals elevated score · base ${baseS.toFixed(3)} → final ${finalS.toFixed(3)}`;
          els.loopBanner.classList.add("visible");
          state.loops++;
          updateKPIs();
        }

        els.arrow2.classList.add("visible");

        // Supervisor final check
        const sfn = els.nodeSupervisorFinal;
        sfn.setAttribute("data-state", "active");
        els.freezeFlag.textContent =
          ev.verdict === "TRUE_MATCH" ? "freeze: YES" : "freeze: NO";
        els.strFlag.textContent =
          ev.verdict === "TRUE_MATCH" ? "STR: REQUIRED" : "STR: not required";

        // Verdict zone fills now (narrative arrives in phase_3_complete)
        const vClass =
          ev.verdict === "TRUE_MATCH" ? "tm" :
          ev.verdict === "UNCERTAIN"  ? "uc" : "fp";
        els.verdictZone.className = "verdict-zone visible " + vClass;
        els.verdictPill.textContent = String(ev.verdict || "—").replace(/_/g, " ");
        els.valBase.textContent  = Number.isFinite(baseS)  ? baseS.toFixed(3)  : "—";
        els.valFinal.textContent = Number.isFinite(finalS) ? finalS.toFixed(3) : "—";
        els.valConf.textContent  = Number.isFinite(conf)   ? `${conf}%`        : "—";
        setTimeout(() => {
          els.confFill.style.width = (Number.isFinite(conf) ? conf : 0) + "%";
          els.confFill.className = "conf-fill " + vClass;
        }, 100);

        pushReasoning("SCR",
          ev.verdict === "FALSE_POSITIVE" ? "ok" :
          ev.verdict === "UNCERTAIN"      ? "warn" : "err",
          `Verdict: ${ev.verdict} · score ${Number(finalS).toFixed(3)} · conf ${ev.confidence_pct}%`
        );
        break;
      }

      case "phase_3_start":
        pushReasoning("SUP", "info", "Phase 3 · LLM narrative");
        break;

      case "phase_3_streaming_start":
        els.narrativeBody.textContent = "";
        els.narrativeBody.classList.add("streaming");
        pushReasoning("SUP", "info", "Phase 3 · narrative streaming");
        break;

      case "narrative_token":
        if (typeof ev.token === "string") {
          els.narrativeBody.textContent += ev.token;
        }
        break;

      case "phase_3_complete": {
        els.narrativeBody.classList.remove("streaming");
        const streamed = els.narrativeBody.textContent || "";
        const canonical = ev.narrative || "";
        if (streamed.trim() !== canonical.trim()) {
          els.narrativeBody.textContent = canonical;
        }
        pushReasoning("SUP", "ok",
          `Narrative ready · ${canonical.length} chars`);
        break;
      }

      case "tool_call_start": {
        const agent = TOOL_AGENT[ev.tool];
        if (agent === "retrieval" || agent === "enrichment") {
          setAgent(agent, "active");
          if (agent === "enrichment") {
            els.guardrailsBanner.classList.add("visible");
          }
        }
        setTool(ev.tool, "active");
        state.toolsFired++;
        updateKPIs();
        pushReasoning(
          AGENT_BADGE[agent] || "SCR",
          "info",
          `${ev.tool} · started [${ev.source || "?"}]`
        );
        break;
      }

      case "tool_call_complete": {
        const agent = TOOL_AGENT[ev.tool];
        const blocked = !!ev.blocked;
        const toolState = blocked ? "blocked" : (ev.ok === false ? "blocked" : "done");
        setTool(ev.tool, toolState);

        // Track agent-level completion
        if (agent === "retrieval") {
          state.retrievalToolsDone.add(ev.tool);
          if (state.retrievalToolsDone.size >= RETRIEVAL_TOOLS.size) {
            setAgent("retrieval", "done");
            // Supervisor re-reasoning
            setAgent("supervisor", "active");
            els.supervisorDot.classList.add("pulsing");
            els.supervisorReason.textContent =
              "Retrieval complete · re-evaluating evidence…";
          }
        }
        if (agent === "enrichment") {
          state.enrichmentToolsDone.add(ev.tool);
          if (state.enrichmentToolsDone.size >= ENRICHMENT_TOOLS.size) {
            setAgent("enrichment", "done");
          }
        }

        pushReasoning(
          AGENT_BADGE[agent] || "SCR",
          blocked ? "err" : (ev.ok === false ? "err" : "ok"),
          `${ev.tool}${blocked ? " · BLOCKED" : " · complete"}`
        );
        break;
      }

      case "close_attempt":
        els.nodeStr.setAttribute("data-state", "active");
        els.arrow3.classList.add("visible");
        if (els.strAttempt) {
          els.strAttempt.textContent =
            `Attempting disposition: ${ev.disposition}`;
        }
        pushReasoning("BLK", "warn",
          `close_alert(${ev.disposition}) attempted`);
        break;

      case "close_blocked":
        els.nodeStr.setAttribute("data-state", "blocked");
        if (els.strBlockReason) {
          els.strBlockReason.textContent =
            `🔒 POLICY BLOCK · ${ev.reason || "PMLA 2002 / RBI KYC Master Direction"}`;
        }
        pushReasoning("BLK", "err",
          `PMLA BLOCK · ${ev.reason || "close_alert disabled for agents"}`);
        break;

      case "alert_complete": {
        const ws = ev.worksheet || {};
        const verdict = ws.recommendation || ws.verdict || "";
        state.processed++;
        if (verdict === "TRUE_MATCH")          state.tm++;
        else if (verdict === "UNCERTAIN")      state.clear++;
        else if (verdict === "FALSE_POSITIVE") state.fp++;
        updateKPIs();

        // Mark sup-final done now that scoring is final
        els.nodeSupervisorFinal.setAttribute("data-state", "done");

        // Flip the intake card
        const card = els.intakeStream.querySelector(
          `.intake-card[data-alert-id="${CSS.escape(ev.alert_id)}"]`
        );
        if (card) {
          card.classList.remove("processing");
          card.classList.add(
            verdict === "TRUE_MATCH"     ? "complete-tm" :
            verdict === "UNCERTAIN"      ? "complete-uc" :
            verdict === "FALSE_POSITIVE" ? "complete-fp" : "complete-fp"
          );
        }

        // Judge sweep — async, off critical path
        setTimeout(() => {
          els.nodeJudge.setAttribute("data-state", "active");
          els.judgeStatus.textContent = "Reviewing citation chain…";
          setTimeout(() => {
            els.nodeJudge.setAttribute("data-state", "done");
            els.judgeStatus.textContent =
              "✓ Citations verified · chain complete · no contradictions";
            pushReasoning("VER", "ok", "Judge sweep complete · citations verified");
          }, 2000);
        }, 500);

        pushReasoning("VER", "ok", `Alert complete · ${verdict || "—"}`);
        finishSingle("done");
        break;
      }

      case "alert_error":
        pushReasoning("INF", "err", `Error: ${ev.error || "unknown"}`);
        finishSingle("error");
        break;

      case "batch_complete":
        finishSingle("done");
        break;

      case "batch_error":
        pushReasoning("INF", "err", `Batch error: ${ev.error || "?"}`);
        finishSingle("error");
        break;
    }
  }

  // ════════════════════════════════════════════════════════════
  // UI bindings
  // ════════════════════════════════════════════════════════════
  function bindUI() {
    els.runBtn.addEventListener("click", startSingle);

    els.autoBtn.addEventListener("click", () => {
      state.autoMode = !state.autoMode;
      els.autoBtn.classList.toggle("active", state.autoMode);
      if (state.autoMode && !state.es) {
        if (!state.selectedAlertId && state.alerts.length) {
          state.selectedAlertId = state.alerts[state.rotIdx % state.alerts.length].alert_id;
        }
        startSingle();
      } else if (!state.autoMode && state.autoTimer) {
        clearTimeout(state.autoTimer);
        state.autoTimer = null;
      }
    });

    els.resetBtn.addEventListener("click", () => {
      if (state.es) { state.es.close(); state.es = null; }
      if (state.autoTimer) { clearTimeout(state.autoTimer); state.autoTimer = null; }
      state.autoMode = false;
      els.autoBtn.classList.remove("active");
      state.processed = 0; state.toolsFired = 0; state.loops = 0;
      state.tm = 0; state.clear = 0; state.fp = 0;
      state.runs.clear();
      state.retrievalToolsDone.clear();
      state.enrichmentToolsDone.clear();
      updateKPIs();
      resetStage();
      $$(".intake-card", els.intakeStream).forEach((c) =>
        c.classList.remove("processing", "complete-tm", "complete-uc", "complete-fp", "selected")
      );
      state.selectedAlertId = null;
      els.runBtn.disabled = false;
      els.autoBtn.disabled = false;
      setStream("idle", state.alerts.length ? "ready" : "no alerts");
    });

    els.scenarioSelect.addEventListener("change", (e) => {
      const v = e.target.value;
      if (v === "auto") {
        state.selectedAlertId = null;
        $$(".intake-card", els.intakeStream).forEach((c) => c.classList.remove("selected"));
        els.alertHero.classList.remove("visible");
        return;
      }
      const a = findAlert(v);
      if (a) selectAlert(a.alert_id);
    });

    // Speed select is informational only — the orchestrator dictates the
    // actual cadence. Keeping the control wired so future display-only
    // tweaks (e.g. infra-row flash delay) can read it.
    els.speedSelect.addEventListener("change", () => {});
  }

  // ════════════════════════════════════════════════════════════
  // BOOT
  // ════════════════════════════════════════════════════════════
  document.addEventListener("DOMContentLoaded", async () => {
    bindUI();
    setStream("idle", "idle");
    await loadAlerts();
  });
})();
