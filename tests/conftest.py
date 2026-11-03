"""Shared setup for the offline paper_acquire test suite.

Run from the repository root:

    pip install -e ".[test]"
    python -m pytest

The suite is offline. HTTP sessions and the browser are stubs, the engine's own
selftest runs in a temporary directory, and every engine instance created here works
inside pytest's tmp_path. No real project state is read or written. The real-browser
suite (tests/test_browser_replica.py) is opt-in and skipped by default.
"""
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]  # the repository root, which holds the package paper_acquire/

# Put the engine package first on sys.path. From the repository root the outer
# paper_acquire/ directory would otherwise import as an empty namespace package.
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))
