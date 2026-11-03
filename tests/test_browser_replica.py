"""Real-browser tests: the engine's Browser class in a real Chrome/Chromium against the
publisher replica (tests/browser_replica.py). Every request is answered inside the browser
by ctx.route and the browser's proxy is a dead local port, so no site is contacted and no
entitlement is needed.

Opt-in, because it needs a browser driver and a browser:
- Windows/macOS (PowerShell: $env:NAME = "value"; cmd.exe: set NAME=value; sh: export):
      PA_BROWSER_CHANNEL=chrome   (or msedge)   python -m pytest tests/test_browser_replica.py
- Linux without a display (CI):
      PA_BROWSER_EXECUTABLE=/path/to/chrome xvfb-run -a python -m pytest tests/test_browser_replica.py
- PA_BROWSER_DRIVER=playwright (default) or patchright selects the driver, as config
  browser_driver does.
Without PA_BROWSER_CHANNEL or PA_BROWSER_EXECUTABLE the tests are skipped.

Scope: the default capture path (PDFs download, browser_pdf_external=true). The rollback
setting (inline viewer) depends on how the viewer exposes its stream and on API-context
re-fetches, neither of which the replica can serve; the engine selftest covers its logic.
"""
import json
import os
import time

import pytest

DRIVER = os.environ.get("PA_BROWSER_DRIVER", "").strip() or "playwright"
driver_api = pytest.importorskip(f"{DRIVER}.sync_api")
CHANNEL = os.environ.get("PA_BROWSER_CHANNEL", "").strip()
EXECUTABLE = os.environ.get("PA_BROWSER_EXECUTABLE", "").strip()
if not (CHANNEL or EXECUTABLE):
    pytest.skip("set PA_BROWSER_CHANNEL (chrome/msedge) or PA_BROWSER_EXECUTABLE to run real-browser tests",
                allow_module_level=True)

from browser_replica import DEAD_PROXY, SCENARIOS, Replica  # noqa: E402
from paper_acquire.browser_channel import Browser  # noqa: E402

TIME_LIMIT_S = 60   # a success takes seconds here; the old failure path took about 90 s


@pytest.fixture
def browser_factory(tmp_path, monkeypatch):
    if EXECUTABLE:
        orig = driver_api.BrowserType.launch_persistent_context

        def launch(self, *args, **kw):
            kw.pop("channel", None)
            kw["executable_path"] = EXECUTABLE
            return orig(self, *args, **kw)
        monkeypatch.setattr(driver_api.BrowserType, "launch_persistent_context", launch)
    made = []

    def make(name, gate_tabs=0, allow_viewer=False):
        cfg = {"browser_channel": CHANNEL or "chrome", "browser_login_gate": False,
               "browser_driver": DRIVER,
               "browser_pdf_external": True,
               "browser_proxy": DEAD_PROXY}      # anything the replica misses fails, never leaks
        b = Browser(cfg, tmp_path / f"profile_{name}")
        made.append(b)
        if b._viewer_active and not allow_viewer:
            # Edge as the system PDF reader keeps its viewer (the self-test says so and
            # the engine falls back to viewer-stream capture); the replica serves only
            # the download path, which is the default
            pytest.skip("this browser keeps its PDF viewer (startup self-test); download-path test")
        rep = Replica()
        b.ctx.route("**/*", rep)
        rep.escaped = []
        b.ctx.on("requestfailed", lambda r: rep.escaped.append(r.url)
                 if "ERR_ABORTED" not in (r.failure or "") else None)   # downloads abort their navigation
        for _ in range(gate_tabs):   # login-gate tabs opened on top of the working tab
            t = b.ctx.new_page()
            t.goto("https://pub.example/article/A")
            t.bring_to_front()
        return b, rep
    yield make
    for b in made:
        try:
            b.close()
        except Exception:
            pass


def _fetch(b, rep, scenario, tmp_path, n=0):
    dest = tmp_path / f"{scenario}{n}.pdf"
    t0 = time.time()
    res, detail, _ = b.fetch(rep.doi(scenario, n), str(dest))
    return res, detail, time.time() - t0, dest


@pytest.mark.parametrize("scenario", ["A", "B", "C", "D", "E", "G", "J", "P", "R"])
def test_scenario_yields_the_pdf(browser_factory, tmp_path, scenario):
    """Each failure mode seen in real runs, modelled in the replica, ends with the PDF saved."""
    b, rep = browser_factory(scenario, gate_tabs=3 if scenario == "G" else 0)
    res, detail, seconds, dest = _fetch(b, rep, scenario, tmp_path)
    assert res == "ok", (SCENARIOS[scenario][0], res, detail)
    assert dest.read_bytes()[:5] == b"%PDF-"
    assert seconds < TIME_LIMIT_S, (seconds, detail)
    assert len(b.ctx.pages) == (4 if scenario == "G" else 1), "tabs opened by the attempt were left open"
    assert not rep.escaped, f"requests escaped the replica: {rep.escaped}"


