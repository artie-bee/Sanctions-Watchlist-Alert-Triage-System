# SETUP:
# Step 1 — Run DynamoDB Local in Docker:
#   docker run -p 8001:8000 amazon/dynamodb-local
#
# Step 2 — Install dependencies:
#   pip install fastapi uvicorn boto3
#
# Step 3 — Run this app:
#   uvicorn alert_intake:app --reload --port 8000
#
# Step 4 — Test in browser:
#   http://localhost:8000/docs
#
# SWITCHING TO REAL AWS LATER:
# Change endpoint_url from "http://localhost:8001" to None
# Change dummy credentials to real AWS credentials
# Everything else stays the same.

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# Make `from agent import HybridOrchestrator` resolve sanctions_triage/src.
_TRIAGE_SRC = Path(__file__).resolve().parent / "sanctions_triage" / "src"
if str(_TRIAGE_SRC) not in sys.path:
    sys.path.insert(0, str(_TRIAGE_SRC))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("alert_intake")

# Honour DYNAMODB_ENDPOINT/REGION from the environment (set by
# docker-compose to reach the `dynamodb` service); fall back to the
# local DynamoDB Local defaults for non-container runs.
DYNAMO_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8001")
DYNAMO_REGION = os.environ.get("DYNAMODB_REGION", "us-east-1")
TABLE_NAME = "sanctions_alerts"
KYC_TABLE_NAME = "customer_kyc"
TXN_TABLE_NAME = "customer_transactions"
ADVERSE_MEDIA_TABLE_NAME = "adverse_media_records"
REGISTRY_TABLE_NAME = "company_registry"
UBO_TABLE_NAME = "ubo_ownership_chains"

dynamodb = boto3.resource(
    "dynamodb",
    endpoint_url=DYNAMO_ENDPOINT,
    region_name=DYNAMO_REGION,
    aws_access_key_id="dummy",
    aws_secret_access_key="dummy",
)


def table():
    return dynamodb.Table(TABLE_NAME)


def kyc_table():
    return dynamodb.Table(KYC_TABLE_NAME)


def txn_table():
    return dynamodb.Table(TXN_TABLE_NAME)


def adverse_media_table():
    return dynamodb.Table(ADVERSE_MEDIA_TABLE_NAME)


def registry_table():
    return dynamodb.Table(REGISTRY_TABLE_NAME)


def ubo_table():
    return dynamodb.Table(UBO_TABLE_NAME)


