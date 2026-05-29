"""
Workflow Visualizer — live agentic workflow on http://localhost:7000.

A separate FastAPI app that streams orchestrator progress over
Server-Sent Events while the HybridOrchestrator processes 10
PENDING alerts pulled live from DynamoDB sanctions_alerts.

Reuses sanctions_triage/src/agent.py without modification (other
than the additive progress_cb parameter on HybridOrchestrator).
run_demo.py keeps working unchanged.

Run:
    pip install fastapi uvicorn jinja2 boto3
    python workflow_ui.py
    → http://localhost:7000
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# Force UTF-8 on stdout/stderr so the orchestrator's box-drawing
# (█) and em-dashes in agent.py prints don't crash with
# UnicodeEncodeError under Windows' default cp1252 console. The
# orchestrator runs on a thread inside run_in_executor — every
# print() lands on this same stdout. We try .reconfigure() first
# (works when stdout is a normal TextIOWrapper) and fall back to
# replacing the stream entirely with a UTF-8 wrapper around the
# underlying binary buffer.
import io as _io
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name)
    _ok = False
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
        _ok = (_stream.encoding or "").lower().startswith("utf")
    except (AttributeError, ValueError):
        pass
    if not _ok:
        _buf = getattr(_stream, "buffer", None)
        if _buf is not None:
            setattr(sys, _name, _io.TextIOWrapper(
                _buf, encoding="utf-8", errors="replace", line_buffering=True,
            ))

print(f"[workflow_ui] stdout encoding = {sys.stdout.encoding}", flush=True)

import boto3
from boto3.dynamodb.conditions import Attr
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Make sanctions_triage/src importable so we can reuse the orchestrator
HERE = Path(__file__).resolve().parent
TRIAGE_SRC = HERE / "sanctions_triage" / "src"
if str(TRIAGE_SRC) not in sys.path:
    sys.path.insert(0, str(TRIAGE_SRC))

from agent import HybridOrchestrator  # noqa: E402


# ── FastAPI ───────────────────────────────────────────────────────
app = FastAPI(title="Workflow Visualizer")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


# ── DynamoDB Local ────────────────────────────────────────────────
dynamodb = boto3.resource(
    "dynamodb",
    endpoint_url=os.environ.get("DYNAMODB_ENDPOINT", "http://localhost:8001"),
    region_name=os.environ.get("DYNAMODB_REGION", "us-east-1"),
    aws_access_key_id="dummy",
    aws_secret_access_key="dummy",
)


# ── Helpers ───────────────────────────────────────────────────────
def decimal_safe(obj: Any) -> Any:
    """Recursively convert Decimal → float so payloads JSON-serialise."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [decimal_safe(v) for v in obj]
    return obj


