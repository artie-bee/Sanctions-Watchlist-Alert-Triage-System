"""
HybridOrchestrator — Anthropic Claude Haiku for dynamic tool calling
and narrative, plus a rule-based scoring formula for the verdict.

Three phases:
  1. LLM dynamically calls the 6 evidence tools.
  2. Rule-based scoring engine produces the deterministic verdict.
  3. LLM writes a plain-English compliance narrative.

The class is named HybridOrchestrator. `MockOrchestrator = HybridOrchestrator`
is exported as a backwards-compatible alias so the existing
`run_demo.py` (which `from agent import MockOrchestrator`) keeps working
without modification.

Active provider: Anthropic Claude Haiku, reached through the OpenAI-compatible
endpoint so the existing `openai` Python client keeps working unchanged.
"""

# To swap providers, change these three lines:
#   - Anthropic Claude :  base_url = "https://api.anthropic.com/v1/"   (active)
#                         api_key  = ANTHROPIC_API_KEY
#                         model    = "claude-haiku-4-5-20251001"
#   - Groq             :  base_url = "https://api.groq.com/openai/v1"
#                         api_key  = GROQ_API_KEY
#                         model    = "llama-3.1-8b-instant"
#   - OpenAI           :  base_url = "https://api.openai.com/v1"
#   - Ollama (local)   :  base_url = "http://localhost:11434/v1"
#                         api_key  = "ollama"
#                         model    = "llama3"
# No other code changes required.
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from anthropic import Anthropic

# Make sibling imports work whether run from src/ or the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hooks import HookManager, ToolBlockedError  # noqa: E402
import tools as tool_module  # noqa: E402
from tools import TOOLS  # noqa: E402
from worksheet import (  # noqa: E402
    PriorCaseSummary,
    SanctionsHit,
    TransactionSummary,
    Worksheet,
)

# ── LLM client (Anthropic Claude, native SDK) ────────────────────────
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL",   "claude-haiku-4-5-20251001")

_client = Anthropic(api_key=ANTHROPIC_API_KEY or "missing-key")


# ── Tool schemas for OpenAI-style function calling ───────────────────
TOOLS_MAP = {
    "screening_api_lookup":        tool_module.screening_api_lookup,
    "core_banking_get_customer":   tool_module.core_banking_get_customer,
    "get_adverse_media":           tool_module.get_adverse_media,
    "get_company_registry":        tool_module.get_company_registry,
    "get_ubo_chain":               tool_module.get_ubo_chain,
    "case_management_prior_cases": tool_module.case_management_prior_cases,
    "close_alert":                 tool_module.close_alert,
}

