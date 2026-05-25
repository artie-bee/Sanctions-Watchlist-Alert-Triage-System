"""
MCP server exposing direct SQLite search over the 66k-row sanctions.db.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP
from tools import sanctions_db_search

mcp = FastMCP("sanctions-db")

@mcp.tool()
def search_sanctions(query: str, limit: int = 10) -> list[dict]:
    """Returns sanctions hits matching `query` from the 66k-row sanctions.db. Use this for ad-hoc analyst lookups: 'show me all sanctions records for MELNIK'."""
    return sanctions_db_search(query, limit)

if __name__ == "__main__":
    mcp.run()
