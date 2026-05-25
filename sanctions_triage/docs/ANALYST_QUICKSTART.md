# Analyst Quickstart — Sanctions MCP Servers

This project ships 4 Model Context Protocol (MCP) servers that expose the bank's sanctions-triage data sources directly to Claude Desktop. Once installed, you can ask Claude in plain English to look up KYC, scan adverse media, search the sanctions feed, and read the audit trail — Claude picks the right tool and calls it for you.

## The 4 servers

| Server | What it does |
|---|---|
| `sanctions-dynamodb` | KYC lookup, transactions, adverse media, company registry, UBO chain (5 DynamoDB-backed tools). |
| `sanctions-db` | Full-text search over the 66k-row sanctions feed (`sanctions.db`). |
| `sanctions-case-management` | Prior-case fuzzy lookup against `prior_cases.json`. |
| `sanctions-audit-log` | Read-only access to the per-alert audit log (events, tools called, blocked attempts). |

## Installation

1. Copy the contents of `claude_desktop_config_snippet.json` (in this folder) into:

   ```
   %APPDATA%\Claude\claude_desktop_config.json
   ```

   If that file already exists, merge the `mcpServers` block — don't overwrite existing entries.

2. **Fully quit** Claude Desktop (system tray → right-click → Quit). Closing the window is not enough.

3. Relaunch Claude Desktop and open a new chat. Click the slider/tool icon in the input area — you should see all 4 servers listed.

## Example questions

Type any of these in plain English. Claude picks the right tool automatically.

- **Case history**: `Show me prior cases for entity kazenska K. Al Khalifa`
- **Sanctions feed**: `Search the sanctions database for anyone named MELNIK`
- **Audit trail**: `Summarize the audit log for alert ALR-7DDF56CADE — what tools were called and were any blocked?`

## Closing alerts is human-only

The agent and these MCP servers can read everything, but they cannot close a sanctions alert. The `close_alert` operation is permanently blocked by a PreToolUse policy hook (PMLA 2002 / RBI KYC Master Direction 2025). Only a human analyst can record a disposition.

## Pre-flight

- DynamoDB Local must be running on port 8001 for `sanctions-dynamodb` tools.
- `sanctions-db` and `sanctions-case-management` only need their local files (`sanctions.db` and `data/prior_cases.json`) — they work offline.
- `sanctions-audit-log` reads `runtime/audit_log.jsonl`. If no alerts have been processed yet, the log will be empty.
