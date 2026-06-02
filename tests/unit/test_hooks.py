"""Tests for the PreToolUse/PostToolUse hook system (hooks.py).

Covers:
  - PreToolUse blocks close_alert (raises ToolBlockedError, tool fn never runs)
  - The audit log gets a JSON line written on a successful tool call
  - The SHA-256 hash on each entry matches the canonical-subset recompute

The module-level AUDIT_LOG_PATH is monkeypatched to a tmp file so these
tests never touch the real runtime/audit_log.jsonl.
"""
import json

import pytest

import hooks
from hooks import HookManager, ToolBlockedError, _sha256


@pytest.fixture
def hm(tmp_path, monkeypatch):
    """A HookManager whose audit log writes to an isolated temp file."""
    audit = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(hooks, "AUDIT_LOG_PATH", audit)
    manager = HookManager(alert_id="TEST-ALERT-001")
    manager._audit_path = audit  # convenience handle for assertions
    return manager


# ── 1. PreToolUse blocks close_alert ──────────────────────────────
def test_pretooluse_blocks_close_alert(hm):
    """close_alert must be denied before the tool function ever runs."""
    ran = {"called": False}

    def fake_close_alert(**kwargs):
        ran["called"] = True
        return {"ok": True}

    with pytest.raises(ToolBlockedError) as exc:
        hm.invoke(
            "close_alert", fake_close_alert,
            alert_id="TEST-ALERT-001", disposition="FALSE_POSITIVE",
        )

    # The underlying tool must NOT have executed.
    assert ran["called"] is False
    # The block reason cites the policy (PMLA / RBI).
    assert "PMLA" in str(exc.value)


def test_blocked_call_writes_tool_blocked_audit_entry(hm):
    """A blocked call still leaves a tamper-evident tool_blocked record."""
    with pytest.raises(ToolBlockedError):
        hm.invoke("close_alert", lambda **kw: None,
                  alert_id="TEST-ALERT-001", disposition="TRUE_MATCH")

    lines = hm._audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "tool_blocked"
    assert entry["tool"] == "close_alert"
    assert entry["alert_id"] == "TEST-ALERT-001"
    assert "sha256" in entry


# ── 2. Audit log gets written on a normal (allowed) tool call ──────
def test_audit_log_written_on_successful_tool(hm):
    out = hm.invoke(
        "screening_api_lookup",
        lambda **kw: {"hit_count": 3, "sanctions_db_hits": [1, 2, 3]},
        alert_id="TEST-ALERT-001",
    )
    # Tool output is returned unchanged to the caller.
    assert out == {"hit_count": 3, "sanctions_db_hits": [1, 2, 3]}

    lines = hm._audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # exactly one PostToolUse entry; PreToolUse allow writes nothing
    entry = json.loads(lines[0])
    assert entry["event"] == "tool_call"
    assert entry["tool"] == "screening_api_lookup"
    assert entry["alert_id"] == "TEST-ALERT-001"
    assert "ts" in entry and "sha256" in entry


# ── 3. SHA-256 hash is correct ─────────────────────────────────────
def test_sha256_matches_canonical_subset(hm):
    """Recomputing the hash over the same canonical subset must match
    the stored sha256 — proves the entry is internally consistent."""
    hm.invoke(
        "get_adverse_media",
        lambda **kw: {"count": 2, "records": ["r1", "r2"]},
        customer_name="John Doe",
    )
    entry = json.loads(
        hm._audit_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    )

    expected = _sha256({
        "alert_id": "TEST-ALERT-001",
        "event": entry.get("event"),
        "tool": entry.get("tool"),
        "tool_input": entry.get("tool_input"),
        "tool_output_summary": entry.get("tool_output_summary"),
        "reason": entry.get("reason"),
        "ts": entry["ts"],
    })
    assert entry["sha256"] == expected
    assert len(entry["sha256"]) == 64  # hex SHA-256 is 64 chars


def test_sha256_detects_tampering(hm):
    """If a field is altered, the recomputed hash should no longer match."""
    hm.invoke("get_company_registry", lambda **kw: {"count": 1}, name="ACME")
    entry = json.loads(
        hm._audit_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    )

    tampered = _sha256({
        "alert_id": "TEST-ALERT-001",
        "event": entry.get("event"),
        "tool": "DIFFERENT_TOOL",  # tampered field
        "tool_input": entry.get("tool_input"),
        "tool_output_summary": entry.get("tool_output_summary"),
        "reason": entry.get("reason"),
        "ts": entry["ts"],
    })
    assert entry["sha256"] != tampered