TOOL_SCHEMAS = [
    {
        "name": "screening_api_lookup",
        "description": (
            "Gets the sanctions alert from DynamoDB and queries the real "
            "66,114-row sanctions.db for the matched entity. Call this FIRST "
            "to learn the customer_id and entity_name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"alert_id": {"type": "string"}},
            "required": ["alert_id"],
        },
    },
    {
        "name": "core_banking_get_customer",
        "description": (
            "Fetches full customer KYC and last 10 transactions from DynamoDB. "
            "Returns risk_rating, nationality, DOB, and counts of large / "
            "international transactions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_adverse_media",
        "description": (
            "Searches DynamoDB adverse_media_records for negative press "
            "coverage about this customer (linked by person name)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id":   {"type": "string"},
                "customer_name": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_ubo_chain",
        "description": (
            "Gets the Ultimate Beneficial Owner chain for the sanctioned "
            "entity from DynamoDB. Linked by entity_name (not customer_id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "entity_name": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "case_management_prior_cases",
        "description": (
            "Gets prior sanctions alert history for this customer name "
            "via fuzzy match against prior_cases.json. Returns counts of "
            "prior clearances and escalations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_company_registry",
        "description": (
            "Checks DynamoDB company_registry for corporate ties to this "
            "entity (matches either company_name or director person_name)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"entity_name": {"type": "string"}},
            "required": ["entity_name"],
        },
    },
    {
        "name": "close_alert",
        "description": (
            "ALWAYS BLOCKED. Only human analysts can close alerts under "
            "PMLA 2002 / RBI KYC Master Direction 2025. Do not call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alert_id":    {"type": "string"},
                "disposition": {"type": "string"},
            },
            "required": ["alert_id", "disposition"],
        },
        "cache_control": {"type": "ephemeral"},
    },
]

PHASE1_PROMPT = """You are a sanctions compliance analyst for an Indian bank \
operating under PMLA 2002 and RBI KYC Master Direction 2025.

You must investigate ONE sanctions alert by calling each of these tools EXACTLY ONCE:

  1. screening_api_lookup(alert_id)          — call FIRST
  2. core_banking_get_customer(customer_id)
  3. get_adverse_media(customer_id, customer_name)
  4. get_ubo_chain(customer_id, entity_name)
  5. case_management_prior_cases(name)
  6. get_company_registry(entity_name)

CRITICAL — argument extraction:
- Tool 1 returns a dict with `alert.customer_id`, `alert.customer_name`,
  and `entity_name`. Read those THREE values from the response.
- Pass the actual `customer_id` (e.g. "CUST-0361") to tools 2,3,4 — NOT the alert_id.
- Pass the actual `customer_name` to tool 3 and the actual `entity_name` (the
  sanctioned-side name, e.g. "Roman Ivanovitj MELNIK") to tools 4 and 6.
- Pass `customer_name` to tool 5 as `name`.

Rules:
- Call each tool ONCE. Never call the same tool twice.
- Do NOT call close_alert — it is blocked by policy.
- Do NOT give a verdict.
- When all six tools have been called successfully, reply with just: DONE
"""

PHASE3_PROMPT = """You are a senior sanctions compliance analyst writing a \
triage report for an Indian bank.

You have been given:
  - The sanctions alert details
  - The customer KYC and transaction data
  - Adverse media results
  - Prior case history
  - A calculated verdict from the bank's rule-based scoring engine

Write a clear compliance narrative explaining:
  1. Who the customer is and their risk profile
  2. What the sanctions match is and why it was flagged
  3. Which attributes corroborate the match and which contradict it
  4. Why the verdict is correct given the evidence
  5. What the analyst should do next

Plain English. Specific. Cite actual values from the tool results. Under \
200 words. Do NOT repeat the verdict — explain it.
"""


# ── Orchestrator ──────────────────────────────────────────────────────
class HybridOrchestrator:
    """
    Phase 1: Claude Haiku decides which tools to call.
    Phase 2: Pure-Python rule-based scoring decides the verdict.
    Phase 3: Claude Haiku writes the analyst narrative.
    """

    def __init__(self, progress_cb: Optional[Callable[[dict], None]] = None):
        self.client = _client
        self.model  = ANTHROPIC_MODEL
        self.progress_cb = progress_cb

    def _emit(self, event_type: str, **data) -> None:
        """Push a typed progress event to progress_cb if registered.

        No-op when progress_cb is None — keeps `run_demo.py` (which
        constructs HybridOrchestrator() with no callback) overhead-free.
        Callback exceptions are swallowed so they cannot break a run.
        """
        if self.progress_cb is None:
            return
        try:
            self.progress_cb({"type": event_type, **data})
        except Exception:
            pass

    # ── Public entry point  ─
    def process_alert(
        self,
        alert_id: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> Worksheet:
        hook = HookManager(alert_id=alert_id, on_event=on_event)

        def emit(payload: dict) -> None:
            if on_event is None:
                return
            try:
                on_event({"alert_id": alert_id, **payload})
            except Exception:
                pass

        print("\n" + "█" * 70)
        print(f" HYBRID ORCHESTRATOR  —  alert {alert_id}")
        print(f" LLM    : {self.model}")
        print(f" Scoring: rule-based (deterministic)")
        print("█" * 70)
        emit({"type": "run_start", "model": self.model})
        self._emit("alert_start", alert_id=alert_id, model=self.model)

        # ── PHASE 1 — LLM dynamic tool calling ──
        emit({"type": "phase_start", "phase": 1, "label": "LLM evidence gathering"})
        self._emit("phase_1_start", alert_id=alert_id)
        tool_results = self._phase1_llm_tool_calls(alert_id, hook, emit)

        # ── Defensive fill: if the LLM skipped any of the 6 evidence
        #    tools we call them directly so Phase 2 has complete inputs.
        self._fill_missing_tools(alert_id, tool_results, hook, emit)
        emit({"type": "phase_end", "phase": 1})
        self._emit("phase_1_complete", alert_id=alert_id)

        # ── PHASE 2 — Rule-based scoring ──
        emit({"type": "phase_start", "phase": 2, "label": "Rule-based scoring"})
        self._emit("phase_2_start", alert_id=alert_id)
        score_pack = self._phase2_rule_based_scoring(tool_results)
        emit({
            "type": "score",
            "base": score_pack["base"],
            "context_score": score_pack["context_score"],
            "confidence_adjust": score_pack["confidence_adjust"],
            "final_risk_score": score_pack["final_risk_score"],
            "recommendation": score_pack["recommendation"],
            "confidence_pct": score_pack["confidence_pct"],
        })
        emit({"type": "phase_end", "phase": 2})
        self._emit(
            "phase_2_complete",
            alert_id=alert_id,
            base_score=score_pack["base"],
            final_score=score_pack["final_risk_score"],
            verdict=score_pack["recommendation"],
            confidence_pct=score_pack["confidence_pct"],
        )

        # ── PHASE 3 — LLM narrative ──
        emit({"type": "phase_start", "phase": 3, "label": "LLM narrative"})
        self._emit("phase_3_start", alert_id=alert_id)
        llm_narrative = self._phase3_llm_narrative(
            alert_id, tool_results, score_pack
        )
        emit({"type": "narrative", "text": llm_narrative})
        emit({"type": "phase_end", "phase": 3})
        self._emit("phase_3_complete", alert_id=alert_id, narrative=llm_narrative)

        # ── Demonstrate the close_alert block (3 attempts × 1 alert) ──
        blocked = []
        for disp in ("FALSE_POSITIVE", "TRUE_MATCH", "ESCALATED"):
            self._emit("close_attempt", alert_id=alert_id, disposition=disp)
            try:
                hook.invoke(
                    "close_alert", TOOLS["close_alert"],
                    alert_id=alert_id, disposition=disp,
                )
            except ToolBlockedError as e:
                blocked.append(f"close_alert({disp}) → BLOCKED: {e}")
                self._emit(
                    "close_blocked",
                    alert_id=alert_id,
                    disposition=disp,
                    reason=str(e),
                )

        ws = self._build_worksheet(
            alert_id, tool_results, score_pack,
            llm_narrative=llm_narrative, blocked=blocked,
        )
        emit({"type": "worksheet", "worksheet": json.loads(ws.model_dump_json())})
        emit({"type": "done"})
        self._emit(
            "alert_complete",
            alert_id=alert_id,
            worksheet=json.loads(ws.model_dump_json()),
        )
        return ws

    # ────────────────────────────────────────────────────────────────
    # PHASE 1
    # ────────────────────────────────────────────────────────────────
    EVIDENCE_TOOLS = {
        "screening_api_lookup", "core_banking_get_customer",
        "get_adverse_media", "get_ubo_chain",
        "case_management_prior_cases", "get_company_registry",
    }

    def _phase1_llm_tool_calls(
        self,
        alert_id: str,
        hook: HookManager,
        emit: Callable[[dict], None] = lambda _e: None,
    ) -> dict:
        print("\n" + "─" * 60)
        print(f" PHASE 1: LLM investigation ({self.model})")
        print("─" * 60)

        # Anthropic shape: system is top-level, NOT a message role.
        messages: list[dict] = [
            {"role": "user", "content": (
                f"Investigate sanctions alert {alert_id}. "
                f"Start by calling screening_api_lookup(alert_id='{alert_id}'), "
                f"then read the returned `customer_id` / `customer_name` / "
                f"`entity_name` and pass THOSE to the other tools."
            )},
        ]

        tool_results: dict[str, Any] = {}
        seen_calls: set[tuple] = set()  # (name, args-tuple) — dedup
        max_iter = 10

        for iteration in range(1, max_iter + 1):
            print(f"\n  [LLM] step {iteration}")
            emit({"type": "llm_step", "step": iteration})
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=512,
                    temperature=0.0,
                    system=[{"type": "text", "text": PHASE1_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
            except Exception as e:
                print(f"  [LLM] API error: {e!r}")
                break

            cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
            print(f"  [cache] read={cache_read} write={cache_write} input={resp.usage.input_tokens}")
            self._emit(
                "cache_stats",
                alert_id=alert_id,
                step=iteration,
                cache_read=cache_read,
                cache_creation=cache_write,
                input_tokens=resp.usage.input_tokens,
            )

            # Stop condition: anything other than tool_use means we're done.
            if resp.stop_reason != "tool_use":
                preview = ""
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        preview = (block.text or "").strip()[:80]
                        break
                print(f"  [LLM] data gathering complete: {preview}")
                break

            # Append the assistant turn verbatim — the SDK accepts its own
            # content blocks back as input on the next turn.
            messages.append({"role": "assistant", "content": resp.content})

            # Build ONE user-role turn whose content is a list of tool_result
            # blocks. Anthropic requires every tool_use to be answered with a
            # matching tool_result in the very next user turn.
            tool_result_blocks: list[dict] = []

            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                name = block.name
                args = block.input or {}
                print(f"  [LLM → {name}]  args={args}")

                # Duplicate-call guard — short-circuit with a hint
                key = (name, tuple(sorted(args.items())))
                if key in seen_calls:
                    note = {"note": f"already called {name} with same args — skipping"}
                    tool_result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(note),
                    })
                    continue
                seen_calls.add(key)

                fn = TOOLS_MAP.get(name)
                self._emit(
                    "tool_call_start",
                    alert_id=alert_id, tool=name, source="llm",
                )
                tool_ok = True
                tool_blocked = False
                if fn is None:
                    tool_out: Any = {"error": f"unknown tool: {name}"}
                    tool_ok = False
                else:
                    try:
                        tool_out = hook.invoke(name, fn, **args)
                        if name != "close_alert":
                            tool_results[name] = tool_out
                    except ToolBlockedError as e:
                        tool_out = {"blocked": True, "reason": str(e)}
                        tool_ok = False
                        tool_blocked = True
                        print("    ⛔ BLOCKED")
                    except TypeError as e:
                        tool_out = {"error": f"bad args: {e}"}
                        tool_ok = False
                        print(f"    ⚠ bad args: {e}")
                    except Exception as e:
                        tool_out = {"error": str(e)}
                        tool_ok = False
                        print(f"    ⚠ {e}")
                self._emit(
                    "tool_call_complete",
                    alert_id=alert_id, tool=name, source="llm",
                    ok=tool_ok, blocked=tool_blocked,
                )

                # Compact tool output for the LLM context — keep each result
                # under 600 chars. Full result is in tool_results for Phase 2.
                summary = self._summarise_tool_output(name, tool_out)
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(summary, default=str)[:600],
                })

            # Append the consolidated user-role tool_result message.
            if tool_result_blocks:
                messages.append({"role": "user", "content": tool_result_blocks})

            # Early exit: every evidence tool already returned a real result
            if self.EVIDENCE_TOOLS.issubset(tool_results.keys()):
                print(f"\n  [LLM] all 6 evidence tools collected — stopping loop")
                break

        return tool_results

    @staticmethod
    def _summarise_tool_output(name: str, out: Any) -> dict:
        """
        Compact JSON the LLM sees back after each tool call. Keeps the
        hint values (customer_id / entity_name / counts) and drops bulky
        nested lists so we don't blow the 6k TPM rate limit.
        """
        if not isinstance(out, dict):
            return {"value": out}
        if "error" in out or "blocked" in out or "note" in out:
            return out
        if name == "screening_api_lookup":
            alert = out.get("alert", {}) or {}
            return {
                "alert.customer_id":   alert.get("customer_id", ""),
                "alert.customer_name": alert.get("customer_name", ""),
                "alert.matched_entity": alert.get("matched_entity", ""),
                "alert.match_score":   str(alert.get("match_score", "")),
                "entity_name":         out.get("entity_name", ""),
                "hit_count":           out.get("hit_count", 0),
            }
        if name == "core_banking_get_customer":
            kyc = out.get("kyc", {}) or {}
            return {
                "kyc.full_name":   kyc.get("full_name", ""),
                "kyc.risk_rating": kyc.get("risk_rating", ""),
                "transaction_count":              out.get("transaction_count", 0),
                "large_transaction_count":        out.get("large_transaction_count", 0),
                "international_transaction_count":out.get("international_transaction_count", 0),
                "suspicious_pattern":             out.get("suspicious_pattern", False),
            }
        if name == "get_adverse_media":
            return {"count": out.get("count", 0),
                    "has_adverse_media": out.get("has_adverse_media", False)}
        if name == "get_ubo_chain":
            return {"chain_count": out.get("chain_count", 0),
                    "has_ubo_chain": out.get("has_ubo_chain", False)}
        if name == "case_management_prior_cases":
            return {"total_cases":       out.get("total_cases", 0),
                    "prior_clearances":  out.get("prior_clearances", 0),
                    "prior_escalations": out.get("prior_escalations", 0)}
        if name == "get_company_registry":
            return {"count": out.get("count", 0)}
        # default — keep small
        return {k: v for k, v in out.items() if not isinstance(v, (list, dict))}

    # ────────────────────────────────────────────────────────────────
    # Defensive fill — guarantees Phase 2 has complete inputs even
    # when the LLM skips a tool. The hook still gets called so audit
    # log + SHA-256 hashes cover everything.
    # ────────────────────────────────────────────────────────────────
    def _fill_missing_tools(
        self, alert_id: str, tool_results: dict, hook: HookManager,
        emit: Callable[[dict], None] = lambda _e: None,
    ) -> None:
        emit({"type": "defensive_fill_start"})

        def _fill_invoke(tool_name: str, **kwargs):
            """hook.invoke wrapped with progress emits so the UI dot
            re-fires for tools the LLM skipped or hallucinated args for."""
            self._emit(
                "tool_call_start",
                alert_id=alert_id, tool=tool_name, source="fill",
            )
            try:
                out = hook.invoke(tool_name, TOOLS[tool_name], **kwargs)
                self._emit(
                    "tool_call_complete",
                    alert_id=alert_id, tool=tool_name, source="fill",
                    ok=True, blocked=False,
                )
                return out
            except Exception:
                self._emit(
                    "tool_call_complete",
                    alert_id=alert_id, tool=tool_name, source="fill",
                    ok=False, blocked=False,
                )
                raise

        if "screening_api_lookup" not in tool_results:
            print("  [fallback] screening_api_lookup")
            tool_results["screening_api_lookup"] = _fill_invoke(
                "screening_api_lookup", alert_id=alert_id,
            )

        alert = tool_results["screening_api_lookup"].get("alert", {}) or {}
        customer_id   = alert.get("customer_id", "")
        customer_name = alert.get("customer_name", "")
        entity_name   = tool_results["screening_api_lookup"].get("entity_name", "")

        # Validate KYC was looked up with the real customer_id; if the
        # LLM used a hallucinated id, the kyc.customer_id will not match.
        kyc_pack = tool_results.get("core_banking_get_customer") or {}
        kyc_cid  = (kyc_pack.get("kyc") or {}).get("customer_id", "")
        if (not kyc_pack) or (customer_id and kyc_cid != customer_id):
            tag = "fallback" if not kyc_pack else "correct-args"
            print(f"  [{tag}] core_banking_get_customer  customer_id={customer_id}")
            tool_results["core_banking_get_customer"] = _fill_invoke(
                "core_banking_get_customer", customer_id=customer_id,
            )

        # adverse_media + prior_cases + registry are string-name-keyed —
        # the LLM most often hallucinates these. Always re-run with the
        # real customer_name / entity_name from screening_api_lookup so
        # Phase 2 scoring sees real numbers.
        if customer_id and customer_name:
            print(f"  [correct-args] get_adverse_media  name='{customer_name[:40]}'")
            tool_results["get_adverse_media"] = _fill_invoke(
                "get_adverse_media",
                customer_id=customer_id, customer_name=customer_name,
            )
        if customer_id:
            print(f"  [correct-args] get_ubo_chain      entity='{entity_name[:40]}'")
            tool_results["get_ubo_chain"] = _fill_invoke(
                "get_ubo_chain",
                customer_id=customer_id, entity_name=entity_name,
            )
        if customer_name:
            print(f"  [correct-args] case_management_prior_cases  name='{customer_name[:40]}'")
            tool_results["case_management_prior_cases"] = _fill_invoke(
                "case_management_prior_cases", name=customer_name,
            )
        if entity_name:
            print(f"  [correct-args] get_company_registry         entity='{entity_name[:40]}'")
            tool_results["get_company_registry"] = _fill_invoke(
                "get_company_registry", entity_name=entity_name,
            )

    # ────────────────────────────────────────────────────────────────
    # PHASE 2 — rule-based scoring (same formula as before)
    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _f(x, default=0.0):
        try:
            return float(x) if x is not None else default
        except (TypeError, ValueError):
            return default

    def _phase2_rule_based_scoring(self, tr: dict) -> dict:
        print("\n" + "─" * 60)
        print(" PHASE 2: rule-based scoring")
        print("─" * 60)

        screening = tr.get("screening_api_lookup", {}) or {}
        alert     = screening.get("alert", {}) or {}
        kyc_pack  = tr.get("core_banking_get_customer", {}) or {}
        adverse   = tr.get("get_adverse_media", {}) or {}
        ubo       = tr.get("get_ubo_chain", {}) or {}
        prior     = tr.get("case_management_prior_cases", {}) or {}
        registry  = tr.get("get_company_registry", {}) or {}

        base       = self._f(alert.get("match_score") or alert.get("confidence") or 0)
        adverse_n  = int(adverse.get("count", 0))
        ubo_found  = bool(ubo.get("has_ubo_chain"))
        large_n    = int(kyc_pack.get("large_transaction_count", 0))
        intl_n     = int(kyc_pack.get("international_transaction_count", 0))
        esc_n      = int(prior.get("prior_escalations", 0))
        clear_n    = int(prior.get("prior_clearances", 0))
        reg_n      = int(registry.get("count", 0))

        adverse_c  = min(adverse_n * 0.05, 0.20)
        ubo_c      = 0.10 if ubo_found else 0.0
        large_c    = min(large_n * 0.05, 0.15)
        intl_c     = min(intl_n  * 0.05, 0.15)
        esc_c      = min(esc_n * 0.10, 0.20)
        clr_pen    = -min(clear_n * 0.05, 0.20)
        reg_c      = min(reg_n * 0.05, 0.10)

        context_score        = adverse_c + ubo_c + large_c + intl_c + reg_c
        confidence_adjust    = esc_c + clr_pen
        final = max(0.0, min(1.0, base + context_score + confidence_adjust))

        if final >= 0.85:
            verdict = "TRUE_MATCH"
        elif final >= 0.65:
            verdict = "UNCERTAIN"
        else:
            verdict = "FALSE_POSITIVE"

        print(f"  base={base:.3f}  +context={context_score:+.3f}  "
              f"+confidence_adj={confidence_adjust:+.3f}  →  final={final:.3f}")
        print(f"  Score    : {final:.3f}")
        print(f"  Verdict  : {verdict}")
        print(f"  Confidence: {round(final * 100)}%")

        return {
            "base":              base,
            "context_score":     round(context_score, 3),
            "confidence_adjust": round(confidence_adjust, 3),
            "final_risk_score":  round(final, 3),
            "recommendation":    verdict,
            "confidence_pct":    round(final * 100),
            "adverse_n": adverse_n, "ubo_found": ubo_found,
            "large_n": large_n, "intl_n": intl_n,
            "esc_n": esc_n, "clear_n": clear_n,
            "registry_n": reg_n,
        }

    # ────────────────────────────────────────────────────────────────
    # PHASE 3 — LLM narrative
    # ────────────────────────────────────────────────────────────────
    def _phase3_llm_narrative(
        self, alert_id: str, tr: dict, score_pack: dict,
    ) -> str:
        print("\n" + "─" * 60)
        print(" PHASE 3: LLM narrative")
        print("─" * 60)

        sr  = tr.get("screening_api_lookup", {}) or {}
        cr  = tr.get("core_banking_get_customer", {}) or {}
        pr  = tr.get("case_management_prior_cases", {}) or {}
        am  = tr.get("get_adverse_media", {}) or {}

        alert    = sr.get("alert", {}) or {}
        kyc      = cr.get("kyc",   {}) or {}

        context = textwrap.dedent(f"""
            Alert ID            : {alert_id}
            Verdict (from rules): {score_pack['recommendation']}
            Confidence          : {score_pack['confidence_pct']}%

            Alert:
              matched_entity : {alert.get('matched_entity','')}
              source_list    : {alert.get('source_list','')}
              match_score    : {alert.get('match_score','')}
              customer_name  : {alert.get('customer_name','')}
              nationality    : {alert.get('nationality','')}

            Customer KYC:
              full_name      : {kyc.get('full_name','')}
              risk_rating    : {kyc.get('risk_rating','')}
              nationality    : {kyc.get('nationality','')}
              country        : {kyc.get('country_name','')}
              dob            : {kyc.get('dob','')}
              occupation     : {kyc.get('occupation','')}

            Transactions:
              total          : {cr.get('transaction_count', 0)}
              large (>5L)    : {score_pack['large_n']}
              intl / wire    : {score_pack['intl_n']}
              suspicious     : {cr.get('suspicious_pattern', False)}

            Prior history:
              clearances     : {score_pack['clear_n']}
              escalations    : {score_pack['esc_n']}
              recent_notes   : {(pr.get('analyst_notes') or [''])[:2]}

            Other signals:
              adverse_media  : {score_pack['adverse_n']} record(s)
              ubo_chain      : {'found' if score_pack['ubo_found'] else 'none'}
              registry_hits  : {score_pack['registry_n']}
        """).strip()

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=420,
                temperature=0.3,
                system=PHASE3_PROMPT,
                messages=[{"role": "user", "content": context}],
            )
            text = (resp.content[0].text or "").strip()
        except Exception as e:
            text = (f"Narrative generation failed: {e}. "
                    f"Verdict: {score_pack['recommendation']} "
                    f"at {score_pack['confidence_pct']}% confidence.")
        print(f"  Narrative generated ({len(text)} chars)")
        return text

    # ────────────────────────────────────────────────────────────────
    # Worksheet assembly (existing Worksheet shape — run_demo.py reads it)
    # ────────────────────────────────────────────────────────────────
    def _build_worksheet(
        self, alert_id: str, tr: dict, score_pack: dict,
        llm_narrative: str, blocked: list[str],
    ) -> Worksheet:
        sr  = tr.get("screening_api_lookup", {}) or {}
        cr  = tr.get("core_banking_get_customer", {}) or {}
        pr  = tr.get("case_management_prior_cases", {}) or {}
        am  = tr.get("get_adverse_media", {}) or {}
        ub  = tr.get("get_ubo_chain", {}) or {}
        reg = tr.get("get_company_registry", {}) or {}

        alert = sr.get("alert", {}) or {}
        kyc   = cr.get("kyc",   {}) or {}

        # Two-line narrative: the existing rule-based summary plus
        # the LLM-written plain-English explanation underneath.
        rule_narrative = (
            f"Initial match score {score_pack['base']:.2f} against "
            f"'{alert.get('matched_entity','')}'. "
            f"{sr.get('hit_count',0)} sanctions.db hit(s). "
            f"{score_pack['adverse_n']} adverse media. "
            f"{score_pack['large_n']} large / {score_pack['intl_n']} intl txns. "
            f"UBO chain: {'found' if score_pack['ubo_found'] else 'none'}. "
            f"Prior: {score_pack['clear_n']} clearance(s) / "
            f"{score_pack['esc_n']} escalation(s). "
            f"Final risk: {score_pack['final_risk_score']:.2f} → "
            f"{score_pack['recommendation']}."
        )

        # Compose the narrative field: rule-based one-liner on top, then
        # the LLM's plain-English explanation underneath. Worksheet schema
        # is untouched — both pieces live in the existing `narrative` field.
        combined_narrative = (
            f"{rule_narrative}\n\n— LLM ({self.model}) ——————————————\n"
            f"{llm_narrative}"
        )

        return Worksheet(
            alert_id            = alert_id,
            customer_id         = alert.get("customer_id", ""),
            customer_name       = alert.get("customer_name", ""),
            matched_entity      = alert.get("matched_entity", ""),
            source_list         = alert.get("source_list", ""),
            initial_match_score = float(alert.get("match_score") or 0),

            sanctions_db_hits = [
                SanctionsHit(
                    full_name   = h.get("full_name", "") or "",
                    program     = h.get("program", "")   or "",
                    source      = h.get("source", "")    or "",
                    nationality = h.get("nationality","")or "",
                    listed_on   = h.get("listed_on", "") or "",
                )
                for h in sr.get("sanctions_db_hits", [])
            ],
            kyc_summary = {
                "full_name":   kyc.get("full_name", ""),
                "nationality": kyc.get("nationality", ""),
                "country":     kyc.get("country_name", ""),
                "dob":         kyc.get("dob", ""),
                "risk_rating": kyc.get("risk_rating", ""),
                "occupation":  kyc.get("occupation", ""),
                "account_type":kyc.get("account_type", ""),
            },
            transactions = TransactionSummary(
                total              = int(cr.get("transaction_count", 0)),
                large_count        = score_pack["large_n"],
                international_count= score_pack["intl_n"],
                suspicious_pattern = bool(cr.get("suspicious_pattern", False)),
            ),
            adverse_media_count = score_pack["adverse_n"],
            ubo_chain_found     = score_pack["ubo_found"],
            registry_match_count= score_pack["registry_n"],
            prior_cases = PriorCaseSummary(
                total_cases            = int(pr.get("total_cases", 0)),
                prior_clearances       = score_pack["clear_n"],
                prior_escalations      = score_pack["esc_n"],
                most_recent_resolution = pr.get("most_recent_resolution", "none") or "none",
            ),

            context_score         = score_pack["context_score"],
            confidence_adjustment = score_pack["confidence_adjust"],
            final_risk_score      = score_pack["final_risk_score"],
            recommendation        = score_pack["recommendation"],

            narrative      = combined_narrative,
            blocked_actions= blocked,
        )


# ── Backwards-compat alias so run_demo.py still works untouched ───────
MockOrchestrator = HybridOrchestrator
