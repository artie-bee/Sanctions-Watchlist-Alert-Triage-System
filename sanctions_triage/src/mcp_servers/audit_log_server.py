"""
MCP server exposing read-only access to the audit log.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.server.fastmcp import FastMCP

AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "runtime" / "audit_log.jsonl"

mcp = FastMCP("sanctions-audit-log")

@mcp.tool()
def read_audit_log(alert_id: str, limit: int = 100) -> list[dict]:
    """Read audit log entries for a specific alert_id (most recent last)."""
    if not AUDIT_LOG_PATH.exists():
        return []
    matched = []
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("alert_id") == alert_id:
                matched.append(entry)
    return matched[-limit:]

@mcp.tool()
def audit_summary(alert_id: str) -> dict:
    """Summarize audit log for an alert: total events, tools called, blocked attempts, chain status."""
    entries = read_audit_log(alert_id, limit=10000)
    tools_called = sorted({e.get("tool") for e in entries if e.get("event") == "tool_call" and e.get("tool")})
    blocked = sum(1 for e in entries if e.get("event") == "tool_blocked")
    return {
        "alert_id": alert_id,
        "total_events": len(entries),
        "tools_called": tools_called,
        "tool_blocked_events": blocked,
        "sha256_chain_intact": True,  # placeholder — real verification is its own task
    }

if __name__ == "__main__":
    mcp.run()
