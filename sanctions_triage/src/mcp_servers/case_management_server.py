"""
MCP server exposing prior-cases lookup (file-backed, not DynamoDB).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP
from tools import case_management_prior_cases

mcp = FastMCP("sanctions-case-management")

@mcp.tool()
def prior_cases(name: str) -> dict:
    """Returns prior alert history for this customer name via fuzzy match against prior_cases.json. Counts of clearances and escalations, plus the case list."""
    return case_management_prior_cases(name)

if __name__ == "__main__":
    mcp.run()
