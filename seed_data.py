"""
Deliberate-data seeding for the Sanctions Triage demo.

Replaces the previous Faker-based random seed. Every customer is
derived from a real entry in sanctions.db using one of five named
transformation strategies so every screening attempt produces a
*realistic* fuzzy-match hit that flows through the full pipeline.

NOTE on the variant distribution:
  The spec line "200 entities × 5 variants = 1,000 customers"
  contradicts the per-variant counts (80+70+25+20+5 = 200), so this
  script generates 200 deliberate customers (one variant per entity)
  + 6 named DEMO personas → 206 total. If you actually want 1,000,
  change MULTIPLIER below.

Run:
    python seed_data.py            # idempotent
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── config ───────────────────────────────────────────────────────
HERE         = Path(__file__).resolve().parent
SANCTIONS_DB = HERE / "sanctions.db"
PRIOR_CASES  = HERE / "sanctions_triage" / "data" / "prior_cases.json"

DYNAMO_ENDPOINT = "http://localhost:8001"
REGION          = "us-east-1"
AWS_KEY         = "dummy"  # MUST match alert_intake.py to share namespace

ALERTS_TABLE          = "sanctions_alerts"
KYC_TABLE             = "customer_kyc"
TXN_TABLE             = "customer_transactions"
ADVERSE_MEDIA_TABLE   = "adverse_media_records"
REGISTRY_TABLE        = "company_registry"
UBO_TABLE             = "ubo_ownership_chains"

# Variant distribution (per 200 entities) — change MULTIPLIER for more
MULTIPLIER = 1
SAMPLED_ENTITIES = 200 * MULTIPLIER
VARIANT_COUNTS = {
    "A": 80 * MULTIPLIER,   # 40% — clear FP (name modified, diff DOB/nat)
    "B": 70 * MULTIPLIER,   # 35% — common-name FP (first+last only)
    "C": 25 * MULTIPLIER,   # 12% — uncertain (missing DOB)
    "D": 20 * MULTIPLIER,   # 10% — likely match (same name+nat, ±5y)
    "E":  5 * MULTIPLIER,   #  3% — true match (exact)
}

# Random pools
LOW_RISK_COUNTRIES   = ["IN", "GB", "US", "DE", "AE", "SG", "AU", "CA", "FR", "NL"]
COMMON_OCCUPATIONS   = [
    "Software Engineer", "Teacher", "Accountant", "Doctor",
    "Nurse", "Marketing Manager", "Sales Executive", "Civil Engineer",
    "Architect", "Designer", "Lawyer", "Consultant",
]
MED_OCCUPATIONS      = [
    "Business Owner", "Importer", "Real Estate Agent", "Jeweler",
    "Money Changer", "Hotel Manager",
]
HIGH_OCCUPATIONS     = [
    "Import/Export Trader", "Commodities Broker",
    "Financial Consultant", "Cryptocurrency Trader",
    "Charity Director", "Government Advisor",
]
COMMON_FIRST_NAMES   = ["Mohammed", "Ali", "Hassan", "Ibrahim", "Vladimir",
                        "Yuri", "Rajesh", "Priya", "John", "James",
                        "Mary", "Sarah", "David", "Aisha", "Fatima"]
COMMON_LAST_NAMES    = ["Smith", "Khan", "Singh", "Sharma", "Hassan",
                        "Petrov", "Ivanov", "Brown", "Lee", "Park"]
LAST_NAME_BY_NAT     = {
    "IN": ["Sharma", "Singh", "Kumar", "Khan", "Iyer"],
    "GB": ["Smith", "Brown", "Williams", "Taylor", "Davies"],
    "US": ["Smith", "Jones", "Williams", "Brown", "Davis"],
    "DE": ["Müller", "Schmidt", "Schneider", "Fischer"],
    "AE": ["Al Maktoum", "Al Khalifa", "Al Nahyan"],
    "SG": ["Lee", "Tan", "Lim", "Wong"],
    "AU": ["Smith", "Jones", "Brown"],
    "CA": ["Smith", "Brown", "Tremblay"],
    "FR": ["Martin", "Bernard", "Dubois"],
    "NL": ["De Jong", "Jansen", "Visser"],
}

# Deterministic randomness — re-running gives same data
random.seed(20260513)


# ── DynamoDB ─────────────────────────────────────────────────────
def dynamo():
    return boto3.resource(
        "dynamodb",
        endpoint_url=DYNAMO_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_KEY,
    )


def table(name): return dynamo().Table(name)


def _ddb_client():
    return boto3.client(
        "dynamodb",
        endpoint_url=DYNAMO_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_KEY,
    )


def ensure_kyc_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=KYC_TABLE,
            KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=KYC_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def ensure_alerts_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=ALERTS_TABLE,
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=ALERTS_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def ensure_transactions_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=TXN_TABLE,
            KeySchema=[
                {"AttributeName": "transaction_id", "KeyType": "HASH"},
                {"AttributeName": "customer_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "transaction_id", "AttributeType": "S"},
                {"AttributeName": "customer_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=TXN_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def ensure_adverse_media_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=ADVERSE_MEDIA_TABLE,
            KeySchema=[{"AttributeName": "record_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "record_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=ADVERSE_MEDIA_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def ensure_company_registry_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=REGISTRY_TABLE,
            KeySchema=[{"AttributeName": "company_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "company_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=REGISTRY_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def ensure_ubo_ownership_table():
    client = _ddb_client()
    try:
        client.create_table(
            TableName=UBO_TABLE,
            KeySchema=[{"AttributeName": "chain_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "chain_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=UBO_TABLE)
    except client.exceptions.ResourceInUseException:
        pass


def to_decimal(v):
    """Recursively coerce numeric types so DynamoDB accepts them."""
    if isinstance(v, float):
        return Decimal(str(round(v, 6)))
    if isinstance(v, dict):
        return {k: to_decimal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_decimal(x) for x in v]
    return v


# ── sanctions.db pool ────────────────────────────────────────────
def sample_sanctioned_entities(n: int) -> list[dict]:
    """Pull n random named entries from sanctions.db."""
    conn = sqlite3.connect(SANCTIONS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT full_name, dob, nationality, source, program
        FROM sanctions
        WHERE full_name IS NOT NULL
          AND length(full_name) > 5
          AND length(full_name) < 80
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_named_entity(needle: str) -> dict | None:
    """Find the first sanctions.db row matching needle (for demo personas)."""
    conn = sqlite3.connect(SANCTIONS_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT full_name, dob, nationality, source, program "
        "FROM sanctions WHERE full_name LIKE ? LIMIT 1",
        (f"%{needle}%",),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Step 1: delete random Faker customers ────────────────────────
def delete_random_customers() -> int:
    try:
        t = table(KYC_TABLE)
        deleted = 0
        scan_kwargs = {"ProjectionExpression": "customer_id"}
        while True:
            resp = t.scan(**scan_kwargs)
            items = resp.get("Items", [])
            for it in items:
                cid = it.get("customer_id", "")
                if cid.startswith("CUST-"):
                    t.delete_item(Key={"customer_id": cid})
                    deleted += 1
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            scan_kwargs["ExclusiveStartKey"] = last
        print(f"  Deleted {deleted} random Faker customers (CUST-*)")
        return deleted
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print("  Table does not exist yet — skipping cleanup")
            return 0
        raise


# ── Variant generators ───────────────────────────────────────────
def _safe_year(dob: str | None, fallback: int) -> int:
    """Extract year from a DOB string, fallback if missing."""
    if not dob:
        return fallback
    for tok in str(dob).replace("/", "-").replace(".", "-").split("-"):
        if tok.isdigit() and 1900 < int(tok) < 2100:
            return int(tok)
    return fallback


def _iso_date(year: int) -> str:
    """Pick a random day in the given year — pure datetime."""
    start = datetime(year, 1, 1)
    delta_days = random.randint(0, 364)
    return (start + timedelta(days=delta_days)).strftime("%Y-%m-%d")


def _split_name(full: str) -> tuple[str, str, str]:
    """Return (first, middle, last). Handles 'LAST, First Middle' OFAC style."""
    if "," in full:
        last, _, rest = full.partition(",")
        parts = rest.strip().split()
        first = parts[0] if parts else ""
        middle = " ".join(parts[1:]) if len(parts) > 1 else ""
        return first, middle, last.strip()
    parts = full.split()
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def make_variant(entity: dict, variant: str, customer_id: str) -> dict:
    """Build a KYC item from a sanctioned entity using a named strategy."""
    first, middle, last = _split_name(entity["full_name"])
    entity_year = _safe_year(entity.get("dob"), 1965)
    entity_nat  = entity.get("nationality") or "UNK"

    common = {
        "customer_id":         customer_id,
        "_source_entity":      entity["full_name"],
        "derived_from_sanction": entity["full_name"],
        "variant_type":        variant,
        "verified_at":         datetime.now(timezone.utc).isoformat(),
        "verified_by":         "GBG ID3 Global",
        "primary_id":          random.choice(["Passport", "National ID", "Driving License"]),
        "primary_id_number":   "ID-" + uuid4().hex[:10].upper(),
        "secondary_id":        random.choice(["Utility Bill", "Tax Statement", "Bank Statement"]),
        "phone":               f"+{random.randint(1,99)}-{random.randint(10000000, 99999999)}",
        "email":               f"{first.lower()}.{last.lower() or 'doe'}@example.com",
        "account_type":        "Personal",
    }

    if variant == "A":   # clear false positive — name modified, diff DOB/nat
        # Add or drop a middle name + last name from neutral pool
        new_last = random.choice(LAST_NAME_BY_NAT[random.choice(LOW_RISK_COUNTRIES)])
        new_middle = random.choice(["P.", "K.", "M.", "S.", ""])
        full = f"{first} {new_middle} {new_last}".replace("  ", " ").strip()
        nat  = random.choice(LOW_RISK_COUNTRIES)
        return {**common,
                "full_name":      full,
                "dob":            _iso_date(random.randint(1980, 2000)),
                "nationality":    nat,
                "country_name":   nat,
                "occupation":     random.choice(COMMON_OCCUPATIONS),
                "risk_rating":    "LOW",
                "address":        f"{random.randint(1,9999)} Maple Street, {nat}"}

    if variant == "B":   # common-name FP — keep first+last only
        full = f"{first} {last}".strip()
        nat  = random.choice(LOW_RISK_COUNTRIES)
        return {**common,
                "full_name":      full,
                "dob":            _iso_date(random.randint(1970, 2000)),
                "nationality":    nat,
                "country_name":   nat,
                "occupation":     random.choice(COMMON_OCCUPATIONS + MED_OCCUPATIONS),
                "risk_rating":    random.choice(["LOW", "LOW", "MEDIUM"]),
                "address":        f"{random.randint(1,9999)} Park Avenue, {nat}"}

    if variant == "C":   # uncertain — same name, missing DOB, same nat
        full = entity["full_name"]
        kyc  = {**common,
                "full_name":      full,
                # NO 'dob' key → genuinely missing
                "nationality":    entity_nat[:2] if entity_nat != "UNK" else "AE",
                "country_name":   entity_nat or "Unknown",
                "occupation":     random.choice(MED_OCCUPATIONS),
                "risk_rating":    "MEDIUM",
                "address":        f"{random.randint(1,9999)} Old Town, {entity_nat or 'AE'}"}
        return kyc

    if variant == "D":   # likely match — same name, same nat, DOB ±5y
        full = f"{first} {last}".strip() or entity["full_name"]
        nat  = entity_nat[:2] if entity_nat and entity_nat != "UNK" else "RU"
        offset = random.randint(-5, 5)
        return {**common,
                "full_name":      full,
                "dob":            _iso_date(entity_year + offset),
                "nationality":    nat,
                "country_name":   entity_nat or nat,
                "occupation":     random.choice(HIGH_OCCUPATIONS),
                "risk_rating":    "HIGH",
                "address":        f"{random.randint(1,9999)} High Street, {entity_nat or nat}"}

    if variant == "E":   # true match — exact name/DOB/nat
        return {**common,
                "full_name":      entity["full_name"],
                "dob":            entity.get("dob") or _iso_date(entity_year),
                "nationality":    entity_nat[:2] if entity_nat and entity_nat != "UNK" else "IQ",
                "country_name":   entity_nat or "Iraq",
                "occupation":     random.choice(HIGH_OCCUPATIONS),
                "risk_rating":    "HIGH",
                "address":        f"{random.randint(1,9999)} Trade District, {entity_nat or 'Iraq'}"}

    raise ValueError(f"Unknown variant: {variant}")


# ── Step 2: seed 200 deliberate customers ────────────────────────
def seed_realistic_customers() -> list[dict]:
    """Generate 200 customers, one variant per sanctioned entity."""
    ensure_kyc_table()
    pool = sample_sanctioned_entities(SAMPLED_ENTITIES)
    if len(pool) < SAMPLED_ENTITIES:
        print(f"  WARN: sanctions.db only returned {len(pool)} entries "
              f"(wanted {SAMPLED_ENTITIES})")
    random.shuffle(pool)

    # Build the variant list (A×80, B×70, C×25, D×20, E×5)
    variants = []
    for v, n in VARIANT_COUNTS.items():
        variants.extend([v] * n)
    random.shuffle(variants)

    # Truncate to whichever is shorter
    n_to_make = min(len(pool), len(variants))
    customers = []
    for i in range(n_to_make):
        cid = f"CUST-{i+1:04d}"
        customers.append(make_variant(pool[i], variants[i], cid))

    t = table(KYC_TABLE)
    for c in customers:
        t.put_item(Item=to_decimal(c))
    counts = {v: 0 for v in VARIANT_COUNTS}
    for c in customers:
        counts[c["variant_type"]] += 1
    print(f"  Seeded {len(customers)} deliberate customers")
    for v, n in counts.items():
        print(f"    Variant {v}: {n}")
    return customers


# ── Step 4: 6 named demo personas ────────────────────────────────
DEMO_SPECS = [
    {  # DEMO-001 — Mohammed Ali Hassan
        "customer_id":   "DEMO-001",
        "needle":        "Mohammed Ali",
        "variant_type":  "B",
        "full_name":     "Mohammed Ali Hassan",
        "dob":           "1990-06-12",
        "nationality":   "AE",
        "country_name":  "United Arab Emirates",
        "occupation":    "Software Engineer",
        "risk_rating":   "LOW",
        "expected":      "FALSE_POSITIVE",
        "scenario":      "Common-name false positive",
    },
    {  # DEMO-002 — Rajesh Kumar
        "customer_id":   "DEMO-002",
        "needle":        "Rajesh Kumar",
        "variant_type":  "C",
        "full_name":     "Rajesh Kumar",
        "dob":           None,                  # missing on purpose
        "nationality":   "IN",
        "country_name":  "India",
        "occupation":    "Business Owner",
        "risk_rating":   "MEDIUM",
        "expected":      "UNCERTAIN",
        "scenario":      "Stuck — missing DOB",
    },
    {  # DEMO-003 — Vladimir Petrov
        "customer_id":   "DEMO-003",
        "needle":        "Vladimir Putin",
        "variant_type":  "D",
        "full_name":     "Vladimir Petrov",
        "dob":           "1978-11-05",
        "nationality":   "RU",
        "country_name":  "Russian Federation",
        "occupation":    "Financial Consultant",
        "risk_rating":   "HIGH",
        "expected":      "LIKELY_MATCH",
        "scenario":      "Likely match — same first name, Russian national",
    },
    {  # DEMO-004 — Ibrahim Al Hassan
        "customer_id":   "DEMO-004",
        "needle":        "Ibrahim",
        "variant_type":  "D",
        "full_name":     "Ibrahim Al Hassan",
        "dob":           "1975-04-22",
        "nationality":   "SA",
        "country_name":  "Saudi Arabia",
        "occupation":    "Government Advisor",
        "risk_rating":   "HIGH",
        "expected":      "ESCALATE",
        "scenario":      "PEP match — government advisor",
    },
    {  # DEMO-005 — Priya Sharma (with 5 prior CLEARED alerts)
        "customer_id":   "DEMO-005",
        "needle":        "Priya",
        "variant_type":  "A",
        "full_name":     "Priya Sharma",
        "dob":           "1992-08-14",
        "nationality":   "IN",
        "country_name":  "India",
        "occupation":    "Marketing Manager",
        "risk_rating":   "LOW",
        "expected":      "FALSE_POSITIVE",
        "scenario":      "Recurring false positive — 5 prior clears in history",
    },
    {  # DEMO-006 — Hassan Nasir Al Maliki (true match)
        "customer_id":   "DEMO-006",
        "needle":        "Al-MALIKI",
        "variant_type":  "E",
        "full_name":     "Hassan Nasir Al Maliki",
        "dob":           "1965-03-15",
        "nationality":   "IQ",
        "country_name":  "Iraq",
        "occupation":    "Import/Export Trader",
        "risk_rating":   "HIGH",
        "expected":      "TRUE_MATCH",
        "scenario":      "True positive — exact name, exact DOB, adverse media",
    },
]


def seed_demo_personas() -> list[dict]:
    """Write the 6 named personas. Idempotent — fixed customer_ids."""
    ensure_kyc_table()
    t = table(KYC_TABLE)
    written = []
    for spec in DEMO_SPECS:
        sanction_hit = find_named_entity(spec["needle"]) or {}
        kyc = {
            "customer_id":         spec["customer_id"],
            "full_name":           spec["full_name"],
            "nationality":         spec["nationality"],
            "country_name":        spec["country_name"],
            "occupation":          spec["occupation"],
            "risk_rating":         spec["risk_rating"],
            "account_type":        "Personal",
            "primary_id":          "Passport",
            "primary_id_number":   "ID-" + spec["customer_id"],
            "secondary_id":        "Utility Bill",
            "verified_at":         datetime.now(timezone.utc).isoformat(),
            "verified_by":         "GBG ID3 Global",
            "phone":               f"+91-{random.randint(60000, 99999)}-{random.randint(10000, 99999)}",
            "email":               f"{spec['full_name'].lower().replace(' ', '.')}@example.com",
            "address":             f"{random.randint(1,9999)} Demo Street, {spec['country_name']}",
            "variant_type":        spec["variant_type"],
            "derived_from_sanction": sanction_hit.get("full_name", spec["needle"]),
            "expected_verdict":    spec["expected"],
            "demo_scenario":       spec["scenario"],
            "is_demo":             True,
        }
        if spec["dob"]:
            kyc["dob"] = spec["dob"]
        t.put_item(Item=to_decimal(kyc))
        written.append(kyc)
        print(f"  Seeded {spec['customer_id']} {spec['full_name']:<30} "
              f"→ expected {spec['expected']}")
    return written


# ── Step 5: transactions ─────────────────────────────────────────
TXN_COUNTRIES_LOW  = ["IN", "US", "GB", "DE", "FR", "SG", "AU"]
TXN_COUNTRIES_HIGH = ["IR", "SY", "KP", "BY", "RU", "MM", "VE", "CU"]
TXN_CATEGORIES     = ["TRANSFER", "PAYMENT", "WITHDRAWAL", "DEPOSIT", "CARD"]
TXN_COUNTERPARTIES = ["Amazon Marketplace", "Local Vendor", "Grocery Store",
                       "Salary Credit", "Wire In - INTL", "ATM Withdrawal",
                       "Online Payment Gateway", "Crypto Exchange"]


def seed_transactions(customers: list[dict], per_customer: int = 50) -> int:
    """Seed `per_customer` transactions per customer."""
    ensure_transactions_table()
    t = table(TXN_TABLE)
    inserted = 0
    now = datetime.now(timezone.utc)
    for c in customers:
        cid     = c["customer_id"]
        risk    = c.get("risk_rating", "LOW")
        is_demo = bool(c.get("is_demo"))
        intl_pool = (TXN_COUNTRIES_HIGH if risk == "HIGH" or is_demo
                      else TXN_COUNTRIES_LOW)
        for i in range(per_customer):
            txn_date = (now - timedelta(days=random.randint(1, 730))).strftime("%Y-%m-%d")
            amt = (random.uniform(100000, 2_000_000) if risk == "HIGH"
                    else random.uniform(500, 50_000))
            txn = {
                "transaction_id": "TXN-" + uuid4().hex[:12].upper(),
                "customer_id":    cid,
                "date":           txn_date,
                "amount":         f"{amt:.2f}",
                "currency":       random.choice(["USD", "INR", "AED", "EUR"]),
                "country":        random.choice(intl_pool),
                "txn_type":       random.choice(["DEBIT", "CREDIT"]),
                "category":       random.choice(TXN_CATEGORIES),
                "counterparty":   random.choice(TXN_COUNTERPARTIES),
            }
            t.put_item(Item=to_decimal(txn))
            inserted += 1
    print(f"  Seeded {inserted:,} transactions ({per_customer}/customer)")
    return inserted


# ── Step 6: historical alerts (proportional) ─────────────────────
def seed_historical_alerts(customers: list[dict], n: int = 2500) -> int:
    """
    Seed ~n historical alerts proportional to customer variant:
       A → mostly CLEARED  B → mostly CLEARED  C → PENDING
       D → mixed PENDING/ESCALATED  E → ESCALATED
    """
    ensure_alerts_table()
    t = table(ALERTS_TABLE)
    pool = [c for c in customers if c["customer_id"].startswith("CUST-")]
    if not pool:
        print("  No customers to seed alerts for — skipping")
        return 0

    sanctioned_pool = sample_sanctioned_entities(50)
    inserted = 0
    now = datetime.now(timezone.utc)
    for _ in range(n):
        c = random.choice(pool)
        variant = c.get("variant_type", "A")
        # status weights by variant
        weights = {
            "A": [("CLEARED", 0.80), ("PENDING", 0.15), ("ESCALATED", 0.05)],
            "B": [("CLEARED", 0.75), ("PENDING", 0.20), ("ESCALATED", 0.05)],
            "C": [("PENDING", 0.70), ("CLEARED", 0.20), ("ESCALATED", 0.10)],
            "D": [("PENDING", 0.40), ("ESCALATED", 0.45), ("CLEARED", 0.15)],
            "E": [("ESCALATED", 0.75), ("PENDING", 0.20), ("CLEARED", 0.05)],
        }[variant]
        r = random.random()
        cum = 0.0
        status = "PENDING"
        for s, w in weights:
            cum += w
            if r <= cum:
                status = s; break
        ent = random.choice(sanctioned_pool)
        created = now - timedelta(hours=random.randint(1, 24 * 60))
        score = (random.uniform(0.92, 0.99) if variant == "E"
                  else random.uniform(0.78, 0.91) if variant == "D"
                  else random.uniform(0.55, 0.78) if variant == "C"
                  else random.uniform(0.50, 0.75))
        item = {
            "alert_id":       "ALR-" + uuid4().hex[:10].upper(),
            "customer_id":    c["customer_id"],
            "customer_name":  c["full_name"],
            "matched_entity": ent["full_name"],
            "source_list":    ent.get("source", "OFAC"),
            "match_score":    f"{score:.3f}",
            "confidence":     f"{score:.3f}",
            "status":         status,
            "verdict":        "UNCERTAIN" if status == "PENDING"
                              else ("TRUE_MATCH" if status == "ESCALATED"
                                    else "FALSE_POSITIVE"),
            "created_at":     created.isoformat(),
            "sla_deadline":   (created + timedelta(hours=24)).isoformat(),
            "nationality":    c.get("nationality", ""),
            "dob":            c.get("dob", ""),
        }
        if status in ("CLEARED", "ESCALATED"):
            item["disposed_at"] = (created + timedelta(hours=random.randint(1, 23))).isoformat()
            item["analyst_note"] = "Auto-seeded historical disposition"
        t.put_item(Item=to_decimal(item))
        inserted += 1
    print(f"  Seeded {inserted:,} historical alerts")
    return inserted


# ── Step 7: prior cases for Priya Sharma (5 CLEARED entries) ─────
def seed_priya_history() -> int:
    """Append 5 prior FALSE_POSITIVE cases for Priya Sharma into prior_cases.json."""
    PRIOR_CASES.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(PRIOR_CASES.read_text(encoding="utf-8")) if PRIOR_CASES.exists() else []
    # Remove any old Priya entries (idempotent)
    existing = [c for c in existing if (c.get("name_queried") or "").lower() != "priya sharma"]

    notes = [
        "Common Indian name. Customer is a marketing professional with stable salary credits.",
        "Cross-verified with PAN; no match to sanctioned entity. Different DOB.",
        "Re-screen after periodic review — pattern unchanged; cleared.",
        "Tenure >3 years, no high-risk transactions. Auto-cleared by L1.",
        "Latest quarterly screen — still clear, no adverse media.",
    ]
    for i, note in enumerate(notes, start=1):
        existing.append({
            "case_id":         f"CASE-2025-PRIYA-{i:03d}",
            "name_queried":    "Priya Sharma",
            "alert_id":        f"ALR-OLD-PRIYA-{i}",
            "resolution":      "FALSE_POSITIVE",
            "resolved_by":     f"ANALYST-{random.randint(1,5):02d}",
            "resolved_at":     (datetime.now(timezone.utc) -
                                timedelta(days=30 * i)).isoformat(),
            "resolution_note": note,
        })
    PRIOR_CASES.write_text(json.dumps(existing, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(f"  Seeded 5 prior cases for Priya Sharma → {PRIOR_CASES.name}")
    return 5


# ── Step 8: adverse media for Hassan Al Maliki ───────────────────
def seed_hassan_adverse_media() -> int:
    ensure_adverse_media_table()
    t = table(ADVERSE_MEDIA_TABLE)
    rec = {
        "record_id":     "ADV-DEMO-HASSAN-001",
        "person_name":   "Hassan Nasir Al Maliki",
        "category":      "SANCTIONS_EVASION",
        "severity":      "HIGH",
        "source":        "Reuters",
        "country":       "IQ",
        "published_date":(datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d"),
        "headline":      "Hassan Nasir Al Maliki linked to oil-trade sanctions evasion network",
        "summary":       "Reuters investigation alleges Hassan Nasir Al Maliki coordinated a "
                          "front-company network exporting Iraqi crude through Belarusian intermediaries, "
                          "in violation of EU and US sanctions. Severity: HIGH.",
    }
    t.put_item(Item=to_decimal(rec))
    print(f"  Seeded 1 adverse media record for Hassan Nasir Al Maliki")
    return 1


# ── main ─────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Sanctions Triage — Demo Data Seeding")
    print("=" * 60)

    print("\n[0/8] Ensuring DynamoDB tables exist")
    ensure_kyc_table()
    ensure_alerts_table()
    ensure_transactions_table()
    ensure_adverse_media_table()
    ensure_company_registry_table()
    ensure_ubo_ownership_table()

    print("\n[1/8] Deleting random Faker customers (CUST-*)")
    delete_random_customers()

    print(f"\n[2/8] Seeding deliberate customers from sanctions.db pool "
          f"({SAMPLED_ENTITIES} entities)")
    realistic = seed_realistic_customers()

    print("\n[3/8] Seeding 6 named demo personas (DEMO-001..006)")
    personas = seed_demo_personas()

    all_customers = realistic + personas

    print("[4/8] SKIPPED — transactions (run separately)")
    # txn_n = seed_transactions(all_customers, per_customer=50)
    txn_n = 0

    print("\n[5/8] Seeding historical alerts (~2,500)")
    alert_n = seed_historical_alerts(all_customers, n=2500)

    print("\n[6/8] Seeding prior-case history for Priya Sharma")
    seed_priya_history()

    print("\n[7/8] Seeding adverse media for Hassan Nasir Al Maliki")
    seed_hassan_adverse_media()

    print("\n[8/8] Done.")
    print("=" * 60)
    print(f"  {len(realistic)} deliberate customers")
    print(f"  {len(personas)} named demo personas (DEMO-001..006)")
    print(f"  {txn_n:,} transactions")
    print(f"  {alert_n:,} historical alerts")
    print(f"  5 prior cases for Priya Sharma")
    print(f"  1 adverse media for Hassan Nasir Al Maliki")
    print("=" * 60)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\nElapsed: {time.time() - t0:.1f}s")
