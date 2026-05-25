"""
Hook system for tool execution — SDK-compatible shapes, manual dispatch.

Uses claude-agent-sdk hook input TypedDicts (PreToolUseHookInput,
PostToolUseHookInput, StopHookInput, PreCompactHookInput). The
orchestrator drives dispatch by calling HookManager.invoke()/on_stop()/
on_pre_compact() — the SDK's own hook-firing machinery is not engaged
because tools are still dispatched directly from agent.py, not through
ClaudeSDKClient.query().

PreToolUse:
  - Blocks close_alert with a PMLA 2002 / RBI 2025 citation, writes
    a tool_blocked audit entry, then raises ToolBlockedError.
PostToolUse:
  - Computes SHA-256 over a canonical subset of the entry and appends
    one JSON line to runtime/audit_log.jsonl. Each entry is independently
    hashed (no chain) — matches the existing 3434-entry log shape.
Stop:
  - Orchestrator-driven: agent.py calls hook.on_stop() at the end of an
    alert run (or on crash) to record an alert_stop event.
PreCompact:
  - Orchestrator-driven: agent.py calls hook.on_pre_compact() before
    any context compaction (not used today; future hook).

SessionStart is not exposed by claude-agent-sdk 0.2.82 (Python).
Policy rules are loaded eagerly in HookManager.__init__ as a workaround.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import (
    PreToolUseHookInput,
    PostToolUseHookInput,
    StopHookInput,
    PreCompactHookInput,
)

AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent / "runtime" / "audit_log.jsonl"
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

EventCallback = Optional[Callable[[dict], None]]

# TODO: SessionStart hook isn't in claude-agent-sdk 0.2.82 (Python). When it
# lands, move this load into a SessionStart callback.
POLICY_RULES = {
    "blocked_tools": frozenset({"close_alert"}),
    "reason": (
        "PMLA 2002 / RBI KYC Master Direction 2025: "
        "alert disposition requires human analyst sign-off."
    ),
}


class ToolBlockedError(Exception):
    """Raised when the PreToolUse callback returns decision=block."""


def _json_safe(obj: Any) -> Any:
    """Coerce Decimals / dates / sets into JSON-compatible primitives."""
    return json.loads(json.dumps(obj, default=str))


def _sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


class HookManager:
    """SDK-shape callbacks, orchestrator-driven dispatch.

    invoke() / on_stop() / on_pre_compact() build SDK TypedDict inputs,
    call the registered callbacks, and translate return values back
    into orchestrator-expected behaviour (raise / return / no-op).
    """

    def __init__(self, alert_id: str, on_event: EventCallback = None):
        self.alert_id = alert_id
        self._on_event = on_event
        self._tool_start_ms: dict[str, float] = {}  # keyed by tool_use_id
        self.policy = POLICY_RULES

    def _base_hook_fields(self) -> dict[str, str]:
        # BaseHookInput-required keys. Synthesised — manual dispatch has no
        # real SDK session/transcript.
        return {
            "session_id": f"manual-{self.alert_id}",
            "transcript_path": "",
            "cwd": os.getcwd(),
        }

    def _emit(self, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event({"alert_id": self.alert_id, **payload})
        except Exception:
            pass

    # ── SDK-shape callbacks ────────────────────────────────────────
    def _pre_tool_use(self, hook_input: PreToolUseHookInput) -> dict[str, Any]:
        """Return {'decision': 'block', 'reason': ...} to deny, or {} to allow."""
        if hook_input["tool_name"] in self.policy["blocked_tools"]:
            return {"decision": "block", "reason": self.policy["reason"]}
        return {}

    def _post_tool_use(self, hook_input: PostToolUseHookInput) -> dict:
        return self._append({
            "event": "tool_call",
            "tool": hook_input["tool_name"],
            "tool_input": _json_safe(hook_input["tool_input"]),
            "tool_output_summary": _summarize_output(hook_input["tool_response"]),
        })

    def _stop(self, hook_input: StopHookInput) -> dict:
        return self._append({"event": "alert_stop"})

    def _pre_compact(self, hook_input: PreCompactHookInput) -> None:
        if AUDIT_LOG_PATH.exists():
            snap = AUDIT_LOG_PATH.parent / f"audit_snapshot_{self.alert_id}_{int(time.time())}.jsonl"
            shutil.copy(AUDIT_LOG_PATH, snap)

    # ── Orchestrator-facing API ────────────────────────────────────
    def invoke(self, tool_name: str, fn: Callable, **kwargs) -> Any:
        """Run a tool through Pre/PostToolUse. Raises ToolBlockedError on block."""
        tool_use_id = f"{self.alert_id}-{tool_name}-{int(time.monotonic() * 1e6)}"
        tool_input = dict(kwargs)

        pre_input: PreToolUseHookInput = {
            **self._base_hook_fields(),
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
        }
        pre_result = self._pre_tool_use(pre_input)

        if pre_result.get("decision") == "block":
            reason = pre_result.get("reason", "Blocked by policy")
            entry = self._append({
                "event": "tool_blocked",
                "tool": tool_name,
                "tool_input": _json_safe(tool_input),
                "reason": reason,
            })
            self._emit({
                "type": "tool_blocked",
                "tool": tool_name,
                "tool_input": _json_safe(tool_input),
                "reason": reason,
                "sha256": entry["sha256"],
                "ts": entry["ts"],
            })
            print(f"\n  [HOOK PreToolUse]  BLOCKED  {tool_name}({tool_input})")
            print(f"                     reason: {reason}")
            raise ToolBlockedError(reason)

        self._tool_start_ms[tool_use_id] = time.monotonic() * 1000.0
        self._emit({
            "type": "tool_call_start",
            "tool": tool_name,
            "tool_input": _json_safe(tool_input),
        })

        out = fn(**kwargs)

        post_input: PostToolUseHookInput = {
            **self._base_hook_fields(),
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": out,
            "tool_use_id": tool_use_id,
        }
        entry = self._post_tool_use(post_input)

        start_ms = self._tool_start_ms.pop(tool_use_id, None)
        dur = round(time.monotonic() * 1000.0 - start_ms, 1) if start_ms else None
        self._emit({
            "type": "tool_call_end",
            "tool": tool_name,
            "tool_input": _json_safe(tool_input),
            "tool_output_summary": entry["tool_output_summary"],
            "sha256": entry["sha256"],
            "ts": entry["ts"],
            "duration_ms": dur,
        })
        return out

    def on_stop(self) -> dict:
        stop_input: StopHookInput = {
            **self._base_hook_fields(),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        return self._stop(stop_input)

    def on_pre_compact(self) -> None:
        pre_compact_input: PreCompactHookInput = {
            **self._base_hook_fields(),
            "hook_event_name": "PreCompact",
            "trigger": "manual",
            "custom_instructions": None,
        }
        self._pre_compact(pre_compact_input)

    # ── Append-hash-write (audit log) ──────────────────────────────
    def _append(self, body: dict) -> dict:
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
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry
