"""Tests for the deterministic rule-based scoring (agent.py
HybridOrchestrator._phase2_rule_based_scoring).

Covers:
  - final score >= 0.85 yields TRUE_MATCH
  - prior clearances pull the score DOWN
  - adverse media pushes the score UP

The scorer is pure Python (no LLM/network), so we instantiate the
orchestrator and call the phase-2 method directly with synthetic
tool-result dicts.
"""
from agent import HybridOrchestrator


def make_tr(base=0.50, adverse_n=0, ubo=False,
            large_n=0, intl_n=0, esc_n=0, clear_n=0, reg_n=0):
    """Build a tool_results dict in the exact shape phase-2 reads."""
    return {
        "screening_api_lookup": {"alert": {"match_score": base}},
        "get_adverse_media": {"count": adverse_n},
        "get_ubo_chain": {"has_ubo_chain": ubo},
        "core_banking_get_customer": {
            "large_transaction_count": large_n,
            "international_transaction_count": intl_n,
        },
        "case_management_prior_cases": {
            "prior_escalations": esc_n,
            "prior_clearances": clear_n,
        },
        "get_company_registry": {"count": reg_n},
    }


orch = HybridOrchestrator()  # no progress_cb; scorer never touches the client


# ── 1. score >= 0.85 → TRUE_MATCH ─────────────────────────────────
def test_high_score_gives_true_match():
    sp = orch._phase2_rule_based_scoring(make_tr(base=0.90))
    assert sp["final_risk_score"] >= 0.85
    assert sp["recommendation"] == "TRUE_MATCH"


def test_exact_threshold_085_is_true_match():
    # base 0.85 with no context adjustments lands exactly on the boundary.
    sp = orch._phase2_rule_based_scoring(make_tr(base=0.85))
    assert sp["final_risk_score"] == 0.85
    assert sp["recommendation"] == "TRUE_MATCH"


def test_below_threshold_is_not_true_match():
    sp = orch._phase2_rule_based_scoring(make_tr(base=0.70))
    assert sp["recommendation"] != "TRUE_MATCH"  # 0.70 → UNCERTAIN band


# ── 2. prior clearances reduce the score ──────────────────────────
def test_prior_clearances_reduce_score():
    without = orch._phase2_rule_based_scoring(make_tr(base=0.80, clear_n=0))
    with_clr = orch._phase2_rule_based_scoring(make_tr(base=0.80, clear_n=4))

    assert with_clr["final_risk_score"] < without["final_risk_score"]
    assert with_clr["confidence_adjust"] < 0  # clearances apply a negative penalty


def test_clearance_penalty_is_capped():
    # clr_pen = -min(clear_n * 0.05, 0.20) — caps at -0.20 even for many clears.
    sp = orch._phase2_rule_based_scoring(make_tr(base=0.80, clear_n=99))
    assert sp["confidence_adjust"] == -0.20


# ── 3. adverse media increases the score ──────────────────────────
def test_adverse_media_increases_score():
    without = orch._phase2_rule_based_scoring(make_tr(base=0.50, adverse_n=0))
    with_am = orch._phase2_rule_based_scoring(make_tr(base=0.50, adverse_n=4))

    assert with_am["final_risk_score"] > without["final_risk_score"]
    assert with_am["context_score"] > 0


def test_adverse_media_contribution_is_capped():
    # adverse_c = min(adverse_n * 0.05, 0.20) — caps at +0.20.
    sp = orch._phase2_rule_based_scoring(make_tr(base=0.50, adverse_n=99))
    assert sp["context_score"] == 0.20
    assert sp["final_risk_score"] == 0.70  # 0.50 + 0.20, no other signals


# ── bonus: score is always clamped to [0, 1] ──────────────────────
def test_final_score_clamped_to_unit_interval():
    high = orch._phase2_rule_based_scoring(
        make_tr(base=0.99, adverse_n=9, ubo=True, large_n=9, intl_n=9, reg_n=9)
    )
    assert 0.0 <= high["final_risk_score"] <= 1.0