def _confidence(a: dict) -> float:
    try:
        return float(a.get("match_score") or a.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_pending_alerts(count: int = 10) -> list[dict]:
    """Pull PENDING alerts from DynamoDB and return a 3-low / 4-mid /
    3-high split (low and high by match_score). If fewer than `count`
    PENDING alerts exist, returns whatever's available, sorted ascending.
    """
    table = dynamodb.Table("sanctions_alerts")
    items: list[dict] = []
    kwargs: dict = {"FilterExpression": Attr("status").eq("PENDING"), "Limit": 500}
    while True:
        r = table.scan(**kwargs)
        items.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r or len(items) >= 1500:
            break
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    if not items:
        return []

    items.sort(key=_confidence)
    n = len(items)
    if n <= count:
        return [decimal_safe(a) for a in items]

    lows  = items[:3]
    highs = items[-3:]
    # 4 evenly spaced picks from the middle band (avoiding overlap with lows/highs)
    mid_lo, mid_hi = 3, n - 3
    if mid_hi - mid_lo >= 4:
        step = (mid_hi - mid_lo - 1) / 3.0
        mids = [items[mid_lo + round(step * i)] for i in range(4)]
    else:
        mids = items[mid_lo:mid_hi]

    selection = lows + mids + highs
    return [decimal_safe(a) for a in selection][:count]


def _scenario_label(score: float) -> str:
    """Score-band tag shown on simulator scenario cards."""
    if score < 0.50:
        return "Low · Likely Clear"
    if score < 0.75:
        return "Mid · Uncertain"
    return "High · Likely Match"


def fetch_simulator_alerts() -> list[dict]:
    """Return 6 PENDING alerts hand-picked across the score
    distribution — 2 low + 2 mid + 2 high — each annotated with a
    `scenario_label` field so the simulator's left rail can show the
    score band at a glance. Shape matches /api/alerts so the same
    intake-card JS works.

    Uses the SAME table scan that fetch_pending_alerts() uses, so
    the alerts returned here are real and live."""
    table = dynamodb.Table("sanctions_alerts")
    items: list[dict] = []
    kwargs: dict = {"FilterExpression": Attr("status").eq("PENDING"), "Limit": 500}
    while True:
        r = table.scan(**kwargs)
        items.extend(r.get("Items", []))
        if "LastEvaluatedKey" not in r or len(items) >= 1500:
            break
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    if not items:
        return []

    items.sort(key=_confidence)

    lows  = [a for a in items if _confidence(a) < 0.50]
    mids  = [a for a in items if 0.50 <= _confidence(a) < 0.75]
    highs = [a for a in items if _confidence(a) >= 0.75]

    def _pick_two(bucket: list[dict]) -> list[dict]:
        if len(bucket) <= 2:
            return list(bucket)
        return [bucket[0], bucket[-1]]

    selection = _pick_two(lows) + _pick_two(mids) + _pick_two(highs)
    if len(selection) < 6:
        seen = {a.get("alert_id") for a in selection}
        for a in items:
            if a.get("alert_id") in seen:
                continue
            selection.append(a)
            if len(selection) >= 6:
                break

    out: list[dict] = []
    for a in selection[:6]:
        safe = decimal_safe(a)
        safe["scenario_label"] = _scenario_label(_confidence(a))
        out.append(safe)
    return out


def fetch_alert_by_id(alert_id: str) -> dict | None:
    """Fetch a single alert row from DynamoDB by its primary key.
    Used by /api/simulator-run to validate the alert_id query param
    before kicking off the orchestrator."""
    table = dynamodb.Table("sanctions_alerts")
    try:
        r = table.get_item(Key={"alert_id": alert_id})
    except Exception:
        return None
    item = r.get("Item")
    return decimal_safe(item) if item else None


def _sse(payload: dict) -> str:
    """Format a dict as a single SSE event."""
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ── Routes ────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "workflow.html")


@app.get("/observability", response_class=HTMLResponse)
async def observability(request: Request):
    """Enterprise observability dashboard — sits on top of the same
    SSE stream and audit-log artefacts produced by the orchestrator."""
    return templates.TemplateResponse(request, "observability.html")


@app.get("/simulator", response_class=HTMLResponse)
async def simulator(request: Request):
    """Live agent-flow simulator. Picks 6 PENDING alerts (2 low / 2 mid /
    2 high match score) and runs the real HybridOrchestrator against
    them one at a time over SSE, with the agent-flow canvas animating
    in real time as supervisor → P3a → P3b → P3c emit progress."""
    return templates.TemplateResponse(request, "simulator.html")


@app.get("/api/alerts")
async def api_alerts():
    try:
        alerts = fetch_pending_alerts(10)
        return JSONResponse(alerts)
    except Exception as e:
        return JSONResponse(
            {"error": f"failed to fetch alerts: {e}"},
            status_code=503,
        )


@app.get("/api/simulator-alerts")
async def api_simulator_alerts():
    """Six PENDING alerts hand-picked across the score distribution,
    annotated with a `scenario_label` field. Feeds the left rail on
    /simulator. Same item shape as /api/alerts so the card-render
    logic on the frontend is identical."""
    try:
        alerts = fetch_simulator_alerts()
        return JSONResponse(alerts)
    except Exception as e:
        return JSONResponse(
            {"error": f"failed to fetch simulator alerts: {e}"},
            status_code=503,
        )


AUDIT_LOG_PATH = HERE / "sanctions_triage" / "runtime" / "audit_log.jsonl"


@app.get("/api/audit/{alert_id}")
async def api_audit(alert_id: str):
    """Return audit-log entries for a single alert_id. Read-only — pulls
    from sanctions_triage/runtime/audit_log.jsonl. Used by the
    Tool Inspector panel to show inputs / outputs / SHA-256 hashes."""
    if not AUDIT_LOG_PATH.exists():
        return JSONResponse([])
    entries: list[dict] = []
    try:
        with AUDIT_LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("alert_id") == alert_id:
                    entries.append(entry)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(entries)


