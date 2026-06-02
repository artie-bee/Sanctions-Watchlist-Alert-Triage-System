"""Tests for the Worksheet Pydantic model (worksheet.py).

Covers:
  - A worksheet with the required fields validates and applies defaults
  - Required fields (no default) are enforced — omitting one raises
"""
import pytest
from pydantic import ValidationError

from worksheet import (
    PriorCaseSummary,
    SanctionsHit,
    TransactionSummary,
    Worksheet,
)

# The four fields with no default on Worksheet.
REQUIRED = ("alert_id", "customer_id", "customer_name", "matched_entity")


def minimal_kwargs():
    return {
        "alert_id": "ALR-TEST-0001",
        "customer_id": "CUST-0001",
        "customer_name": "John Doe",
        "matched_entity": "BAD ACTOR, John",
    }


# ── 1. Pydantic validates correctly ───────────────────────────────
def test_minimal_worksheet_validates_and_applies_defaults():
    ws = Worksheet(**minimal_kwargs())

    # Provided fields round-trip.
    assert ws.alert_id == "ALR-TEST-0001"
    assert ws.customer_name == "John Doe"

    # Defaults are applied for everything else.
    assert ws.recommendation == "UNCERTAIN"
    assert ws.final_risk_score == 0.0
    assert ws.source_list == ""
    assert ws.sanctions_db_hits == []
    assert ws.kyc_summary == {}
    assert ws.blocked_actions == []
    # Nested model defaults are real instances, not shared mutables.
    assert isinstance(ws.transactions, TransactionSummary)
    assert isinstance(ws.prior_cases, PriorCaseSummary)


def test_nested_models_and_coercion():
    ws = Worksheet(
        **minimal_kwargs(),
        initial_match_score="0.83",  # str coerced to float
        sanctions_db_hits=[{"full_name": "BAD ACTOR, John", "source": "OFAC"}],
        transactions={"total": 10, "large_count": 2},
        prior_cases={"prior_clearances": 5},
        recommendation="TRUE_MATCH",
    )
    assert ws.initial_match_score == 0.83
    assert isinstance(ws.sanctions_db_hits[0], SanctionsHit)
    assert ws.sanctions_db_hits[0].source == "OFAC"
    assert ws.transactions.large_count == 2
    assert ws.prior_cases.prior_clearances == 5

    # Pydantic round-trips through JSON cleanly (used by the SSE layer).
    assert Worksheet.model_validate_json(ws.model_dump_json()) == ws


# ── 2. Required fields are enforced ───────────────────────────────
@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_field_raises(missing):
    kwargs = minimal_kwargs()
    kwargs.pop(missing)
    with pytest.raises(ValidationError) as exc:
        Worksheet(**kwargs)
    # The error names the field that was missing.
    assert missing in str(exc.value)


def test_wrong_type_for_score_raises():
    kwargs = minimal_kwargs()
    kwargs["final_risk_score"] = "not-a-number"
    with pytest.raises(ValidationError):
        Worksheet(**kwargs)
