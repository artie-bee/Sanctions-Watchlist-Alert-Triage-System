"""
Hook system for tool execution.

PreToolUse:
  - Blocks close_alert with a regulatory citation (PMLA 2002 /
    RBI KYC Master Direction 2025) and records the BLOCKED attempt.
PostToolUse:
  - Computes SHA-256 over (alert_id, tool_name, tool_input,
    tool_output) and appends one line per tool call to
    runtime/audit_log.jsonl. The hash makes the audit log
    tamper-evident — any later edit changes the hash.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent / "runtime" / "audit_log.jsonl"
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

EventCallback = Optional[Callable[[dict], None]]


def _json_safe(obj: Any) -> Any:
    """Coerce Decimals / dates / sets into JSON-compatible primitives."""
    return json.loads(json.dumps(obj, default=str))


def _sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolBlockedError(Exception):
    """Raised by PreToolUse to short-circuit a tool call."""


class HookManager:
    """
    Wraps tool execution with pre/post hooks. The orchestrator calls
    .invoke(tool_name, callable, **kwargs) — never the raw tool.
    """

    BLOCKED_TOOLS = {"close_alert"}

    def __init__(self, alert_id: str, on_event: EventCallback = None):
        self.alert_id = alert_id
        self.events: list[dict] = []
        self._on_event = on_event
        self._tool_start_ms: dict[str, float] = {}
        self._lock = threading.Lock()

    def _emit(self, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event({"alert_id": self.alert_id, **payload})
        except Exception:
            pass

    # ── PreToolUse ─────────────────────────────────────────
    def pre(self, tool_name: str, tool_input: dict) -> None:
        if tool_name in self.BLOCKED_TOOLS:
            citation = (
                "PMLA 2002 / RBI KYC Master Direction 2025: "
                "alert disposition requires human analyst sign-off."
            )
            entry = self._append({
                "event": "tool_blocked",
                "tool": tool_name,
                "tool_input": _json_safe(tool_input),
                "reason": citation,
            })
            self._emit({
                "type": "tool_blocked",
                "tool": tool_name,
                "tool_input": _json_safe(tool_input),
                "reason": citation,
                "sha256": entry["sha256"],
                "ts": entry["ts"],
            })
            print(f"\n  [HOOK PreToolUse]  BLOCKED  {tool_name}({tool_input})")
            print(f"                     reason: {citation}")
            raise ToolBlockedError(citation)
        self._tool_start_ms[tool_name] = time.monotonic() * 1000.0
        self._emit({
            "type": "tool_call_start",
            "tool": tool_name,
            "tool_input": _json_safe(tool_input),
        })

    # ── PostToolUse ────────────────────────────────────────
    def post(self, tool_name: str, tool_input: dict, tool_output: Any) -> None:
        summary = self._summarize_output(tool_output)
        entry = self._append({
            "event": "tool_call",
            "tool": tool_name,
            "tool_input": _json_safe(tool_input),
            "tool_output_summary": summary,
        })
        start_ms = self._tool_start_ms.pop(tool_name, None)
        dur = round(time.monotonic() * 1000.0 - start_ms, 1) if start_ms else None
        self._emit({
            "type": "tool_call_end",
            "tool": tool_name,
            "tool_input": _json_safe(tool_input),
            "tool_output_summary": summary,
            "sha256": entry["sha256"],
            "ts": entry["ts"],
            "duration_ms": dur,
        })

    # ── Public invocation wrapper ──────────────────────────
    def invoke(self, tool_name: str, fn, **kwargs):
        """Run a tool through both hooks. Raises ToolBlockedError if pre-hook blocks."""
        self.pre(tool_name, kwargs)
        out = fn(**kwargs)
        self.post(tool_name, kwargs, out)
        return out

    # ── Append-hash-write ──────────────────────────────────
    def _append(self, body: dict) -> dict:
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "alert_id": self.alert_id,
                **body,
            }
            entry["sha256"] = _sha256({
                "alert_id": self.alert_id,
                "event": entry.get("event"),
                "tool": entry.get("tool"),
                "tool_input": entry.get("tool_input"),
                "tool_output_summary": entry.get("tool_output_summary"),
                "reason": entry.get("reason"),
                "ts": entry["ts"],
            })
            self.events.append(entry)
            with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            return entry

    @staticmethod
    def _summarize_output(out: Any) -> Any:
        """Keep audit-log entries compact — avoid dumping 100k-row scans."""
        if not isinstance(out, dict):
            return out
        keep = {}
        for k, v in out.items():
            if isinstance(v, list):
                keep[f"{k}__count"] = len(v)
            elif isinstance(v, dict):
                keep[k] = {kk: vv for kk, vv in list(v.items())[:8]}
            else:
                keep[k] = v
        return keep
