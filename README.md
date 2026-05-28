# Sanctions Search

Bank-grade sanctions screening and AI triage system.

## What It Does
Pulls real sanctions lists from 8 regulators (OFAC, UN, EU, UK, MHA UAPA, SEBI) into a 66k-row SQLite database. Screens customer names through a 25-algorithm fuzzy match engine. Runs alerts through a 3-phase hybrid AI triage pipeline using Anthropic Claude Haiku (claude-haiku-4-5-20251001) for dynamic tool calling and narrative generation, rule-based scoring for reliable verdicts. Full audit trail with SHA-256 hashing.

## Tech Stack
- Python + FastAPI
- SQLite (sanctions.db — 66k entities)
- DynamoDB Local (Docker)
- Anthropic Claude Haiku (claude-haiku-4-5-20251001)
- 3-phase hybrid pipeline:
  - Phase 1: Claude dynamic tool calling
  - Phase 2: Rule-based scoring
  - Phase 3: Claude compliance narrative
- Pydantic
- BeautifulSoup + Playwright + pdfplumber

## Data Sources
- OFAC SDN List (USA)
- UN Consolidated List
- EU Sanctions List
- UK FCDO List
- MHA UAPA Banned Organisations (India)
- SEBI Debarred Entities (India)
- OpenSanctions

## How to Run Locally
1. pip install -r requirements.txt
2. set ANTHROPIC_API_KEY=sk-ant-your-key
3. docker run -d -p 8001:8000 amazon/dynamodb-local
4. python seed_data.py
5. uvicorn alert_intake:app --port 8005
6. cd sanctions_triage
   python run_batch.py

## Architecture
```
sanctions data (8 sources)
    ↓
sanctions.db (66k entities)
    ↓
fuzzy_match.py (25 algorithms)
    ↓
alert_intake.py (FastAPI :8005)
    ↓
HybridOrchestrator
  Phase 1: Claude dynamic tool calling
  Phase 2: Rule-based scoring → verdict
  Phase 3: Claude compliance narrative
    ↓
ComplianceWorksheet (Pydantic)
    ↓
Analyst final decision
```

## Compliance
PMLA 2002 | RBI KYC Master Direction 2025
PreToolUse hook blocks auto-close
SHA-256 audit trail on every tool call
