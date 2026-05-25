"""
Pydantic worksheet model — the structured output an analyst sees.
Mirrors the older `worksheets/<id>.json` shape but is validated.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class SanctionsHit(BaseModel):
    full_name: str = ""
    program: str = ""
    source: str = ""
    nationality: str = ""
    listed_on: str = ""


class TransactionSummary(BaseModel):
    total: int = 0
    large_count: int = 0
    international_count: int = 0
    suspicious_pattern: bool = False


class PriorCaseSummary(BaseModel):
    total_cases: int = 0
    prior_clearances: int = 0
    prior_escalations: int = 0
    most_recent_resolution: str = "none"


class Worksheet(BaseModel):
    alert_id: str
    customer_id: str
    customer_name: str
    matched_entity: str
    source_list: str = ""
    initial_match_score: float = 0.0

    # Real data attachments
    sanctions_db_hits: list[SanctionsHit] = Field(default_factory=list)
    kyc_summary: dict[str, Any] = Field(default_factory=dict)
    transactions: TransactionSummary = Field(default_factory=TransactionSummary)
    adverse_media_count: int = 0
    ubo_chain_found: bool = False
    registry_match_count: int = 0
    prior_cases: PriorCaseSummary = Field(default_factory=PriorCaseSummary)

    # Scoring
    context_score: float = 0.0
    confidence_adjustment: float = 0.0
    final_risk_score: float = 0.0
    recommendation: str = "UNCERTAIN"  # FALSE_POSITIVE | TRUE_MATCH | UNCERTAIN

    narrative: str = ""
    narrative_citations: list[dict] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)
