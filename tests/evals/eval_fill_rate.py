"""Defensive-fill-rate eval — measures the agent's tool-calling self-sufficiency.

Phase 1 of HybridOrchestrator lets Claude call the 6 evidence tools itself.
_fill_missing_tools (agent.py) is the safety net: it re-runs tools the LLM
skipped or called with hallucinated args, so Phase 2 always has real inputs.

Every tool call emits a `tool_call_start` event carrying a `source`:
    source == "llm"   -> the model called it
    source == "fill"  -> the safety net called it

Classification (mirrors run_batch.py lines 43-56):
    TRUE fill      = a "fill" event for a tool the LLM never called
                     (the model failed to gather it; the net rescued it)
    redundant fill = a "fill" event for a tool the LLM ALSO called
                     (re-run anyway; a code smell, not an LLM-quality signal)

Eligibility — which tools count toward the headline:
    ELIGIBLE (real "is it missing?" guards in _fill_missing_tools):
        screening_api_lookup           (filled only if absent)
        core_banking_get_customer      (filled only if absent / id mismatch)
    ALWAYS RERUN by design (agent.py lines 653-672, no presence guard):
        get_adverse_media
        get_ubo_chain
        case_management_prior_cases
        get_company_registry
    The 4 always-rerun tools register a "fill" on every alert regardless of
    what the LLM did, so their ~100% fill rate is meaningless as a quality
    signal — they are excluded from the headline.

Headline metric:
    TRUE_fill_rate = (TRUE fills on the 2 eligible tools)
                     / (alerts x 2 eligible tools)
    Lower is better: 0% means the model gathered both eligible tools itself
    on every alert; 100% means the net had to rescue every one.

Trend tracking: one JSON line per run is appended to
tests/evals/fill_rate_history.jsonl so the rate can be watched over time.

NOTE: this eval only needs Phase 1 tool events + Phase 2 scoring. If the
Claude API is unavailable (e.g. low credits), Phase 1 falls back to the
defensive-fill path and Phase 3's narrative degrades gracefully — the run
still completes and the fill counts are still captured. When the LLM is
down, every eligible tool is filled, so expect a high TRUE_fill_rate.

Usage:
    python tests/evals/eval_fill_rate.py
    python tests/evals/eval_fill_rate.py --alerts 10
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# This file lives at <repo>/tests/evals/ ; walk up to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TRIAGE_SRC = REPO_ROOT / "sanctions_triage" / "src"
HISTORY_PATH = Path(__file__).resolve().parent / "fill_rate_history.jsonl"

# Windows consoles default to cp1252; force UTF-8 so box-drawing renders.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_env() -> None:
    """Load <repo>/.env into os.environ BEFORE importing agent.py (it reads
    ANTHROPIC_API_KEY at import time). dotenv if available, else manual."""
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
sys.path.insert(0, str(TRIAGE_SRC))

from db import get_table              # noqa: E402
from agent import HybridOrchestrator  # noqa: E402

# All six evidence tools, in their natural call order.
EVIDENCE_TOOLS = (
    "screening_api_lookup",
    "core_banking_get_customer",
    "get_adverse_media",
    "get_ubo_chain",
    "case_management_prior_cases",
    "get_company_registry",
)

# Only these two have real presence-guards in _fill_missing_tools, so a fill
# on them is a genuine signal the model missed something.
ELIGIBLE_TOOLS = (
    "screening_api_lookup",
    "core_banking_get_customer",
)


def first_n_alert_ids(n: int) -> list[str]:
    """Same selection pattern as run_batch.py."""
    t = get_table("sanctions_alerts")
    items = t.scan(ProjectionExpression="alert_id").get("Items", [])
    return [i["alert_id"] for i in items[:n]]


def run_one(alert_id: str) -> dict:
    """Run one alert; capture per-tool TRUE/redundant fill counts.
    Pattern copied from run_batch.py run_one()."""
    llm_called = {t: False for t in EVIDENCE_TOOLS}
    true_fills_by_tool: dict[str, int] = {t: 0 for t in EVIDENCE_TOOLS}
    redundant_fills_by_tool: dict[str, int] = {t: 0 for t in EVIDENCE_TOOLS}

    def cb(event: dict) -> None:
        if event.get("type") != "tool_call_start":
            return
        tool = event.get("tool")
        source = event.get("source")
        if tool not in EVIDENCE_TOOLS:
            return
        if source == "llm":
            llm_called[tool] = True
        elif source == "fill":
            if not llm_called[tool]:
                true_fills_by_tool[tool] += 1
            else:
                redundant_fills_by_tool[tool] += 1

    orch = HybridOrchestrator(progress_cb=cb)
    buf = io.StringIO()
    error = ""
    try:
        with contextlib.redirect_stdout(buf):
            orch.process_alert(alert_id)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    return {
        "alert_id": alert_id,
        "llm_called": llm_called,
        "true_fills_by_tool": true_fills_by_tool,
        "redundant_fills_by_tool": redundant_fills_by_tool,
        "error": error,
    }


def aggregate(rows: list[dict]) -> dict:
    """Sum per-tool TRUE/redundant fills across all alerts."""
    per_tool = {
        t: {"true_fills": 0, "redundant_fills": 0}
        for t in EVIDENCE_TOOLS
    }
    for r in rows:
        for t in EVIDENCE_TOOLS:
            per_tool[t]["true_fills"] += r["true_fills_by_tool"].get(t, 0)
            per_tool[t]["redundant_fills"] += r["redundant_fills_by_tool"].get(t, 0)
    return per_tool


def print_report(rows: list[dict], per_tool: dict) -> dict:
    n_alerts = len(rows)
    crashed = sum(1 for r in rows if r["error"])

    print()
    print("=" * 86)
    print(f"  DEFENSIVE FILL RATE EVAL  —  {n_alerts} alert(s)"
          + (f"  ({crashed} crashed)" if crashed else ""))
    print("=" * 86)

    hdr = f"{'tool':<28}| {'TRUE_fills':^10} | {'redundant_fills':^15} | TRUE_fill_rate"
    print(hdr)
    print("-" * 86)

    for t in EVIDENCE_TOOLS:
        eligible = t in ELIGIBLE_TOOLS
        tf = per_tool[t]["true_fills"]
        rf = per_tool[t]["redundant_fills"]
        rate = (tf / n_alerts * 100) if n_alerts else 0.0
        label = f"{t} *" if eligible else t
        suffix = "" if eligible else " [excluded - always rerun]"
        print(f"{label:<28}| {tf:^10} | {rf:^15} | {rate:>6.1f}%{suffix}")

    print("-" * 86)
    print("* eligible tools only")
    print()

    # Headline: TRUE fills on eligible tools / (alerts x eligible count)
    eligible_true = sum(per_tool[t]["true_fills"] for t in ELIGIBLE_TOOLS)
    opportunities = n_alerts * len(ELIGIBLE_TOOLS)
    headline = (eligible_true / opportunities * 100) if opportunities else 0.0
    print(f"HEADLINE: TRUE fill rate = {headline:.1f}% "
          f"({eligible_true} true fills / {opportunities} opportunities "
          f"on {len(ELIGIBLE_TOOLS)} eligible tools)")
    print()
    print("Note: 4 tools excluded — always re-run by design in agent.py lines 653-674.")
    if crashed:
        print(f"Note: {crashed} alert(s) crashed mid-run; their partial fill events are still counted.")
    print("=" * 86)

    return {
        "alerts": n_alerts,
        "crashed": crashed,
        "eligible_true_fills": eligible_true,
        "opportunities": opportunities,
        "true_fill_rate_pct": round(headline, 2),
        "per_tool": per_tool,
    }


def append_history(summary: dict) -> None:
    """One JSONL line per run for trend tracking."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "alerts": summary["alerts"],
        "crashed": summary["crashed"],
        "eligible_true_fills": summary["eligible_true_fills"],
        "opportunities": summary["opportunities"],
        "true_fill_rate_pct": summary["true_fill_rate_pct"],
        "per_tool_true_fills": {
            t: summary["per_tool"][t]["true_fills"] for t in EVIDENCE_TOOLS
        },
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"\nAppended trend record -> {HISTORY_PATH.relative_to(REPO_ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Defensive fill-rate eval.")
    ap.add_argument("--alerts", type=int, default=10,
                    help="number of alerts to run (default 10)")
    args = ap.parse_args()

    ids = first_n_alert_ids(args.alerts)
    if not ids:
        print("ERROR: no alerts in sanctions_alerts. Is the table seeded?", file=sys.stderr)
        return 2

    print(f"Running {len(ids)} alert(s) through HybridOrchestrator "
          f"(capturing Phase 1 tool events)...")
    rows = []
    for i, aid in enumerate(ids, 1):
        print(f"  [{i}/{len(ids)}] {aid} ...", flush=True)
        rows.append(run_one(aid))

    per_tool = aggregate(rows)
    summary = print_report(rows, per_tool)
    append_history(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
