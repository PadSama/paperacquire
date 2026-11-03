"""Diagnostic probe, not a test: how a real Chrome/Edge stores an in-flight download on
this OS, which the engine's stall guard (Browser._bounded_save) depends on.

Serves everything from 127.0.0.1; the browser's proxy is a dead local port, so nothing
else is contacted. Prints a timeline of the downloads folder (every entry, hidden ones
included) and of new download-like entries in the user Downloads and temp folders,
during (1) a transfer that stalls after 2 kB and (2) a slow transfer that completes.

    PA_BROWSER_CHANNEL=chrome python tests/probe_downloads.py
    PA_BROWSER_EXECUTABLE=/path/to/chrome xvfb-run -a python tests/probe_downloads.py
"""
import http.server
import os
import tempfile
import threading
import time
from pathlib import Path

import importlib

DRIVER = os.environ.get("PA_BROWSER_DRIVER", "").strip() or "playwright"
sync_playwright = importlib.import_module(f"{DRIVER}.sync_api").sync_playwright

CHANNEL = os.environ.get("PA_BROWSER_CHANNEL", "").strip() or "chrome"
EXECUTABLE = os.environ.get("PA_BROWSER_EXECUTABLE", "").strip()
SLOW_N = 600_000
HOLD = threading.Event()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/stall"):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(5_000_000))
            self.end_headers()
            self.wfile.write(b"%PDF-1.4\n" + b"0" * 2048)
            self.wfile.flush()
            HOLD.wait(40)
        elif self.path.startswith("/slow"):
            body = b"%PDF-1.4\n" + b"0" * (SLOW_N - 15) + b"%%EOF\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for i in range(0, len(body), 30000):     # about 6 s in total
                self.wfile.write(body[i:i + 30000])
                self.wfile.flush()
                time.sleep(0.3)
        else:
            page = (b"<!doctype html><title>probe</title>"
                    b"<a id=s href='/stall.pdf'>stall</a> <a id=w href='/slow.pdf'>slow</a>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    def log_message(self, *a):
        pass


def entries(d, both=False):
    """name -> size from os.stat; with both=True, "listing size/stat size" so a stale
    directory-listing size (Windows, file still being written) shows up."""
    out = {}
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    st = os.stat(e.path).st_size
                except OSError as exc:
                    st = f"stat failed: {type(exc).__name__}"
                if both:
                    try:
                        listed = e.stat().st_size
                    except OSError as exc:
                        listed = type(exc).__name__
                    out[e.name] = f"{listed}/{st}"
                else:
                    out[e.name] = st
    except OSError as exc:
        out["<scandir failed>"] = type(exc).__name__
    return out


def new_downloadish(d, before):
    hits = {}
    for name, size in entries(d).items():
        low = name.lower()
        if name not in before and (low.endswith((".crdownload", ".tmp", ".partial", ".pdf"))
                                   or low.startswith(("unconfirmed", ".com.google", ".org.chromium"))):
            hits[name] = size
    return hits


def timeline(label, page, dl_dir, extra_dirs, seconds):
    t0 = time.time()
    before = {d: set(entries(d)) for d in extra_dirs}
    while time.time() - t0 < seconds:
        line = {"downloads_path (listing/stat)": entries(dl_dir, both=True)}
        for d in extra_dirs:
            hits = new_downloadish(d, before[d])
            if hits:
                line[str(d)] = hits
        print(f"  [{label} +{time.time() - t0:4.1f}s] {line}", flush=True)
        page.wait_for_timeout(500)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    work = Path(tempfile.mkdtemp(prefix="pa_probe_"))
    dl_dir = work / "browser_downloads"
    extra = [p for p in (Path.home() / "Downloads", Path(tempfile.gettempdir())) if p.is_dir()]
    print(f"probe: channel={CHANNEL or '-'} executable={'set' if EXECUTABLE else '-'} os={os.name}", flush=True)
    with sync_playwright() as pw:
        kw = dict(headless=False, accept_downloads=True, downloads_path=str(dl_dir),
                  proxy={"server": "http://127.0.0.1:9", "bypass": "127.0.0.1,localhost"})
        if EXECUTABLE:
            kw["executable_path"] = EXECUTABLE
        else:
            kw["channel"] = CHANNEL
        prefs = work / "profile" / "Default"
        prefs.mkdir(parents=True)
        (prefs / "Preferences").write_text('{"plugins": {"always_open_pdf_externally": true}}')
        ctx = pw.chromium.launch_persistent_context(str(work / "profile"), **kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"browser version: {ctx.browser.version if ctx.browser else 'n/a'}", flush=True)
        page.goto(base + "/")
        seen = []
        ctx.on("download", lambda d: seen.append(d))

        print("1) stalled transfer (2 kB, then nothing)", flush=True)
        page.click("#s")
        timeline("stall", page, dl_dir, extra, 10)
        print(f"  downloads seen: {len(seen)}", flush=True)
        if seen:
            t = time.time()
            seen[0].cancel()
            print(f"  cancel() returned in {time.time() - t:.1f}s", flush=True)
            timeline("after cancel", page, dl_dir, extra, 2)
            t = time.time()
            try:
                seen[0].save_as(str(work / "stalled.pdf"))
                print(f"  save_as after cancel: saved ({time.time() - t:.1f}s)", flush=True)
            except Exception as e:
                print(f"  save_as after cancel: raised {type(e).__name__} in {time.time() - t:.1f}s", flush=True)

        print("2) slow transfer that completes (600 kB over ~6 s)", flush=True)
        n0 = len(seen)
        page.goto(base + "/")      # a browser that kept its PDF viewer has left the page
        page.click("#w")
        timeline("slow", page, dl_dir, extra, 9)
        if len(seen) > n0:
            t = time.time()
            try:
                seen[-1].save_as(str(work / "slow.pdf"))
                size = (work / "slow.pdf").stat().st_size
                print(f"  save_as: saved {size} bytes in {time.time() - t:.1f}s", flush=True)
            except Exception as e:
                print(f"  save_as raised {type(e).__name__} in {time.time() - t:.1f}s", flush=True)
            timeline("after save", page, dl_dir, extra, 1)
        HOLD.set()
        ctx.close()
    srv.shutdown()
    print("probe done", flush=True)


if __name__ == "__main__":
    main()
