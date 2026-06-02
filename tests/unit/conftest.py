"""Shared test setup.

The src modules use bare sibling imports (`from hooks import ...`,
`from worksheet import ...`), so the test process needs
sanctions_triage/src on sys.path before importing them.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "sanctions_triage" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
