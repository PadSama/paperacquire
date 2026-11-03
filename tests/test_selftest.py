"""The engine's offline acceptance gate, run under pytest.

`python -m paper_acquire --project <dir> selftest` checks the waterfall order, the
publisher mapping, the privacy-gate fuse, the v0.4 state machine and the v0.4.8
browser-channel semantics (README.md). This test runs that exact
command in a subprocess, so the selftest's stubs cannot leak into other tests.
"""
import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def test_engine_selftest_cli(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "paper_acquire", "--project", str(tmp_path / "project"), "selftest"],
        cwd=ENGINE_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    assert proc.returncode == 0, f"selftest failed\n--- stdout\n{proc.stdout[-3000:]}\n--- stderr\n{proc.stderr[-6000:]}"
    assert "selftest PASS" in proc.stdout