@app.get("/api/audit-tail")
async def api_audit_tail(limit: int = 100):
    """Return the most recent N audit-log entries across all alerts.
    Powers the architecture / system-health view."""
    if not AUDIT_LOG_PATH.exists():
        return JSONResponse([])
    entries: list[dict] = []
    try:
        with AUDIT_LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(entries[-max(1, min(limit, 1000)):])


@app.get("/api/run-batch")
async def api_run_batch():
    """Stream orchestrator progress events as SSE. One alert at a time."""

    async def event_stream():
        loop = asyncio.get_running_loop()

        try:
            alerts = fetch_pending_alerts(10)
        except Exception as e:
            yield _sse({"type": "batch_error", "error": str(e)})
            return

        yield _sse({"type": "batch_start", "count": len(alerts)})

        if not alerts:
            yield _sse({"type": "batch_complete", "processed": 0})
            return

        SENTINEL = object()

        for alert in alerts:
            alert_id = alert.get("alert_id")
            queue: asyncio.Queue = asyncio.Queue()

            def progress_cb(event: dict, _q=queue, _loop=loop) -> None:
                """Called from the orchestrator's executor thread; hands
                the event to the SSE loop without blocking. Decimal
                values are coerced so the JSON dump always succeeds."""
                try:
                    _loop.call_soon_threadsafe(_q.put_nowait, decimal_safe(event))
                except RuntimeError:
                    # event loop closed (client disconnected) — drop event
                    pass

            orchestrator = HybridOrchestrator(progress_cb=progress_cb)

            def run_and_signal(_aid=alert_id, _orch=orchestrator,
                               _q=queue, _loop=loop, _sent=SENTINEL) -> None:
                try:
                    _orch.process_alert(_aid)
                except Exception as e:
                    try:
                        _loop.call_soon_threadsafe(
                            _q.put_nowait,
                            {"type": "alert_error",
                             "alert_id": _aid, "error": str(e)},
                        )
                    except RuntimeError:
                        pass
                finally:
                    try:
                        _loop.call_soon_threadsafe(_q.put_nowait, _sent)
                    except RuntimeError:
                        pass

            # Fire-and-forget — sentinel signals end of this alert's events
            loop.run_in_executor(None, run_and_signal)

            while True:
                evt = await queue.get()
                if evt is SENTINEL:
                    break
                yield _sse(evt)

        yield _sse({"type": "batch_complete"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering if any
        },
    )


@app.get("/api/simulator-run")
async def api_simulator_run(alert_id: str):
    """Stream HybridOrchestrator progress for ONE alert as SSE. Same
    event shape as /api/run-batch (alert_start → phase_* →
    tool_call_* → close_* → alert_complete → batch_complete) so the
    simulator can share event-handling code with the observability
    dashboard. The simulator UI uses this for its per-scenario
    Send-Alert button."""

    async def event_stream():
        loop = asyncio.get_running_loop()

        if not alert_id:
            yield _sse({"type": "batch_error", "error": "alert_id is required"})
            return

        try:
            alert = fetch_alert_by_id(alert_id)
        except Exception as e:
            yield _sse({"type": "batch_error", "error": str(e)})
            return

        if not alert:
            yield _sse({
                "type": "batch_error",
                "error": f"alert {alert_id} not found",
            })
            return

        yield _sse({"type": "batch_start", "count": 1})

        SENTINEL = object()
        queue: asyncio.Queue = asyncio.Queue()

        def progress_cb(event: dict, _q=queue, _loop=loop) -> None:
            try:
                _loop.call_soon_threadsafe(_q.put_nowait, decimal_safe(event))
            except RuntimeError:
                pass

        orchestrator = HybridOrchestrator(progress_cb=progress_cb)

        def run_and_signal(_aid=alert_id, _orch=orchestrator,
                           _q=queue, _loop=loop, _sent=SENTINEL) -> None:
            try:
                _orch.process_alert(_aid)
            except Exception as e:
                try:
                    _loop.call_soon_threadsafe(
                        _q.put_nowait,
                        {"type": "alert_error",
                         "alert_id": _aid, "error": str(e)},
                    )
                except RuntimeError:
                    pass
            finally:
                try:
                    _loop.call_soon_threadsafe(_q.put_nowait, _sent)
                except RuntimeError:
                    pass

        loop.run_in_executor(None, run_and_signal)

        while True:
            evt = await queue.get()
            if evt is SENTINEL:
                break
            yield _sse(evt)

        yield _sse({"type": "batch_complete"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry point ───────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("workflow_ui:app", host="0.0.0.0", port=7000, reload=True)
