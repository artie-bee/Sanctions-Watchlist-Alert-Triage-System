"""Batch runner: 10 alerts -> metric on TRUE defensive fills.

A TRUE defensive fill = the LLM did NOT call a tool during Phase 1 but
_fill_missing_tools had to call it.  A redundant fill is when the LLM
called the tool and the fill ran it again anyway (code smell, not an
LLM quality signal).  Both are reported; only TRUE fills are the
headline metric.
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from db import get_table              # noqa: E402
from agent import HybridOrchestrator  # noqa: E402

EVIDENCE_TOOLS = (
    "screening_api_lookup",
    "core_banking_get_customer",
    "get_adverse_media",
    "get_company_registry",
    "get_ubo_chain",
    "case_management_prior_cases",
)


def first_n_alert_ids(n: int) -> list[str]:
    t = get_table("sanctions_alerts")
    items = t.scan(ProjectionExpression="alert_id").get("Items", [])
    return [i["alert_id"] for i in items[:n]]


def run_one(alert_id: str) -> dict:
    llm_called = {t: False for t in EVIDENCE_TOOLS}
    true_fills_by_tool: dict[str, int] = {}
    redundant_fills_by_tool: dict[str, int] = {}

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
                true_fills_by_tool[tool] = true_fills_by_tool.get(tool, 0) + 1
            else:
                redundant_fills_by_tool[tool] = redundant_fills_by_tool.get(tool, 0) + 1

    orch = HybridOrchestrator(progress_cb=cb)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ws = orch.process_alert(alert_id)
    except Exception as e:
        return {
            "alert_id": alert_id,
            "verdict": "-",
            "score": float("nan"),
            "llm_tools": sum(1 for v in llm_called.values() if v),
            "true_fills": sum(true_fills_by_tool.values()),
            "redundant_fills": sum(redundant_fills_by_tool.values()),
            "true_fills_by_tool": true_fills_by_tool,
            "error": f"{type(e).__name__}: {e}",
        }

    return {
        "alert_id": alert_id,
        "verdict": ws.recommendation,
        "score": ws.final_risk_score,
        "llm_tools": sum(1 for v in llm_called.values() if v),
        "true_fills": sum(true_fills_by_tool.values()),
        "redundant_fills": sum(redundant_fills_by_tool.values()),
        "true_fills_by_tool": true_fills_by_tool,
        "error": "",
    }


def main() -> int:
    ids = first_n_alert_ids(10)
    print(f"Picked {len(ids)} alerts from DynamoDB sanctions_alerts.\n")
    rows = []
    for i, aid in enumerate(ids, 1):
        print(f"  [{i}/{len(ids)}] {aid} ...", flush=True)
        rows.append(run_one(aid))

    hdr = ("alert_id", "verdict", "score", "LLM/6", "TRUE_fills", "redund.", "error")
    widths = (16, 15, 7, 6, 11, 8, 38)
    sep = "-" * (sum(widths) + 3 * (len(widths) - 1))
    print()
    print(sep)
    print(" | ".join(f"{h:<{w}}" for h, w in zip(hdr, widths)))
    print(sep)
    for r in rows:
        score_str = "-" if r["score"] != r["score"] else f"{r['score']:.3f}"
        cells = (
            r["alert_id"],
            r["verdict"],
            score_str,
            str(r["llm_tools"]),
            str(r["true_fills"]),
            str(r["redundant_fills"]),
            (r["error"] or "")[:widths[6]],
        )
        print(" | ".join(f"{c:<{w}}" for c, w in zip(cells, widths)))
    print(sep)

    total = len(rows)
    crashed = sum(1 for r in rows if r["error"])
    processed = total - crashed
    total_true_fills = sum(r["true_fills"] for r in rows)
    by_tool: dict[str, int] = {}
    for r in rows:
        for t, n in r["true_fills_by_tool"].items():
            by_tool[t] = by_tool.get(t, 0) + n
    scores = [r["score"] for r in rows if not r["error"]]
    avg_score = sum(scores) / len(scores) if scores else float("nan")
    verdict_counts = {"TRUE_MATCH": 0, "UNCERTAIN": 0, "FALSE_POSITIVE": 0}
    for r in rows:
        if r["verdict"] in verdict_counts:
            verdict_counts[r["verdict"]] += 1

    print()
    print(f"Total alerts attempted    : {total}")
    print(f"Total alerts processed    : {processed}")
    print(f"Total alerts crashed      : {crashed}")
    if avg_score == avg_score:
        print(f"Average score             : {avg_score:.3f}")
    else:
        print("Average score             : -")
    print(
        f"Verdicts                  : "
        f"TRUE_MATCH={verdict_counts['TRUE_MATCH']}  "
        f"UNCERTAIN={verdict_counts['UNCERTAIN']}  "
        f"FALSE_POSITIVE={verdict_counts['FALSE_POSITIVE']}"
    )
    print()
    print(f"** TRUE defensive fills (the number)    : {total_true_fills} **")
    if by_tool:
        print("TRUE defensive fills, by tool:")
        for t in EVIDENCE_TOOLS:
            n = by_tool.get(t, 0)
            if n:
                print(f"    {t:<32} {n}")
    else:
        print("TRUE defensive fills, by tool: (none - LLM called every tool)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
