"""Cross-alert clearance memory.

Append-only JSONL store at sanctions_triage/runtime/cleared_entities.jsonl.
Step 7b — read path feeds Phase 3 narrative/citations only (never Phase 2),
so there is no feedback loop into the rule-based score.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

MEMORY_PATH = Path(__file__).resolve().parent.parent / "runtime" / "cleared_entities.jsonl"
FUZZY_THRESHOLD = 0.80


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def lookup_cleared_entries(
    customer_name: str,
    dob: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    """Return cleared-entity entries matching the given (name, dob).

    Match rule:
      - Fuzzy name match >= FUZZY_THRESHOLD
      - AND if BOTH this alert's dob AND the entry's dob are non-empty,
        they must match exactly. If either side is missing dob, fall back
        to name-only match.

    Returns most-recent-first, capped at `limit`.
    """
    if not MEMORY_PATH.exists():
        return []
    matches: list[dict] = []
    with MEMORY_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            name_score = _fuzzy(entry.get("customer_name", ""), customer_name)
            if name_score < FUZZY_THRESHOLD:
                continue
            entry_dob = entry.get("dob") or ""
            if dob and entry_dob and entry_dob != dob:
                continue
            entry["_match_score"] = round(name_score, 3)
            matches.append(entry)
    matches.sort(key=lambda e: e.get("resolved_at", ""), reverse=True)
    return matches[:limit]


def append_cleared_entry(
    *,
    customer_name: str,
    customer_id: str,
    dob: Optional[str],
    nationality: Optional[str],
    alert_id: str,
    final_risk_score: float,
    source: str = "auto",
) -> None:
    """Append a FALSE_POSITIVE clearance to the memory store."""
    entry = {
        "customer_name": customer_name,
        "customer_id": customer_id,
        "dob": dob or "",
        "nationality": nationality or "",
        "alert_id": alert_id,
        "final_risk_score": float(final_risk_score),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "requires_review": (source == "auto"),
    }
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEMORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
