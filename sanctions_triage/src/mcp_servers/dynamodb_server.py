"""
MCP server exposing the 5 DynamoDB-backed tools to Claude Desktop.
All tools delegate to src/tools.py — no logic duplication.
"""
from __future__ import annotations
import sys
from pathlib import Path

# Make src/ imports work whether launched from repo root or src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP
from tools import (
    screening_api_lookup,
    core_banking_get_customer,
    get_adverse_media,
    get_company_registry,
    get_ubo_chain,
)

mcp = FastMCP("sanctions-dynamodb")

@mcp.tool()
def screening_lookup(alert_id: str) -> dict:
    """Fetch a sanctions alert from DynamoDB and search sanctions.db for the matched entity. Call this first to learn customer_id and entity_name."""
    return screening_api_lookup(alert_id)

@mcp.tool()
def kyc_lookup(customer_id: str) -> dict:
    """Fetch full customer KYC and last 10 transactions for a given customer_id."""
    return core_banking_get_customer(customer_id)

@mcp.tool()
def adverse_media_lookup(customer_id: str, customer_name: str = "") -> dict:
    """Search adverse media records linked to this customer (by person name)."""
    return get_adverse_media(customer_id, customer_name)

@mcp.tool()
def company_registry_lookup(entity_name: str) -> dict:
    """Check company registry for corporate ties to this entity (company_name or director person_name match)."""
    return get_company_registry(entity_name)

@mcp.tool()
def ubo_chain_lookup(customer_id: str, entity_name: str = "") -> dict:
    """Get the Ultimate Beneficial Owner chain for the sanctioned entity (linked by entity_name)."""
    return get_ubo_chain(customer_id, entity_name)

if __name__ == "__main__":
    mcp.run()
