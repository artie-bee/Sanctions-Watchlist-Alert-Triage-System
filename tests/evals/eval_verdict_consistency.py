"""Verdict-consistency eval.

Runs the SAME alert through HybridOrchestrator several times and checks
that the two deterministic outputs of the worksheet stay stable:

    ws.recommendation     (the verdict: TRUE_MATCH / UNCERTAIN / FALSE_POSITIVE)
    ws.final_risk_score   (the [0,1] score Phase 2 computes)

Why this should hold: Phase 2 scoring is pure Python over the tool
outputs; Phase 1 runs the LLM at temperature 0.0 and _fill_missing_tools
guarantees all six evidence tools execute with correct args; Phase 3
(the narrative) runs at temperature 0.3 but must NOT influence the
verdict. If a verdict drifts across identical re-runs, that invariant is
broken.

Usage (run from anywhere; loads <repo>/.env automatically):
    python tests/evals/eval_verdict_consistency.py
    python tests/evals/eval_verdict_consistency.py --alerts 6 --runs 3
    python tests/evals/eval_verdict_consistency.py --ids ALR-AC367F292C,ALR-7C16AF8EB7
    python tests/evals/eval_verdict_consistency.py --json out.json

Exit code 0 = every sampled alert produced an identical verdict AND
identical score on every run. Exit code 1 = at least one alert drifted
(or every run of some alert crashed). Suitable as a CI gate.

NOTE: each run appends to runtime/audit_log.jsonl, and a FALSE_POSITIVE
verdict appends to runtime/cleared_entities.jsonl. The memory file is
read only in Phase 3 (never Phase 2), so re-runs growing it cannot move
the verdict — consistency is preserved.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
from pathlib import Path

# This file lives at <repo>/tests/evals/ ; walk up to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent   # tests/evals -> tests -> repo
TRIAGE_SRC = REPO_ROOT / "sanctions_triage" / "src"         # bare-import modules (db, agent)

# Windows consoles default to cp1252; force UTF-8 so em-dashes etc. render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_env() -> None:
    """Load REPO_ROOT/.env into os.environ BEFORE importing agent.py
    (agent.py reads ANTHROPIC_API_KEY at import time). Uses python-dotenv
    if available, else a minimal manual parser. Never overwrites a value
    already present in the real environment."""
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
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env()
sys.path.insert(0, str(TRIAGE_SRC))

from db import get_table              # noqa: E402
from agent import HybridOrchestrator  # noqa: E402


# ── alert selection ───────────────────────────────────────────────────
def _scan_alerts() -> list[dict]:
    """Return all alerts with the fields we need for selection/reporting."""
    t = get_table("sanctions_alerts")
    items: list[dict] = []
    resp = t.scan(ProjectionExpression="alert_id, customer_name, match_score")
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = t.scan(
            ProjectionExpression="alert_id, customer_name, match_score",
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return items


def _to_float(x, default=0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def select_alert_ids(n: int) -> list[dict]:
    """Pick n alerts spread evenly across the match_score distribution so
    the eval exercises low / mid / high verdict classes. Deterministic:
    sorts by (match_score, alert_id) and samples at even indices."""
    rows = _scan_alerts()
    rows = [r for r in rows if r.get("alert_id")]
    if not rows:
        return []
    rows.sort(key=lambda r: (_to_float(r.get("match_score")), r.get("alert_id", "")))
    if n >= len(rows):
        chosen = rows
    else:
        step = (len(rows) - 1) / (n - 1) if n > 1 else 0
        idxs = sorted({round(i * step) for i in range(n)})
        # round() collisions can yield < n picks; backfill from the tail.
        i = len(rows) - 1
        while len(idxs) < n and i >= 0:
            idxs.append(i)
            idxs = sorted(set(idxs))
            i -= 1
        chosen = [rows[i] for i in idxs[:n]]
    return [
        {
            "alert_id": r["alert_id"],
            "customer_name": r.get("customer_name", ""),
            "match_score": _to_float(r.get("match_score")),
        }
        for r in chosen
    ]


def alerts_by_ids(ids: list[str]) -> list[dict]:
    t = get_table("sanctions_alerts")
    out = []
    for aid in ids:
        item = t.get_item(Key={"alert_id": aid}).get("Item") or {}
        out.append(
            {
                "alert_id": aid,
                "customer_name": item.get("customer_name", ""),
                "match_score": _to_float(item.get("match_score")),
                "_missing": not item,
            }
        )
    return out


# ── single run ────────────────────────────────────────────────────────
def run_once(alert_id: str) -> dict:
    """Run the orchestrator once; return {verdict, score, error}.
    Orchestrator stdout is silenced (same approach as run_batch.py)."""
    orch = HybridOrchestrator()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ws = orch.process_alert(alert_id)
    except Exception as e:  # noqa: BLE001
        return {"verdict": None, "score": float("nan"), "error": f"{type(e).__name__}: {e}"}
    return {"verdict": ws.recommendation, "score": float(ws.final_risk_score), "error": ""}


# ── consistency evaluation ──────────────────────────────────────────────
SCORE_EPS = 1e-9       # final_risk_score is round(...,3); equality expected.
T_UNCERTAIN = 0.65     # >= this -> UNCERTAIN ; below -> FALSE_POSITIVE
T_TRUE = 0.85          # >= this -> TRUE_MATCH
DEFAULT_FRAGILE_MARGIN = 0.03  # margin below this = verdict could flip easily (Omkar spec)


def verdict_margin(score: float) -> float:
    """Distance from `score` to the nearest verdict-decision boundary
    (0.65 and 0.85). A small margin means a tiny score change would flip
    the verdict (fragile); a large margin means the verdict is robust."""
    if isinstance(score, float) and math.isnan(score):
        return float("nan")
    if score < T_UNCERTAIN:                 # FALSE_POSITIVE
        return T_UNCERTAIN - score
    if score < T_TRUE:                       # UNCERTAIN
        return min(score - T_UNCERTAIN, T_TRUE - score)
    return score - T_TRUE                    # TRUE_MATCH


def evaluate_alert(meta: dict, runs: int) -> dict:
    alert_id = meta["alert_id"]
    results = [run_once(alert_id) for _ in range(runs)]

    ok_runs = [r for r in results if not r["error"]]
    verdicts = [r["verdict"] for r in ok_runs]
    scores = [r["score"] for r in ok_runs]

    unique_verdicts = sorted(set(verdicts))
    verdict_consistent = len(ok_runs) == runs and len(unique_verdicts) == 1
    if scores:
        score_spread = max(scores) - min(scores)
    else:
        score_spread = float("nan")
    score_consistent = len(ok_runs) == runs and score_spread <= SCORE_EPS

    rep_score = scores[0] if scores else float("nan")
    margin = verdict_margin(rep_score)
    fragile = (not math.isnan(margin)) and margin < DEFAULT_FRAGILE_MARGIN

    return {
        "alert_id": alert_id,
        "customer_name": meta.get("customer_name", ""),
        "match_score": meta.get("match_score", 0.0),
        "runs": results,
        "ok_run_count": len(ok_runs),
        "unique_verdicts": unique_verdicts,
        "verdict": unique_verdicts[0] if len(unique_verdicts) == 1 else None,
        "rep_score": rep_score,
        "verdict_consistent": verdict_consistent,
        "score_spread": score_spread,
        "score_consistent": score_consistent,
        "margin": margin,
        "fragile": fragile,
        "passed": verdict_consistent and score_consistent,
    }


# ── reporting ────────────────────────────────────────────────────────────
def _fmt_score(x: float) -> str:
    return "  nan" if isinstance(x, float) and math.isnan(x) else f"{x:.3f}"


def print_report(rows: list[dict], runs: int) -> None:
    print()
    print("=" * 78)
    print(f"  VERDICT CONSISTENCY EVAL  —  {len(rows)} alert(s) x {runs} run(s) each")
    print("=" * 78)

    hdr = ("alert_id", "match", "runs_ok", "verdict(s)", "score", "spread", "result")
    widths = (16, 6, 7, 16, 7, 8, 8)
    sep = "-" * (sum(widths) + 3 * (len(widths) - 1))
    print(sep)
    print(" | ".join(f"{h:<{w}}" for h, w in zip(hdr, widths)))
    print(sep)
    for r in rows:
        verdict_cell = ",".join(v or "ERR" for v in r["unique_verdicts"]) or "ERR"
        # representative score = first ok run's score, else nan
        rep_score = next((x["score"] for x in r["runs"] if not x["error"]), float("nan"))
        cells = (
            r["alert_id"],
            _fmt_score(r["match_score"]),
            f"{r['ok_run_count']}/{runs}",
            verdict_cell[:widths[3]],
            _fmt_score(rep_score),
            _fmt_score(r["score_spread"]),
            "PASS" if r["passed"] else "FAIL",
        )
        print(" | ".join(f"{c:<{w}}" for c, w in zip(cells, widths)))
    print(sep)

    # Detail any failures.
    failures = [r for r in rows if not r["passed"]]
    if failures:
        print("\nFAILURES (per-run breakdown):")
        for r in failures:
            print(f"  {r['alert_id']}  ({r['customer_name']}):")
            for i, run in enumerate(r["runs"], 1):
                if run["error"]:
                    print(f"    run {i}: ERROR  {run['error']}")
                else:
                    print(f"    run {i}: {run['verdict']:<15} score={_fmt_score(run['score'])}")

    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    verdict_ok = sum(1 for r in rows if r["verdict_consistent"])
    score_ok = sum(1 for r in rows if r["score_consistent"])
    print("\nSummary:")
    print(f"  Alerts evaluated         : {total}")
    print(f"  Verdict-consistent       : {verdict_ok}/{total}")
    print(f"  Score-consistent (<{SCORE_EPS:g}) : {score_ok}/{total}")
    print(f"  Overall PASS             : {passed}/{total}")
    print("=" * 78)
    print("RESULT:", "PASS — all sampled verdicts are stable"
          if passed == total else "FAIL — at least one verdict drifted")
    print("=" * 78)


def print_report_omkar(rows: list[dict]) -> None:
    """Omkar's exact table + one-line summary.

    columns: alert_id | verdict | score_range | margin | PASS/FAIL
      score_range = spread of final_risk_score across runs (0.0000 = stable)
      margin      = distance from verdict's nearest threshold (0.65 / 0.85)
    """
    hdr = f"{'alert_id':<15} | {'verdict':<15} | {'score_range':<11} | {'margin':<6} | PASS/FAIL"
    print()
    print(hdr)
    for r in rows:
        verdict = (r["verdict"] or ",".join(v or "ERR" for v in r["unique_verdicts"]) or "ERR")
        sr = r["score_spread"]
        sr_str = " nan  " if isinstance(sr, float) and math.isnan(sr) else f"{sr:.4f}"
        mg = r["margin"]
        mg_str = " nan " if isinstance(mg, float) and math.isnan(mg) else f"{mg:.3f}"
        result = "PASS" if r["passed"] else "FAIL"
        print(f"{r['alert_id']:<15} | {verdict:<15} | {sr_str:<11} | {mg_str:<6} | {result}")

    total = len(rows)
    consistent = sum(1 for r in rows if r["verdict_consistent"])
    drift = sum(1 for r in rows if not r["score_consistent"])
    fragile = sum(1 for r in rows if r["fragile"])
    print()
    print(f"{consistent}/{total} consistent | {drift} score drift | {fragile} fragile")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verdict-consistency eval for HybridOrchestrator.")
    ap.add_argument("--alerts", type=int, default=6,
                    help="number of alerts to sample across the score range (default 6)")
    ap.add_argument("--runs", type=int, default=3,
                    help="how many times to run each alert (default 3)")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated explicit alert_ids (overrides --alerts sampling)")
    ap.add_argument("--json", type=str, default="",
                    help="optional path to write the full result as JSON")
    ap.add_argument("--format", choices=("default", "omkar"), default="default",
                    help="report style (default verbose table, or Omkar's compact format)")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (and not found in .env). "
              "Phase 1/3 call the Claude API.", file=sys.stderr)
        return 2

    if args.ids.strip():
        ids = [s.strip() for s in args.ids.split(",") if s.strip()]
        metas = alerts_by_ids(ids)
        missing = [m["alert_id"] for m in metas if m.get("_missing")]
        if missing:
            print(f"WARNING: alert_id(s) not found in sanctions_alerts: {missing}", file=sys.stderr)
    else:
        metas = select_alert_ids(args.alerts)

    if not metas:
        print("ERROR: no alerts available to evaluate. Is sanctions_alerts seeded?", file=sys.stderr)
        return 2

    print(f"Evaluating {len(metas)} alert(s), {args.runs} run(s) each "
          f"({len(metas) * args.runs} orchestrator runs)...")
    rows = []
    for i, meta in enumerate(metas, 1):
        print(f"  [{i}/{len(metas)}] {meta['alert_id']}  (match={meta['match_score']:.3f}) ...",
              flush=True)
        rows.append(evaluate_alert(meta, args.runs))

    if args.format == "omkar":
        print_report_omkar(rows)
    else:
        print_report(rows, args.runs)

    if args.json.strip():
        Path(args.json).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote JSON report -> {args.json}")

    all_pass = all(r["passed"] for r in rows)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
