# Sanctions Search

**Bank-grade sanctions screening and AI triage system.**

---

## 1. What problem does this solve?

Banks are legally required to check every customer against government **sanctions lists** —
lists of people, companies, and organisations that they are not allowed to do business with
(terrorists, sanctioned officials, debarred entities, etc.).

The hard part isn't *finding* exact matches — it's the **false alarms**. A customer named
"John Smith" or "Priya Sharma" will match many sanctioned names by coincidence. Each of these
"hits" (called an **alert**) traditionally has to be reviewed by a human compliance analyst,
who digs through KYC records, transactions, news, and prior decisions to decide:

- **False positive** → same name, different person → close it.
- **True match** → this really is the sanctioned entity → escalate it.
- **Uncertain** → not enough evidence → needs a closer human look.

This project automates the **investigation and recommendation** part of that work, while keeping
a human firmly in charge of the final decision. It pulls real sanctions data, screens names, and
runs each alert through an AI-assisted pipeline that gathers evidence, scores the risk, and writes
a plain-English explanation — all with a full, tamper-evident audit trail.

> **Important:** the AI *recommends*; it can **never** close or dispose of an alert by itself.
> A human analyst always makes the final call (enforced in code — see [Compliance & safety](#9-compliance--safety)).

### Quick glossary (for newcomers)
| Term | Meaning |
|---|---|
| **Sanctions list** | Government list of entities you're forbidden to transact with (e.g. OFAC SDN). |
| **Alert** | A potential match between a customer and a sanctioned entity that needs review. |
| **KYC** | "Know Your Customer" — the identity/profile data a bank holds on a customer. |
| **UBO** | "Ultimate Beneficial Owner" — the real person who ultimately owns/controls a company. |
| **False positive** | An alert that turns out *not* to be the sanctioned entity. |
| **Triage** | Reviewing an alert and deciding what to do with it. |

---

## 2. Key components

| Component | File / folder | What it does |
|---|---|---|
| **Sanctions data engine** | `sanction_engine.py` | Automatically downloads sanctions lists from 8 sources and stores ~66,000 entities in a local SQLite database (`sanctions.db`). |
| **Fuzzy-match engine** | `fuzzy_match.py` | Compares a customer name to the sanctions database using 25 name-matching algorithms (handles typos, transliteration, nicknames). *Library module — see note in [How it works](#3-how-it-works-end-to-end).* |
| **Intake API** | `alert_intake.py` (FastAPI, port **8005**) | The REST API for creating alerts, listing customers, and recording the analyst's final decision. Stores data in DynamoDB. |
| **Data store** | DynamoDB Local (Docker, port **8001**) | Holds alerts, KYC records, transactions, adverse media, company registry, and ownership chains (6 tables). |
| **AI triage engine** | `sanctions_triage/src/agent.py` (`HybridOrchestrator`) | The brain. Runs each alert through a 3-phase pipeline (gather evidence → score → explain). |
| **Scoring engine** | inside `agent.py` (Phase 2) | A deterministic, rule-based risk score → produces the verdict. No AI randomness here. |
| **Safety hook** | `sanctions_triage/src/hooks.py` | Blocks the AI from auto-closing alerts and writes a SHA-256-hashed audit log of every action. |
| **Visualisation dashboards** | `workflow_ui.py` (FastAPI, port **7000**) | Three live web dashboards that show the pipeline running in real time. |
| **Evaluation system** | `tests/evals/` | Automated checks that prove the system is consistent, safe, and trustworthy (see [Evaluations](#8-evaluations--why-they-matter)). |
| **Seed script** | `seed_data.py` | Fills the database with realistic demo data so you can try the system immediately. |

---

## 3. How it works (end-to-end)

When an alert comes in, it flows through these stages:

```
Alert (customer matched a sanctioned name)
   │
   ▼
Phase 1 — GATHER EVIDENCE   (Claude Haiku calls 6 "tools")
   • screening_api_lookup        → fetch the alert + sanctions DB hits
   • core_banking_get_customer   → KYC + recent transactions
   • get_adverse_media           → negative news about the person
   • get_company_registry        → corporate records
   • get_ubo_chain               → ownership structure
   • case_management_prior_cases → how similar alerts were resolved before
   │  (a "defensive fill" safety net re-runs any tool the AI skipped)
   ▼
Phase 2 — SCORE THE RISK    (plain rules, no AI — fully repeatable)
   final_score = match_score
               + adverse media, large/international transactions, UBO, registry hits
               − prior clearances (this name was cleared before)
   verdict:  ≥ 0.85 → TRUE_MATCH      (escalate)
             ≥ 0.65 → UNCERTAIN       (needs human review)
             < 0.65 → FALSE_POSITIVE  (recommend close)
   ▼
Phase 3 — EXPLAIN           (Claude Haiku writes a compliance narrative)
   • Plain-English summary of who the customer is, what matched,
     what supports or contradicts the match, and the next steps.
   • Cites its evidence with [cite:…] markers.
   ▼
ComplianceWorksheet  (structured result: verdict + score + narrative + evidence)
   ▼
Human analyst reviews it and makes the FINAL decision (CLEARED / ESCALATED)
```

**Why a "hybrid" design?** The AI is great at *gathering* messy evidence (Phase 1) and *explaining*
it in human language (Phase 3), but you don't want an AI inventing risk scores. So the **verdict
itself is decided by fixed rules (Phase 2)** — meaning the same alert always produces the same
score, which is exactly what auditors and regulators require.

> **Note on `fuzzy_match.py`:** the 25-algorithm engine is a ready-to-use library. The live agent
> currently uses a simpler SQL `LIKE` search in `screening_api_lookup`; swapping in `screen_fuzzy()`
> is the intended upgrade path.

---

## 4. The 3-phase pipeline at a glance

| Phase | Who runs it | Output | Deterministic? |
|---|---|---|---|
| **1. Tool calling** | Claude Haiku (`claude-haiku-4-5-20251001`) | Evidence from 6 data sources | No (LLM, temp 0.0) |
| **2. Rule-based scoring** | Plain Python | Risk score + verdict | **Yes** |
| **3. Narrative** | Claude Haiku (streamed) | Cited, plain-English explanation | No (LLM, temp 0.3) |

---

## 5. Tech stack

- Python + FastAPI
- SQLite (`sanctions.db` — 66k entities)
- DynamoDB Local (Docker)
- Anthropic Claude Haiku (`claude-haiku-4-5-20251001`)
- 3-phase hybrid pipeline:
  - Phase 1: Claude dynamic tool calling
  - Phase 2: Rule-based scoring
  - Phase 3: Claude compliance narrative
- Pydantic (data validation / structured worksheet)
- BeautifulSoup + Playwright + pdfplumber (for scraping sanctions sources)

---

## 6. Data sources

The sanctions engine pulls from 8 sources:

- OFAC SDN List (USA)
- UN Consolidated List
- EU Sanctions List
- UK FCDO List
- MHA UAPA Banned Organisations (India)
- SEBI Debarred Entities (India)
- OpenSanctions
- RBI / MCA disqualified directors (India)

---

## 7. How to run it locally

You need **Python 3.11+** and **Docker** installed, plus an Anthropic API key.

### Option A — Docker Compose (easiest, runs everything)

```bash
# 1. Set your API key (create a .env file from the template)
cp .env.example .env        # then edit .env and add your ANTHROPIC_API_KEY

# 2. Start all services (DynamoDB :8001, API :8005, UI :7000)
docker-compose up -d

# 3. Load demo data into DynamoDB
python seed_data.py

# 4. Open the dashboards
#    Intake API docs : http://localhost:8005/docs
#    Live dashboards : http://localhost:7000/workflow
#                      http://localhost:7000/observability
#                      http://localhost:7000/simulator
```

### Option B — Manual (run pieces individually)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Provide your Anthropic API key
set ANTHROPIC_API_KEY=sk-ant-your-key        # Windows
# export ANTHROPIC_API_KEY=sk-ant-your-key   # macOS/Linux

# 3. Start DynamoDB Local
docker run -d -p 8001:8000 amazon/dynamodb-local

# 4. Seed demo data
python seed_data.py

# 5. Start the intake API
uvicorn alert_intake:app --port 8005

# 6. (Optional) run a batch of alerts through the triage engine
cd sanctions_triage
python run_batch.py
```

> **Tip:** the system is built to degrade gracefully. If the Anthropic API is unavailable
> (e.g. no credits), Phase 1 falls back to the "defensive fill" path and Phase 3 produces a
> short fallback note — the deterministic verdict (Phase 2) still works.

---

## 8. Evaluations — why they matter

In a compliance system, "it usually works" isn't good enough — you have to be able to **prove**
it behaves correctly. The `tests/evals/` folder contains automated evaluations that do exactly
that. Each can be run on its own (`python tests/evals/<name>.py`).

| Eval | Question it answers | Why it matters |
|---|---|---|
| **`eval_verdict_consistency.py`** | Does the same alert always produce the same verdict and score? | Regulators require repeatable, explainable decisions. |
| **`eval_block_rate.py`** | Can the AI ever auto-close an alert? (It must not.) | Proves the human-in-the-loop safeguard actually holds — 100% of close attempts are blocked. |
| **`eval_fill_rate.py`** | How often does the AI gather evidence itself vs. needing the safety net? Are all 6 tools always covered? | Measures the AI's self-sufficiency and guarantees full evidence coverage before any verdict. |
| **`eval_narrative_quality.py`** | Is the AI's explanation trustworthy — no made-up citations, grounded in regulation, numbers consistent? | Stops "hallucinated" or misleading narratives from reaching an analyst. |

There are also fast **unit tests** in `tests/unit/` (scoring rules, the safety hook, the worksheet
schema). Run them with:

```bash
pytest tests/unit/ -v
```

---

## 9. Compliance & safety

This system is designed around Indian banking regulation — **PMLA 2002** and the
**RBI KYC Master Direction 2025**.

Two guarantees are enforced **in code**, not just by policy:

1. **The AI cannot dispose of alerts.** A `PreToolUse` hook blocks the `close_alert` action every
   time it's attempted — only a human analyst can mark an alert CLEARED or ESCALATED.
2. **Everything is auditable.** Every tool call is written to an append-only audit log with a
   **SHA-256 hash**, so the full investigation trail is tamper-evident.

---

## 10. Architecture

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

---

## 11. Repository layout (where to look)

```
.
├── sanction_engine.py        # downloads + stores sanctions lists (sanctions.db)
├── fuzzy_match.py            # 25-algorithm name-matching library
├── seed_data.py              # generates demo data in DynamoDB
├── alert_intake.py           # REST API (:8005) — alerts, customers, dispositions
├── workflow_ui.py            # dashboards (:7000) — workflow / observability / simulator
├── docker-compose.yml        # runs DynamoDB + API + UI together
├── sanctions_triage/
│   ├── src/
│   │   ├── agent.py          # HybridOrchestrator (the 3-phase pipeline)
│   │   ├── tools.py          # the 6 evidence tools + blocked close_alert
│   │   ├── hooks.py          # PreToolUse block + SHA-256 audit log
│   │   ├── worksheet.py      # Pydantic result model
│   │   ├── memory.py         # remembers prior false-positive clearances
│   │   ├── db.py             # DynamoDB + SQLite connections
│   │   └── mcp_servers/      # expose the tools to Claude Desktop (MCP)
│   ├── run_batch.py          # run a batch of alerts
│   └── data/prior_cases.json # historical analyst decisions
├── tests/
│   ├── unit/                 # fast deterministic tests (scoring, hooks, schema)
│   └── evals/                # consistency / safety / quality evaluations
├── templates/ + static/      # the dashboard front-ends
└── README.md
```
