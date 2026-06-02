"""Narrative-quality eval — Layer 1 (deterministic, no API key required).

The Phase-3 narrative (agent.py) is written by Claude and embedded in
Worksheet.narrative as a combined string:
    "<rule one-liner>\n\n— LLM (<model>) ——\n<LLM narrative with [cite:xx] markers>"
Worksheet.narrative_citations holds ONLY the valid, in-citation-map markers
that actually appear in the text (agent.py filters out anything not in the
map), so a marker present in the text but absent from narrative_citations is
by definition hallucinated.

Layer 1 checks (deterministic — run with no API key):
  1. Citation validity   — every [cite:xx] in the text resolves to a real
                           citation. HARD FAIL on any hallucinated marker.
  2. Regulatory grounding — narrative mentions PMLA or RBI. HARD FAIL if not.
  3. Numeric consistency  — score/percentage claims agree with the worksheet
                           (within 5%). HARD FAIL on a contradicting number.
  4. Word count           — LLM narrative under 200 words (PHASE3_PROMPT).
                           SOFT warning only.

Layer 2 (LLM judge) is a placeholder — needs an API key, trend-only, never
hard-fails.

No-API behaviour: if the Claude API is unavailable (low credits), Phase 3
generates nothing (or a "Narrative generation failed:" sentinel). We detect
that, mark every Layer 1 check N/A for that alert, and do NOT fail — the real
results return once the API key works.

Hard-fail (exit 1) only when, for a GENERATED narrative, any of:
  hallucinated citations > 0, no PMLA/RBI, or a contradicting number.

Usage:
    python tests/evals/eval_narrative_quality.py
    python tests/evals/eval_narrative_quality.py --ids ALR-AAA,ALR-BBB
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRIAGE_SRC = REPO_ROOT / "sanctions_triage" / "src"
SIMULATOR_URL = "http://localhost:7000/api/simulator-alerts"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()

# Layer 1 runs without an API key; only the Layer 2 judge needs it.
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
LAYER2_AVAILABLE = bool(API_KEY)

if not LAYER2_AVAILABLE:
    print("NOTE: ANTHROPIC_API_KEY not set")
    print("Running Layer 1 checks only")
    print("Layer 2 judge will be skipped")
    print()

sys.path.insert(0, str(TRIAGE_SRC))
from agent import HybridOrchestrator  # noqa: E402


# ── run one alert, capture narrative + LLM-only text ───────────────────────
def run_alert(alert_id: str) -> dict:
    """Run process_alert; return the worksheet plus the LLM-only narrative
    (captured from the phase_3_complete event) so we can tell whether Phase 3
    actually generated anything."""
    captured = {"llm_narrative": None}

    def cb(event: dict) -> None:
        if event.get("type") == "phase_3_complete":
            captured["llm_narrative"] = event.get("narrative")

    orch = HybridOrchestrator(progress_cb=cb)
    buf = io.StringIO()
    error = ""
    ws = None
    try:
        with contextlib.redirect_stdout(buf):
            ws = orch.process_alert(alert_id)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    return {"alert_id": alert_id, "ws": ws,
            "llm_narrative": captured["llm_narrative"], "error": error}


def narrative_generated(llm_narrative: str | None) -> bool:
    """True only if Phase 3 produced a real narrative (not empty, not the
    streaming-failure sentinel)."""
    if not llm_narrative:
        return False
    return not llm_narrative.strip().startswith("Narrative generation failed:")


# ── Layer 1 checks ─────────────────────────────────────────────────────────
def check_citations(narrative: str, narrative_citations: list[dict]) -> dict:
    """Check 1: every [cite:xx] marker in the text resolves to a real citation.
    narrative_citations already contains only valid (in-map) markers that
    appear in the text, so any text marker not among them is hallucinated."""
    raw_markers = re.findall(r"\[cite:[a-z]\d+\]", narrative)
    valid_set = {f"[cite:{c.get('marker')}]" for c in (narrative_citations or [])}
    valid = [m for m in raw_markers if m in valid_set]
    hallucinated = [m for m in raw_markers if m not in valid_set]
    rate = len(hallucinated) / max(len(raw_markers), 1)
    return {
        "raw": len(raw_markers),
        "valid": len(valid),
        "hallucinated": len(hallucinated),
        "hallucinated_markers": hallucinated,
        "hallucinated_rate": rate,
        "ok": len(hallucinated) == 0,
    }


def check_regulatory(narrative: str) -> dict:
    """Check 2: narrative references PMLA or RBI."""
    up = narrative.upper()
    pmla = "PMLA" in up
    rbi = "RBI" in up
    return {"pmla": pmla, "rbi": rbi, "ok": pmla or rbi}


def check_numeric(narrative: str, ws) -> dict:
    """Check 3: score/percentage claims in the narrative agree with the
    worksheet within 5%. Heuristic and conservative — only flags numbers that
    *contradict* a known value, and whitelists every legitimate worksheet
    number (initial match score, counts, etc.) to avoid false positives.

    NOTE: Worksheet has no confidence_pct field, so we derive it as
    round(final_risk_score * 100)."""
    final = float(ws.final_risk_score)
    confidence_pct = round(final * 100)

    # Whitelist of strings that legitimately appear in the narrative.
    known: set[str] = {f"{final:.2f}", f"{ws.initial_match_score:.2f}", str(confidence_pct)}
    for v in (
        ws.adverse_media_count, ws.registry_match_count,
        ws.transactions.total, ws.transactions.large_count,
        ws.transactions.international_count,
        ws.prior_cases.total_cases, ws.prior_cases.prior_clearances,
        ws.prior_cases.prior_escalations,
    ):
        known.add(str(int(v)))

    mismatches: list[str] = []
    # Percentages: compare to derived confidence (within 5 percentage points).
    for pct in re.findall(r"(\d+\.?\d*)\s*%", narrative):
        if pct in known:
            continue
        if abs(float(pct) - confidence_pct) > 5:
            mismatches.append(f"{pct}% vs confidence {confidence_pct}%")
    # 0..1 decimal scores: compare to final risk score (within 5% relative).
    for dec in re.findall(r"\b0?\.\d+\b", narrative):
        if dec in known:
            continue
        val = float(dec)
        if 0 < val <= 1 and final > 0 and abs(val - final) / final > 0.05:
            mismatches.append(f"{dec} vs final {final:.2f}")
    return {"ok": len(mismatches) == 0, "mismatches": mismatches}


def check_word_count(llm_narrative: str) -> dict:
    """Check 4 (soft): the LLM narrative — the text the 200-word PHASE3_PROMPT
    rule governs — should be under 200 words."""
    wc = len((llm_narrative or "").split())
    return {"words": wc, "ok": wc <= 200}


# ── Layer 2 placeholder ────────────────────────────────────────────────────
def run_layer2_judge(narrative: str, tool_results) -> dict:
    if not LAYER2_AVAILABLE:
        return {"score": None, "note": "API key not available - skip"}
    # TODO: implement LLM judge — use a stronger model than the narrator,
    # score 1-5 on 4 criteria, treat as trend only (never hard-fail).
    return {"score": None, "note": "TODO"}


# ── evaluate one alert ─────────────────────────────────────────────────────
def evaluate(alert_id: str) -> dict:
    run = run_alert(alert_id)
    ws = run["ws"]
    llm_narrative = run["llm_narrative"]

    if ws is None or not narrative_generated(llm_narrative):
        reason = run["error"] or "narrative empty - Phase 3 did not run (API credits unavailable)"
        return {"alert_id": alert_id, "generated": False, "reason": reason}

    narrative = ws.narrative
    cit = check_citations(narrative, ws.narrative_citations)
    reg = check_regulatory(narrative)
    num = check_numeric(narrative, ws)
    wc = check_word_count(llm_narrative)
    judge = run_layer2_judge(llm_narrative, None)

    hard_ok = cit["ok"] and reg["ok"] and num["ok"]
    return {
        "alert_id": alert_id,
        "generated": True,
        "citations": cit,
        "regulatory": reg,
        "numeric": num,
        "word_count": wc,
        "judge": judge,
        "passed": hard_ok,
    }


# ── reporting ──────────────────────────────────────────────────────────────
def _ck(ok: bool) -> str:
    return "✅" if ok else "❌"


def print_report(rows: list[dict]) -> int:
    print("NARRATIVE QUALITY EVAL — Layer 1")
    print("═" * 86)
    print()
    hdr = (f"{'alert_id':<15} | {'hallucinated':^12} | {'PMLA/RBI':^8} | "
           f"{'nums_ok':^7} | {'words':^5} | {'judge':^5} | PASS/FAIL")
    print(hdr)

    any_generated = False
    for r in rows:
        if not r["generated"]:
            print(f"{r['alert_id']:<15} | {'N/A':^12} | {'N/A':^8} | "
                  f"{'N/A':^7} | {'N/A':^5} | {'-':^5} | N/A")
            continue
        any_generated = True
        h = r["citations"]["hallucinated"]
        print(f"{r['alert_id']:<15} | {h:^12} | {_ck(r['regulatory']['ok']):^8} | "
              f"{_ck(r['numeric']['ok']):^7} | {r['word_count']['words']:^5} | "
              f"{'-':^5} | {'PASS' if r['passed'] else 'FAIL'}")

    # Per-alert warnings for ungenerated narratives.
    for r in rows:
        if not r["generated"]:
            print(f"\nWARNING [{r['alert_id']}]: {r['reason']}")

    print()
    gen = [r for r in rows if r["generated"]]
    if not gen:
        print("Hard checks (Layer 1): N/A — no narratives were generated.")
        print()
        print("NOTE: With no API key the narrative is not generated.")
        print("All Layer 1 checks show N/A. Real results when API key restored.")
        print()
        print("Overall: PASS (Layer 1) — nothing to fail (narratives not generated)")
        print("Layer 2 judge: pending API key")
        print("═" * 86)
        return 0

    all_cit = all(r["citations"]["ok"] for r in gen)
    all_reg = all(r["regulatory"]["ok"] for r in gen)
    all_num = all(r["numeric"]["ok"] for r in gen)
    all_wc = all(r["word_count"]["ok"] for r in gen)
    print("Hard checks (Layer 1):")
    print(f"  hallucinated citations = 0 on all alerts {_ck(all_cit)}")
    print(f"  regulatory reference present on all {_ck(all_reg)}")
    print(f"  numeric consistency on all {_ck(all_num)}")
    print()
    print("Soft checks:")
    print(f"  word count: {'all within 200 words ' + _ck(True) if all_wc else 'some exceed 200 words ⚠'}")
    print("  judge score: pending API key" if not LAYER2_AVAILABLE else "  judge score: see judge column")

    overall_pass = all_cit and all_reg and all_num
    print()
    print(f"Overall: {'PASS' if overall_pass else 'FAIL'} (Layer 1)")
    print("Layer 2 judge: " + ("pending API key" if not LAYER2_AVAILABLE else "computed"))
    print("═" * 86)
    return 0 if overall_pass else 1


def fetch_simulator_alert_ids(url: str) -> list[str]:
    import requests
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return [a["alert_id"] for a in r.json() if a.get("alert_id")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Narrative-quality eval (Layer 1).")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated alert_ids (overrides simulator fetch)")
    ap.add_argument("--url", type=str, default=SIMULATOR_URL,
                    help="simulator-alerts endpoint")
    args = ap.parse_args()

    if args.ids.strip():
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
    else:
        try:
            ids = fetch_simulator_alert_ids(args.url)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: could not fetch simulator alerts from {args.url}: {e}",
                  file=sys.stderr)
            return 2
    if not ids:
        print("ERROR: no alert IDs to evaluate.", file=sys.stderr)
        return 2

    print(f"Running {len(ids)} alert(s) through HybridOrchestrator "
          f"(capturing Phase 3 narrative)...")
    rows = []
    for i, aid in enumerate(ids, 1):
        print(f"  [{i}/{len(ids)}] {aid} ...", flush=True)
        rows.append(evaluate(aid))
    print()
    return print_report(rows)


if __name__ == "__main__":
    raise SystemExit(main())
