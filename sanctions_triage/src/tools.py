"""
Tool functions for the sanctions-triage agent.

Every tool hits LIVE data:
  - DynamoDB Local on port 8001  (sanctions_alerts, customer_kyc,
    customer_transactions, adverse_media_records, company_registry,
    ubo_ownership_chains)
  - sanctions.db SQLite           (real sanctions feed, 66k rows)

The only file-backed tool is case_management_prior_cases() which
reads prior_cases.json — that data is not in DynamoDB.

close_alert() exists solely so the PreToolUse hook can BLOCK it.
"""
from __future__ import annotations

import difflib
import json
import sys
from decimal import Decimal
from pathlib import Path

from boto3.dynamodb.conditions import Attr, Key

# Make `from db import ...` work when a script runs this module directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_table, get_sanctions_db  # noqa: E402


def _f(x, default=0.0):
    """Decimal/str/None → float without raising."""
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def sanctions_db_search(query: str, limit: int = 10) -> list[dict]:
    """
    Search the sanctions.db SQLite table by full_name LIKE %query%.
    Returns list of hit dicts: id, full_name, nationality, program, source, listed_on, raw_data.
    Used by screening_api_lookup AND the mcp-sanctions-db server.
    """
    conn = get_sanctions_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, full_name, nationality, program, source, listed_on, raw_data
            FROM sanctions
            WHERE full_name LIKE ?
            LIMIT ?
            """,
            (f"%{query}%", limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Tool 1 ── screening API + real sanctions.db hit ────────────────
def screening_api_lookup(alert_id: str) -> dict:
    """
    Fetch alert from DynamoDB sanctions_alerts; then look up the
    matched entity name in the real sanctions.db (66k rows).
    """
    alerts = get_table("sanctions_alerts")
    alert = alerts.get_item(Key={"alert_id": alert_id}).get("Item")
    if not alert:
        raise ValueError(f"Alert {alert_id} not found in DynamoDB sanctions_alerts")

    entity_name = (
        alert.get("matched_entity")
        or alert.get("entity_name")
        or alert.get("customer_name")
        or ""
    )

    db_hits = sanctions_db_search(entity_name, limit=10)

    print(f"\n  [Tool: screening_api_lookup]")
    print(f"    Alert ID    : {alert_id}")
    print(f"    Entity name : {entity_name}")
    print(f"    DB hits     : {len(db_hits)} real records in sanctions.db")
    if db_hits:
        h = db_hits[0]
        print(f"    Top hit     : {h['full_name']}  [{h['source']} / {h['program']}]")

    return {
        "alert": dict(alert),
        "entity_name": entity_name,
        "sanctions_db_hits": db_hits,
        "hit_count": len(db_hits),
    }


# ── Tool 2 ── KYC + transactions ──────────────────────────────────
def core_banking_get_customer(customer_id: str) -> dict:
    """
    Pull full KYC from DynamoDB customer_kyc, then the last 10
    transactions from customer_transactions (which has a composite
    PK [transaction_id, customer_id] — we Scan with FilterExpression
    because there's no GSI on customer_id alone).
    """
    kyc_t = get_table("customer_kyc")
    customer = kyc_t.get_item(Key={"customer_id": customer_id}).get("Item")
    if not customer:
        raise ValueError(f"Customer {customer_id} not found in DynamoDB customer_kyc")

    # DynamoDB `Limit` applies BEFORE FilterExpression, so with 100k
    # rows across 1k customers a single 200-row scan finds almost
    # nothing. Paginate until we have 10 matches or run out of pages.
    txn_t = get_table("customer_transactions")
    transactions: list[dict] = []
    scan_kwargs: dict = {
        "FilterExpression": Attr("customer_id").eq(customer_id),
    }
    pages = 0
    while len(transactions) < 50 and pages < 30:  # cap on pages
        resp = txn_t.scan(**scan_kwargs)
        transactions.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last
        pages += 1
    # Sort newest first; cap at 10
    transactions.sort(key=lambda t: t.get("date", ""), reverse=True)
    transactions = transactions[:10]

    amounts = [_f(t.get("amount")) for t in transactions]
    large_txns = [a for a in amounts if a > 500_000]

    flag_words = (
        "international", "wire", "swift", "foreign", "uae", "dubai",
        "turkey", "hawala", "western union", "payoneer",
    )
    intl_count = 0
    for t in transactions:
        blob = (
            str(t.get("counterparty", ""))
            + " " + str(t.get("category", ""))
            + " " + str(t.get("country", ""))
        ).lower()
        if any(w in blob for w in flag_words):
            intl_count += 1

    print(f"\n  [Tool: core_banking_get_customer]")
    print(f"    Customer ID   : {customer_id}")
    print(f"    Name          : {customer.get('full_name', 'N/A')}")
    print(f"    Risk rating   : {customer.get('risk_rating', 'N/A')}")
    print(f"    Transactions  : {len(transactions)}")
    print(f"    Large (>5L)   : {len(large_txns)}")
    print(f"    Intl/wire     : {intl_count}")

    return {
        "kyc": dict(customer),
        "transactions": [dict(t) for t in transactions],
        "transaction_count": len(transactions),
        "large_transaction_count": len(large_txns),
        "international_transaction_count": intl_count,
        "suspicious_pattern": len(large_txns) > 1 or intl_count > 0,
    }


# ── Tool 3 ── adverse media (linked by person_name, not customer_id) ─
def get_adverse_media(customer_id: str, customer_name: str = "") -> dict:
    """
    adverse_media_records has NO customer_id column. The link is via
    person_name. We accept customer_name as a hint and fall back to
    looking up the KYC full_name when not supplied.
    """
    if not customer_name:
        try:
            kyc = get_table("customer_kyc").get_item(
                Key={"customer_id": customer_id}
            ).get("Item") or {}
            customer_name = kyc.get("full_name", "")
        except Exception:
            customer_name = ""

    records = []
    if customer_name:
        try:
            resp = get_table("adverse_media_records").scan(
                FilterExpression=Attr("person_name").contains(customer_name),
                Limit=20,
            )
            records = resp.get("Items", [])
        except Exception:
            records = []

    print(f"\n  [Tool: get_adverse_media]")
    print(f"    Customer    : {customer_id}  ({customer_name})")
    print(f"    Media hits  : {len(records)}")

    return {
        "customer_id": customer_id,
        "person_name": customer_name,
        "records": [dict(r) for r in records],
        "count": len(records),
        "has_adverse_media": len(records) > 0,
    }


# ── Tool 4 ── company registry (corporate connections) ────────────
def get_company_registry(entity_name: str) -> dict:
    """
    company_registry has company_name + person_name (director-ish).
    We look up rows where either field contains the entity_name.
    """
    table = get_table("company_registry")
    matches = []
    try:
        resp = table.scan(
            FilterExpression=(
                Attr("company_name").contains(entity_name)
                | Attr("person_name").contains(entity_name)
            ),
            Limit=10,
        )
        matches = resp.get("Items", [])
    except Exception:
        matches = []

    print(f"\n  [Tool: get_company_registry]")
    print(f"    Entity      : {entity_name}")
    print(f"    Registry    : {len(matches)} match(es)")

    return {
        "entity_name": entity_name,
        "registry_hits": [dict(m) for m in matches],
        "count": len(matches),
    }


# ── Tool 5 ── UBO chain (linked by entity_name, not customer_id) ──
def get_ubo_chain(customer_id: str, entity_name: str = "") -> dict:
    """
    ubo_ownership_chains has NO customer_id — entries link by
    entity_name (sanctioned-entity-side). We try entity_name; if
    no hit, skip silently for individuals.
    """
    table = get_table("ubo_ownership_chains")
    chains = []
    if entity_name:
        try:
            resp = table.scan(
                FilterExpression=Attr("entity_name").contains(entity_name),
                Limit=5,
            )
            chains = resp.get("Items", [])
        except Exception:
            chains = []

    print(f"\n  [Tool: get_ubo_chain]")
    print(f"    Customer    : {customer_id}")
    print(f"    Entity      : {entity_name or '(individual — skipped)'}")
    print(f"    UBO chain   : {'found' if chains else 'none'}")

    return {
        "customer_id": customer_id,
        "entity_name": entity_name,
        "chains": [dict(c) for c in chains],
        "chain_count": len(chains),
        "has_ubo_chain": len(chains) > 0,
    }


# ── Tool 6 ── prior cases (the only JSON-backed tool) ─────────────
PRIOR_CASES_PATH = Path(__file__).resolve().parent.parent / "data" / "prior_cases.json"


def case_management_prior_cases(name: str) -> dict:
    """
    Fuzzy lookup against prior_cases.json (case history is the only
    data the bank does NOT keep in DynamoDB in this prototype).
    Threshold: difflib SequenceMatcher ratio >= 0.80.
    """
    if not PRIOR_CASES_PATH.exists():
        all_cases = []
    else:
        all_cases = json.loads(PRIOR_CASES_PATH.read_text(encoding="utf-8"))

    matched = []
    for case in all_cases:
        ratio = difflib.SequenceMatcher(
            None,
            (name or "").lower(),
            (case.get("name_queried") or "").lower(),
        ).ratio()
        if ratio >= 0.80:
            matched.append(case)

    clearances = sum(1 for c in matched if c.get("resolution") == "FALSE_POSITIVE")
    escalations = sum(1 for c in matched if c.get("resolution") == "ESCALATED")

    print(f"\n  [Tool: case_management_prior_cases]")
    print(f"    Name queried : {name}")
    print(f"    Cases found  : {len(matched)}")
    print(f"    Clearances   : {clearances}")
    print(f"    Escalations  : {escalations}")

    return {
        "name_queried": name,
        "prior_clearances": clearances,
        "prior_escalations": escalations,
        "total_cases": len(matched),
        "most_recent_resolution": matched[-1].get("resolution", "none") if matched else "none",
        "analyst_notes": [c.get("resolution_note", "") for c in matched],
        "case_list": matched,
    }


# ── Tool 7 ── close_alert (blocked by PreToolUse hook) ────────────
def close_alert(alert_id: str, disposition: str) -> dict:
    """
    EXISTS ONLY TO BE BLOCKED by the PreToolUse hook.
    If this body runs, the hook failed.
    """
    raise RuntimeError(
        "BLOCKED: Analyst disposition required   . "
        "Agent cannot auto-close sanctions alerts. "
        "PMLA 2002 / RBI KYC Master Direction 2025."
    )


# ── Registry used by hooks / orchestrator ─────────────────────────
TOOLS = {
    "screening_api_lookup":          screening_api_lookup,
    "core_banking_get_customer":     core_banking_get_customer,
    "get_adverse_media":             get_adverse_media,
    "get_company_registry":          get_company_registry,
    "get_ubo_chain":                 get_ubo_chain,
    "case_management_prior_cases":   case_management_prior_cases,
    "close_alert":                   close_alert,
}