def test_one_session_many_papers(browser_factory, tmp_path):
    """A working day in miniature: one browser session behind login-gate tabs, several papers
    from the same sites in a row (catches per-site download throttling and tab or
    listener leaks that a fresh profile per paper would hide)."""
    b, rep = browser_factory("session", gate_tabs=3)
    plan = [("A", 1), ("A", 2), ("B", 1), ("C", 1), ("D", 1), ("E", 1), ("J", 1), ("P", 1), ("R", 1), ("A", 3)]
    results = []
    for s, n in plan:
        res, detail, seconds, dest = _fetch(b, rep, s, tmp_path, n)
        results.append((s, n, res, round(seconds, 1), detail[:60]))
    assert all(r[2] == "ok" for r in results), results
    assert len(b.ctx.pages) == 4, "tabs accumulated over the session"
    left = [p.name for p in b.downloads_dir.iterdir()]
    assert left == [], f"v0.5.3: saved downloads must not stay in browser_downloads/: {left}"
    trace = json.dumps(b.last_trace)
    assert '"ev": "download"' in trace and "://" not in trace, trace   # trace: classes only
    assert not rep.escaped, f"requests escaped the replica: {rep.escaped}"


def test_block_page_strikes_at_once(browser_factory, tmp_path):
    """ScienceDirect's 403 block page is challenge class: 'blocked' (a strike toward the
    publisher's day stop) at once, never a per-paper no-access verdict."""
    b, rep = browser_factory("K")
    res, detail, seconds, _ = _fetch(b, rep, "K", tmp_path)
    assert res == "blocked" and "block page" in detail, (res, detail)
    assert seconds < 30, seconds


def test_startup_selftest_proves_pdfs_download(browser_factory):
    """The launch self-test (127.0.0.1 only) sees a real download on this browser. Edge
    acting as the system PDF reader keeps its viewer; the self-test must then say so
    (the engine prints the warning and falls back to viewer-stream capture)."""
    b, _ = browser_factory("selftest", allow_viewer=True)
    if CHANNEL == "msedge" and b._viewer_active:
        assert b._pdf_selftest() is False      # detected, not mistaken for a download
        return
    assert b._viewer_active is False
    assert b._pdf_selftest() is True


@pytest.fixture
def stalling_server():
    """Sends PDF headers and 2 kB, then holds the connection open (a stalled transfer)."""
    import http.server
    import threading
    release = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(5_000_000))
            self.end_headers()
            self.wfile.write(b"%PDF-1.4\n" + b"0" * 2048)
            self.wfile.flush()
            release.wait(120)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/stall.pdf"
    release.set()
    srv.shutdown()


def test_stalled_download_is_cancelled_not_hung(browser_factory, tmp_path, stalling_server):
    b, rep = browser_factory("S")
    rep.stall_url = stalling_server
    b._stall_s = 3
    res, detail, seconds, _ = _fetch(b, rep, "S", tmp_path)
    # the click window (30 s) bounds it: the stalled transfer is cancelled, the same link
    # is not navigated again, and no request-API fallback follows a browser download
    assert res != "ok" and seconds < 45, (res, seconds, detail)
    assert any(e["ev"] == "download_stalled" for e in b.last_trace), b.last_trace
    assert sum(1 for e in b.last_trace if e["ev"] == "download") <= 1, b.last_trace


@pytest.fixture
def slow_server():
    """Serves a 600 kB PDF in 30 kB pieces every 0.4 s (about 8 s): slow but moving."""
    import http.server
    import threading
    body = b"%PDF-1.4\n" + b"0" * (600_000 - 15) + b"%%EOF\n"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 30000):
                self.wfile.write(body[i:i + 30000])
                self.wfile.flush()
                time.sleep(0.4)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/slow.pdf", len(body)
    srv.shutdown()


def test_slow_download_is_not_cancelled(browser_factory, tmp_path, slow_server):
    """The stall guard must see a transfer's progress on this OS: a download that keeps
    moving for longer than the stall window is saved, not cancelled (v0.5.2)."""
    url, size = slow_server
    b, rep = browser_factory("W")
    rep.stall_url = url
    b._stall_s = 3
    res, detail, seconds, dest = _fetch(b, rep, "S", tmp_path)
    assert res == "ok", (res, detail, b.last_trace)
    assert dest.stat().st_size == size and seconds > 6, (dest.stat().st_size, seconds)
    assert not any(e["ev"] == "download_stalled" for e in b.last_trace), b.last_trace


def test_no_access_landing_is_a_verdict(browser_factory, tmp_path):
    b, rep = browser_factory("N")
    res, detail, _, _ = _fetch(b, rep, "N", tmp_path)
    assert res == "no_access", (res, detail)


def test_fetch_url_takes_a_late_pdf_after_a_relay(browser_factory, tmp_path):
    b, _ = browser_factory("url")
    dest = tmp_path / "u.pdf"
    res, detail = b.fetch_url("https://pub.example/pdf/E", str(dest))
    assert res == "ok" and dest.read_bytes()[:5] == b"%PDF-", (res, detail)
