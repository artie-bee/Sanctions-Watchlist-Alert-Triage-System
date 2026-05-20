(() => {
  "use strict";

  const TOOLS = [
    "screening_api_lookup",
    "core_banking_get_customer",
    "get_adverse_media",
    "get_ubo_chain",
    "case_management_prior_cases",
    "get_company_registry",
    "close_alert",
  ];

  // ── State ─────────────────────────────────────────────
  let alerts = [];
  let total = 0;
  let es = null;
  const worksheets = new Map();
  const counters = { processed: 0, tools: 0, blocks: 0 };

  // ── DOM ───────────────────────────────────────────────
  const $ = (sel, root = document) => root.querySelector(sel);
  const els = {
    runBtn:        $("#run-btn"),
    workflow:      $("#workflow-card"),
    currentLabel:  $("#current-label"),
    phaseStatus:   $("#phase-status"),
    phase1:        $("#phase-1"),
    phase2:        $("#phase-2"),
    phase3:        $("#phase-3"),
    scoreBase:     $("#score-base"),
    scoreFinal:    $("#score-final"),
    scoreVerdict:  $("#score-verdict"),
    narrative:     $("#phase-3-narrative"),
    grid:          $("#alerts-grid"),
    ctProcessed:   $("#ct-processed"),
    ctTools:       $("#ct-tools"),
    ctBlocks:      $("#ct-blocks"),
    modal:         $("#modal"),
    modalTitle:    $("#modal-title"),
    modalBody:     $("#modal-body"),
  };

  // ── Init ──────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", async () => {
    els.runBtn.addEventListener("click", startBatch);
    els.modal.querySelectorAll("[data-modal-close]").forEach((el) =>
      el.addEventListener("click", closeModal)
    );
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });
    await loadAlerts();
  });

  // ── Initial load ──────────────────────────────────────
  async function loadAlerts() {
    try {
      const r = await fetch("/api/alerts");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (data && data.error) throw new Error(data.error);
      alerts = Array.isArray(data) ? data : [];
      total = alerts.length;
      renderGrid();
      updateCounters();
      els.phaseStatus.textContent = total ? "ready" : "no alerts";
    } catch (e) {
      console.error("Failed to load alerts", e);
      els.grid.innerHTML = `<div class="grid-empty mono">Failed to load alerts: ${escapeHtml(
        e.message || String(e)
      )}</div>`;
      els.phaseStatus.textContent = "error";
    }
  }

  function renderGrid() {
    if (!alerts.length) {
      els.grid.innerHTML = `<div class="grid-empty mono">No PENDING alerts in DynamoDB. Seed first.</div>`;
      return;
    }
    els.grid.innerHTML = "";
    alerts.forEach((a) => {
      const card = document.createElement("div");
      card.className = "grid-card waiting";
      card.dataset.alertId = a.alert_id || "";
      card.innerHTML = `
        <div class="card-eyebrow">${escapeHtml((a.alert_id || "").slice(0, 16))}</div>
        <div class="card-title">${escapeHtml(a.customer_name || "Unknown customer")}</div>
        <div class="card-sub">→ ${escapeHtml(a.matched_entity || "—")}</div>
        <div class="card-row">
          <span class="kbd">${scorePct(a)}%</span>
          <span class="status-dot" data-status="waiting"></span>
        </div>
      `;
      card.addEventListener("click", () => openModal(a.alert_id));
      els.grid.appendChild(card);
    });
  }

  // ── Helpers for state ─────────────────────────────────
  function findCard(alertId) {
    if (!alertId) return null;
    return els.grid.querySelector(`[data-alert-id="${cssEscape(alertId)}"]`);
  }

  function setPhaseState(phaseEl, state) {
    if (phaseEl) phaseEl.dataset.state = state;
  }

  function setToolDot(tool, state) {
    const li = document.querySelector(
      `.tools li[data-tool="${cssEscape(tool)}"]`
    );
    if (li) li.dataset.state = state;
  }

  function setCardState(alertId, state, statusKind) {
    const card = findCard(alertId);
    if (!card) return;
    card.className = "grid-card " + state;
    const dot = card.querySelector(".status-dot");
    if (dot) dot.dataset.status = statusKind || state;
  }

  function resetPhases() {
    setPhaseState(els.phase1, "pending");
    setPhaseState(els.phase2, "pending");
    setPhaseState(els.phase3, "pending");
    TOOLS.forEach((t) => setToolDot(t, "empty"));
    els.scoreBase.textContent = "—";
    els.scoreFinal.textContent = "—";
    els.scoreVerdict.textContent = "—";
    els.scoreVerdict.dataset.verdict = "";
    els.narrative.textContent = "Awaiting verdict…";
  }

  function updateCounters() {
    els.ctProcessed.textContent = `${counters.processed} / ${total}`;
    els.ctTools.textContent = String(counters.tools);
    els.ctBlocks.textContent = String(counters.blocks);
  }

  function setPhaseStatus(text) {
    els.phaseStatus.textContent = text;
  }

  // ── Batch run ─────────────────────────────────────────
  function startBatch() {
    if (es) return;
    if (!alerts.length) return;

    counters.processed = 0;
    counters.tools = 0;
    counters.blocks = 0;
    worksheets.clear();
    updateCounters();

    els.runBtn.disabled = true;
    els.runBtn.textContent = "Running…";
    els.workflow.classList.remove("hidden");
    resetPhases();
    setPhaseStatus("connecting…");

    document.querySelectorAll(".grid-card").forEach((c) => {
      c.className = "grid-card waiting";
      const dot = c.querySelector(".status-dot");
      if (dot) dot.dataset.status = "waiting";
    });

    es = new EventSource("/api/run-batch");
    es.onmessage = (m) => {
      try {
        handleEvent(JSON.parse(m.data));
      } catch (err) {
        console.error("Bad SSE payload", err, m.data);
      }
    };
    es.onerror = () => {
      // Some browsers fire onerror on normal close; only end if we got
      // batch_complete or the connection state shows closed.
      if (es && es.readyState === EventSource.CLOSED) {
        finishBatch("connection closed");
      } else if (es) {
        finishBatch("connection lost");
      }
    };
  }

  function finishBatch(statusText) {
    if (es) { es.close(); es = null; }
    els.runBtn.disabled = false;
    els.runBtn.textContent = "Run Batch";
    if (statusText) setPhaseStatus(statusText);
  }

  // ── SSE event dispatch ────────────────────────────────
  function handleEvent(ev) {
    switch (ev.type) {
      case "batch_start":
        if (ev.count != null) {
          total = ev.count;
          updateCounters();
        }
        setPhaseStatus(`processing 0 / ${total}`);
        break;

      case "alert_start":      handleAlertStart(ev); break;
      case "phase_1_start":    handlePhase1Start(); break;
      case "tool_call_start":  handleToolStart(ev); break;
      case "tool_call_complete": handleToolComplete(ev); break;
      case "phase_1_complete": handlePhase1Complete(); break;
      case "phase_2_start":    handlePhase2Start(); break;
      case "phase_2_complete": handlePhase2Complete(ev); break;
      case "phase_3_start":    handlePhase3Start(); break;
      case "phase_3_complete": handlePhase3Complete(ev); break;

      case "close_attempt":
        setToolDot("close_alert", "active");
        break;
      case "close_blocked":
        setToolDot("close_alert", "blocked");
        counters.blocks++;
        updateCounters();
        break;

      case "alert_complete": handleAlertComplete(ev); break;
      case "alert_error":    handleAlertError(ev);    break;

      case "batch_complete":
        setPhaseStatus("batch complete");
        finishBatch();
        break;

      case "batch_error":
        setPhaseStatus(`batch error: ${ev.error || "unknown"}`);
        finishBatch();
        break;
    }
  }

  function handleAlertStart(ev) {
    const alert = alerts.find((a) => a.alert_id === ev.alert_id);
    const name = (alert && alert.customer_name) || ev.alert_id || "—";
    els.currentLabel.textContent = `${ev.alert_id} · ${name}`;
    setCardState(ev.alert_id, "processing", "processing");
    resetPhases();
    setPhaseStatus(`processing ${counters.processed + 1} / ${total}`);
  }

  function handlePhase1Start() { setPhaseState(els.phase1, "active"); }
  function handlePhase1Complete() { setPhaseState(els.phase1, "done"); }

  function handlePhase2Start() {
    setPhaseState(els.phase1, "done");
    setPhaseState(els.phase2, "active");
  }
  function handlePhase2Complete(ev) {
    setPhaseState(els.phase2, "done");
    els.scoreBase.textContent  = fmt(ev.base_score);
    els.scoreFinal.textContent = fmt(ev.final_score);
    const verdict = (ev.verdict || "").toUpperCase();
    els.scoreVerdict.textContent = verdict || "—";
    els.scoreVerdict.dataset.verdict = verdict;
  }

  function handlePhase3Start() {
    setPhaseState(els.phase2, "done");
    setPhaseState(els.phase3, "active");
  }
  function handlePhase3Complete(ev) {
    setPhaseState(els.phase3, "done");
    const text = (ev.narrative || "").trim();
    els.narrative.textContent = text || "No narrative generated.";
  }

  function handleToolStart(ev) {
    // Phase 1 may be reached via the LLM or defensive-fill path; make
    // sure the panel is marked active either way.
    if (els.phase1.dataset.state !== "done") {
      setPhaseState(els.phase1, "active");
    }
    if (ev.tool && ev.tool !== "close_alert") {
      setToolDot(ev.tool, "active");
    } else if (ev.tool === "close_alert") {
      setToolDot("close_alert", "active");
    }
  }

  function handleToolComplete(ev) {
    if (!ev.tool) return;
    if (ev.tool === "close_alert") {
      // The hook always blocks close_alert. Block counter is bumped
      // by the explicit close_blocked event, not here.
      if (ev.blocked) setToolDot("close_alert", "blocked");
      return;
    }
    setToolDot(ev.tool, ev.ok === false ? "blocked" : "done");
    counters.tools++;
    updateCounters();
  }

  function handleAlertComplete(ev) {
    if (ev.worksheet) worksheets.set(ev.alert_id, ev.worksheet);
    const verdict = (ev.worksheet && ev.worksheet.recommendation || "")
      .toUpperCase();
    const kind =
      verdict === "TRUE_MATCH"     ? "complete-tm"
    : verdict === "UNCERTAIN"      ? "complete-uc"
    : verdict === "FALSE_POSITIVE" ? "complete-fp"
    :                                "complete-fp";
    setCardState(ev.alert_id, kind, kind);
    counters.processed++;
    updateCounters();
    setPhaseStatus(`processed ${counters.processed} / ${total}`);
  }

  function handleAlertError(ev) {
    console.error("alert error", ev);
    setCardState(ev.alert_id, "error", "error");
    counters.processed++;
    updateCounters();
  }

  // ── Modal ─────────────────────────────────────────────
  function openModal(alertId) {
    if (!alertId) return;
    const ws = worksheets.get(alertId);
    if (!ws) return; // nothing to show yet
    els.modalTitle.textContent = `Worksheet · ${alertId}`;
    els.modalBody.innerHTML = renderWorksheet(ws);
    els.modal.classList.remove("hidden");
  }

  function closeModal() {
    els.modal.classList.add("hidden");
  }

  function renderWorksheet(ws) {
    const tx  = ws.transactions || {};
    const pc  = ws.prior_cases || {};
    const kyc = ws.kyc_summary || {};
    const hits = Array.isArray(ws.sanctions_db_hits) ? ws.sanctions_db_hits : [];
    const blockedActions = Array.isArray(ws.blocked_actions) ? ws.blocked_actions : [];
    const verdict = (ws.recommendation || "").toUpperCase();

    return `
      <div class="ws-grid">
        <div class="ws-cell">
          <div class="ws-label">Customer</div>
          <div class="ws-val">${escapeHtml(ws.customer_name || "—")}
            <span class="mono mut">(${escapeHtml(ws.customer_id || "—")})</span></div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Matched entity</div>
          <div class="ws-val">${escapeHtml(ws.matched_entity || "—")}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Source list</div>
          <div class="ws-val">${escapeHtml(ws.source_list || "—")}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Initial match score</div>
          <div class="ws-val mono">${fmt(ws.initial_match_score)}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Risk rating</div>
          <div class="ws-val">${escapeHtml(kyc.risk_rating || "—")}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Nationality</div>
          <div class="ws-val">${escapeHtml(kyc.nationality || "—")}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Transactions</div>
          <div class="ws-val">total ${tx.total ?? 0} · large ${tx.large_count ?? 0} · intl ${tx.international_count ?? 0}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Adverse media</div>
          <div class="ws-val">${ws.adverse_media_count ?? 0} record(s)</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">UBO chain</div>
          <div class="ws-val">${ws.ubo_chain_found ? "found" : "none"}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Registry hits</div>
          <div class="ws-val">${ws.registry_match_count ?? 0}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Prior cases</div>
          <div class="ws-val">${pc.total_cases ?? 0}
            <span class="mut">(${pc.prior_clearances ?? 0} clear · ${pc.prior_escalations ?? 0} esc)</span></div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Context score</div>
          <div class="ws-val mono">${fmt(ws.context_score)}</div>
        </div>
        <div class="ws-cell">
          <div class="ws-label">Confidence adjust</div>
          <div class="ws-val mono">${fmt(ws.confidence_adjustment)}</div>
        </div>
        <div class="ws-cell ws-cell-wide">
          <div class="ws-label">Final risk score</div>
          <div class="ws-val large">
            <span class="mono">${fmt(ws.final_risk_score)}</span>
            <span>→</span>
            <span class="verdict-pill mono" data-verdict="${escapeHtml(verdict)}">${escapeHtml(verdict || "—")}</span>
          </div>
        </div>
      </div>

      ${hits.length ? `
        <div class="ws-section-title">sanctions.db hits (${hits.length})</div>
        <ul class="ws-hits">
          ${hits.slice(0, 5).map((h) => `
            <li>
              <span class="mono">${escapeHtml(h.full_name || "")}</span>
              <span class="mut">${escapeHtml(h.source || "")} / ${escapeHtml(h.program || "")}</span>
            </li>`).join("")}
        </ul>` : ""}

      <div class="ws-section-title">narrative</div>
      <div class="ws-narrative">${escapeHtml(ws.narrative || "—")}</div>

      ${blockedActions.length ? `
        <div class="ws-section-title">blocked actions (${blockedActions.length})</div>
        <ul class="ws-blocked">
          ${blockedActions.map((b) => `<li>${escapeHtml(b)}</li>`).join("")}
        </ul>` : ""}
    `;
  }

  // ── Utilities ─────────────────────────────────────────
  function fmt(x) {
    if (x === null || x === undefined || x === "") return "—";
    const n = Number(x);
    if (Number.isNaN(n)) return String(x);
    return n.toFixed(3);
  }

  function scorePct(a) {
    const raw = Number(
      a.match_score != null ? a.match_score : (a.confidence != null ? a.confidence : 0)
    );
    if (!isFinite(raw)) return 0;
    const v = raw <= 1 ? raw * 100 : raw;
    return Math.max(0, Math.min(100, Math.round(v)));
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // CSS.escape polyfill for older browsers / quotes in attribute values
  function cssEscape(s) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(s);
    }
    return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
  }
})();