def ensure_table():
    client = dynamodb.meta.client
    try:
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def ensure_kyc_table():
    client = dynamodb.meta.client
    try:
        client.create_table(
            TableName=KYC_TABLE_NAME,
            KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        log.info(f"Created table {KYC_TABLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    client.get_waiter("table_exists").wait(TableName=KYC_TABLE_NAME)


def ensure_txn_table():
    client = dynamodb.meta.client
    try:
        client.create_table(
            TableName=TXN_TABLE_NAME,
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
        log.info(f"Created table {TXN_TABLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    client.get_waiter("table_exists").wait(TableName=TXN_TABLE_NAME)


def _ensure_simple_table(name: str, key_attr: str) -> None:
    client = dynamodb.meta.client
    try:
        client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key_attr, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key_attr, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        log.info(f"Created table {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    client.get_waiter("table_exists").wait(TableName=name)


def ensure_adverse_media_table():
    _ensure_simple_table(ADVERSE_MEDIA_TABLE_NAME, "record_id")


def ensure_registry_table():
    _ensure_simple_table(REGISTRY_TABLE_NAME, "company_id")


def ensure_ubo_table():
    _ensure_simple_table(UBO_TABLE_NAME, "chain_id")


def decimal_safe(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_safe(v) for v in obj]
    return obj


def to_decimal(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_decimal(v) for v in obj]
    return obj


def scan_all(**kwargs):
    items = []
    response = table().scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = table().scan(**kwargs)
        items.extend(response.get("Items", []))
    return items


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_table()
    ensure_kyc_table()
    ensure_txn_table()
    ensure_adverse_media_table()
    ensure_registry_table()
    ensure_ubo_table()
    Path("worksheets").mkdir(exist_ok=True)
    log.info(
        "DynamoDB Local connected. Tables ready: "
        f"{TABLE_NAME}, {KYC_TABLE_NAME}, {TXN_TABLE_NAME}, "
        f"{ADVERSE_MEDIA_TABLE_NAME}, {REGISTRY_TABLE_NAME}, {UBO_TABLE_NAME}."
    )
    yield


app = FastAPI(title="Sanctions Alert Intake", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


class AlertPayload(BaseModel):
    customer_name: str
    match_score: float
    matched_entity: str
    source_list: str
    dob: Optional[str] = None
    nationality: Optional[str] = None


class Disposition(BaseModel):
    decision: str
    analyst_note: Optional[str] = None


@app.post("/alerts")
def create_alert(payload: AlertPayload):
    alert_id = "ALR-" + uuid4().hex[:10].upper()
    customer_id = "CUST-" + uuid4().hex[:8].upper()
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    sla_deadline = (now + timedelta(hours=24)).isoformat()

    item = {
        "alert_id": alert_id,
        "customer_id": customer_id,
        "customer_name": payload.customer_name,
        "match_score": payload.match_score,
        "matched_entity": payload.matched_entity,
        "source_list": payload.source_list,
        "status": "PENDING",
        "created_at": created_at,
        "sla_deadline": sla_deadline,
    }
    if payload.dob:
        item["dob"] = payload.dob
    if payload.nationality:
        item["nationality"] = payload.nationality

    table().put_item(Item=to_decimal(item))
    log.info(f"New alert received: {alert_id} — {payload.customer_name}")

    # Agent triage now lives in sanctions_triage/ — call it externally
    # via `python sanctions_triage/src/run_demo.py` (or wire a fresh
    # subprocess invocation here if you want auto-triage on insert).

    return {
        "alert_id": alert_id,
        "customer_id": customer_id,
        "status": "PENDING",
        "created_at": created_at,
        "sla_deadline": sla_deadline,
    }


@app.get("/alerts")
def list_alerts(status: Optional[str] = Query(None)):
    kwargs = {}
    if status:
        kwargs["FilterExpression"] = Attr("status").eq(status)
    items = scan_all(**kwargs)
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return decimal_safe(items)


@app.get("/alerts/{alert_id}")
def get_alert(alert_id: str):
    response = table().get_item(Key={"alert_id": alert_id})
    item = response.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Alert not found")
    return decimal_safe(item)


@app.patch("/alerts/{alert_id}/dispose")
def dispose_alert(alert_id: str, body: Disposition):
    decision = body.decision.upper()
    if decision not in ("CLEARED", "ESCALATED"):
        raise HTTPException(
            status_code=400, detail="decision must be CLEARED or ESCALATED"
        )

    existing = table().get_item(Key={"alert_id": alert_id}).get("Item")
    if not existing:
        raise HTTPException(status_code=404, detail="Alert not found")

    disposed_at = datetime.now(timezone.utc).isoformat()
    update_expr = "SET #s = :s, disposed_at = :d"
    expr_values = {":s": decision, ":d": disposed_at}
    expr_names = {"#s": "status"}
    if body.analyst_note is not None:
        update_expr += ", analyst_note = :n"
        expr_values[":n"] = body.analyst_note

    response = table().update_item(
        Key={"alert_id": alert_id},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
        ExpressionAttributeNames=expr_names,
        ReturnValues="ALL_NEW",
    )
    log.info(f"Alert {alert_id} disposed as {decision}")
    return decimal_safe(response.get("Attributes", {}))


@app.get("/customers")
def list_customers(
    risk_rating: Optional[str] = Query(None),
    nationality: Optional[str] = Query(None),
):
    kwargs = {}
    filters = None
    if risk_rating:
        filters = Attr("risk_rating").eq(risk_rating)
    if nationality:
        cond = Attr("nationality").eq(nationality)
        filters = cond if filters is None else (filters & cond)
    if filters is not None:
        kwargs["FilterExpression"] = filters

    items = []
    response = kyc_table().scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = kyc_table().scan(**kwargs)
        items.extend(response.get("Items", []))
    items.sort(key=lambda x: x.get("customer_id", ""))
    return decimal_safe(items)


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    response = kyc_table().get_item(Key={"customer_id": customer_id})
    item = response.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="Customer not found")
    return decimal_safe(item)


def _scan_count(table_obj, filter_expr=None) -> int:
    kwargs = {"Select": "COUNT"}
    if filter_expr is not None:
        kwargs["FilterExpression"] = filter_expr
    total = 0
    response = table_obj.scan(**kwargs)
    total += response.get("Count", 0)
    while "LastEvaluatedKey" in response:
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = table_obj.scan(**kwargs)
        total += response.get("Count", 0)
    return total


# ── Live triage demo (Server-Sent Events) ──────────────────────────
TRIAGE_ALERTS_FILE = (
    Path(__file__).resolve().parent
    / "sanctions_triage" / "data" / "alerts_10.json"
)
TRIAGE_HTML_FILE = Path(__file__).resolve().parent / "static" / "triage.html"
GENERATE_SCRIPT = Path(__file__).resolve().parent / "generate_alerts_10.py"


def _regenerate_alerts() -> None:
    """Run generate_alerts_10.py as a subprocess so the import side-effects
    (its own boto3 client) stay isolated. Blocking — caller schedules in a
    thread."""
    subprocess.run(
        [sys.executable, str(GENERATE_SCRIPT)],
        check=True,
        cwd=str(Path(__file__).resolve().parent),
    )


@app.get("/triage")
def triage_page():
    if not TRIAGE_HTML_FILE.exists():
        raise HTTPException(status_code=500, detail="static/triage.html missing")
    return FileResponse(TRIAGE_HTML_FILE, media_type="text/html")


@app.get("/triage/alerts")
async def triage_alerts(regenerate: bool = Query(False)):
    if regenerate or not TRIAGE_ALERTS_FILE.exists():
        try:
            await asyncio.to_thread(_regenerate_alerts)
        except subprocess.CalledProcessError as e:
            raise HTTPException(
                status_code=500,
                detail=f"generate_alerts_10.py failed: {e}",
            )
    try:
        return json.loads(TRIAGE_ALERTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="alerts_10.json not found")


@app.get("/triage/run")
async def triage_run(alert_ids: str = Query(...)):
    """SSE: runs all alert_ids in parallel threads; multiplexed event stream.
    EventSource uses GET, so alert_ids comes in as a comma-separated query."""
    ids = [a.strip() for a in alert_ids.split(",") if a.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="alert_ids required")

    # Imported lazily so a missing GROQ_API_KEY only breaks /triage, not the
    # whole app on import.
    from agent import HybridOrchestrator  # type: ignore

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_event_factory(alert_id: str):
        def cb(event: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass  # loop closed — client disconnected
        return cb

    async def run_one(alert_id: str) -> None:
        orch = HybridOrchestrator()
        try:
            await asyncio.to_thread(
                orch.process_alert, alert_id, on_event_factory(alert_id),
            )
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "error",
                "alert_id": alert_id,
                "error": repr(e),
            })

    async def event_stream():
        # Kick off all alerts concurrently.
        tasks = [asyncio.create_task(run_one(aid)) for aid in ids]
        yield f"data: {json.dumps({'type': 'started', 'alert_ids': ids})}\n\n"

        remaining = set(ids)
        all_done_task = asyncio.create_task(asyncio.gather(*tasks))
        try:
            while remaining:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    if all_done_task.done():
                        break
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event.get("type") == "done":
                    remaining.discard(event.get("alert_id"))
            yield f"data: {json.dumps({'type': 'all_done'})}\n\n"
        finally:
            for t in tasks:
                t.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/health")
def health():
    alerts_total = _scan_count(table())
    alerts_pending = _scan_count(table(), Attr("status").eq("PENDING"))
    kyc_total = _scan_count(kyc_table())
    txn_total = _scan_count(txn_table())
    adverse_total = _scan_count(adverse_media_table())
    registry_total = _scan_count(registry_table())
    ubo_total = _scan_count(ubo_table())
    return {
        "status": "ok",
        "dynamodb": "local",
        "endpoint": DYNAMO_ENDPOINT,
        "tables": {
            "sanctions_alerts": {
                "total": alerts_total,
                "pending": alerts_pending,
            },
            "customer_kyc": {"total": kyc_total},
            "customer_transactions": {"total": txn_total},
            "adverse_media_records": {"total": adverse_total},
            "company_registry": {"total": registry_total},
            "ubo_ownership_chains": {"total": ubo_total},
        },
    }
