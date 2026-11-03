"""CLI: python -m paper_acquire --project <dir> <command>

Commands:
  init --records CSV_OR_JSONL   validate inputs, write config.json from example if absent
  status                         per-state counts + today's fuses
  fetch-day [--no-browser]      one polite acquisition pass over pending records
  pack [--max-mb 8]             privacy-gated zip of new fulltexts + records for sharing
  selftest                      OFFLINE: config schema, waterfall order, privacy-gate fuse
  reopen --status FILE [--step S] [--uids A,B] [--records F --doi-prefix P] [--note N] [--apply]
                                 deliberate state repair: fresh retry budget for one step on
                                 records a tool defect exhausted (dry run unless --apply)
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.json"   # shipped with the package


def load_records(project: Path):
    for name in ("records.csv", "records.jsonl"):
        p = project / "inputs" / name
        if p.exists():
            if name.endswith(".csv"):
                with p.open(encoding="utf-8-sig") as f:
                    return {r["uid"]: {"uid": r["uid"], "DOI": r.get("DOI", ""), "Title": r.get("Title", "")}
                            for r in csv.DictReader(f)}
            out = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    out[r["uid"]] = {"uid": r["uid"], "DOI": r.get("DOI", ""), "Title": r.get("Title", "")}
            return out
    raise SystemExit("no inputs/records.csv|jsonl under the project (run init)")


def load_candidates(project: Path):
    p = project / "inputs" / "candidates.csv"
    out = {}
    if p.exists():
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                out.setdefault(r["uid"], []).append({"url": r["url"], "version": r.get("version") or None})
    return out


def load_config(project: Path):
    p = project / "config.json"
    if not p.exists():
        example = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        p.write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"config.json created from example - fill contact_email and privacy_markers: {p}")
        raise SystemExit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_init(a):
    project = Path(a.project).resolve()
    (project / "inputs").mkdir(parents=True, exist_ok=True)
    (project / "inputs" / "inhand").mkdir(exist_ok=True)
    for d in ("fulltext", "logs", "work/tmp", "results", "return"):
        (project / d).mkdir(parents=True, exist_ok=True)
    recs = load_records(project)
    cfg = load_config(project)
    print(f"project ready: {len(recs)} records; contact_email "
          f"{'set' if cfg.get('contact_email') else 'MISSING'}; "
          f"privacy_markers {len(cfg.get('privacy_markers') or [])} listed")


def cmd_status(a):
    project = Path(a.project).resolve()
    recs = load_records(project)
    st = json.loads((project / "work" / "status.json").read_text(encoding="utf-8")) \
        if (project / "work" / "status.json").exists() else {}
    counts = {"acquired": 0, "pending": 0, "not_found": 0, "untouched": 0}
    for uid in recs:
        counts[{"acquired": "acquired", "not_found": "not_found"}.get(st.get(uid, {}).get("state"), "pending")
               if uid in st else "untouched"] += 1
    print(json.dumps(counts, ensure_ascii=False))
    day = project / "work" / "day" / f"{time.strftime('%Y-%m-%d')}.json"
    if day.exists():
        d = json.loads(day.read_text(encoding="utf-8"))
        if d.get("blocked"):
            print("fused today:", ", ".join(d["blocked"]))


def cmd_fetch_day(a):
    project = Path(a.project).resolve()
    cfg, recs = load_config(project), load_records(project)
    from .engine import Acquirer, NetworkDown
    acq = Acquirer(project, cfg, a)
    acq.candidates = load_candidates(project)
    todo = [u for u in recs if acq.status.get(u, {}).get("state") != "acquired"
            and acq.status.get(u, {}).get("state") != "not_found"]
    print(f"fetch-day {time.strftime('%Y-%m-%d')}: {len(todo)} of {len(recs)} records to try")
    ok = defer = nf = 0
    try:
        for i, u in enumerate(todo, 1):
            res = None
            while True:
                try:
                    res = acq.process(u, recs[u])
                    break
                except NetworkDown:
                    if acq.wait_for_network():
                        continue    # network returned: retry the same record, nothing failed
                    print("\nnetwork down beyond the patience budget: stopped in place, "
                          "nothing recorded as failed.")
                    res = None
                    break
                except Exception as e:
                    acq.log(u, "process", "", "unknown", "fail", None, f"{type(e).__name__}: {e}")
                    acq.set_status(u, state="pending", next_step=None, error=str(e)[:200])
                    res = "error(parked)"
                    break
            if res is None:
                break
            ok += res == "acquired"
            defer += res == "deferred"
            nf += res == "not_found"
            print(f"  [{i}/{len(todo)}] {u}  {res}")
    finally:
        acq.close()
    print(f"summary: acquired {ok} (total {sum(1 for s in acq.status.values() if s.get('state') == 'acquired')}), "
          f"deferred {defer}, not_found {nf}")


def cmd_pack(a):
    project = Path(a.project).resolve()
    cfg = load_config(project)
    files = [p for p in (project / "fulltext").glob("*.pdf")]
    for rel in ("work/status.json", "logs/attempts.jsonl"):
        if (project / rel).exists():
            files.append(project / rel)
    from .packer import write_pack
    parts = write_pack(project, files, "pa_upload", a.max_mb)
    print("pack(s) written (privacy gate passed):")
    for p in parts:
        print("  ", p)


def _state_machine_tests():
    """Offline v0.4 semantics: fall-through defers, retry budgets, lazy migration."""
    import tempfile
    from types import SimpleNamespace
    from .engine import Acquirer
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        cfg = {"limits": {}, "browser_enabled": False, "contact_email": "", "privacy_markers": []}
        args = SimpleNamespace(no_browser=True)
        def rec(uid):
            return {"uid": uid, "DOI": "10.1/x", "Title": "t"}

        def mk(step_map):
            return Acquirer(proj, cfg, args, steps=list(step_map), extra_steps=step_map)

        # defer must fall through: an upstream fused step may not starve later steps
        acq = mk({"a": lambda u, r: "defer", "b": lambda u, r: "ok", "c": lambda u, r: "ok"})
        assert acq.process("T1", rec("T1")) == "acquired", "defer did not fall through"
        # pure deferral stays pending with the earliest deferred step, never not_found
        acq = mk({"a": lambda u, r: "defer", "b": lambda u, r: "defer", "c": lambda u, r: "defer"})
        assert acq.process("T2", rec("T2")) == "deferred"
        s = acq.status["T2"]
        assert s["state"] == "pending" and s["next_step"] == "a", str(s)
        # retry budget (default 2 for unknown steps): fail once -> retry; twice -> exhausted;
        # not_found only after EVERY step exhausted, never on transient failure alone
        acq = mk({"a": lambda u, r: "fail", "b": lambda u, r: "fail", "c": lambda u, r: "fail"})
        assert acq.process("T3", rec("T3")) == "retry: a"
        assert acq.status["T3"]["step_state"]["a"]["tries"] == 1
        assert acq.process("T3", rec("T3")) == "not_found"
        assert acq.status["T3"]["state"] == "not_found"
        # deterministic exhaust (API 403 class) consumes the channel in one shot
        acq = mk({"a": lambda u, r: "exhaust", "b": lambda u, r: "exhaust", "c": lambda u, r: "exhaust"})
        assert acq.process("T4", rec("T4")) == "not_found"
        # legacy steps_done migrates to one spent try that stays open (starved step recovers)
        acq = mk({"a": lambda u, r: "ok", "b": lambda u, r: "ok", "c": lambda u, r: "ok"})
        acq.status["T5"] = {"state": "pending", "steps_done": ["a"], "tried_urls": ["u1"], "api_403": True}
        assert acq.process("T5", rec("T5")) == "acquired"
        assert acq.status["T5"]["tried_urls"] == ["u1"] and "api_403" not in acq.status["T5"]

        # a composite step whose every candidate was fuse-skipped
        # made zero real attempts - it must defer WITHOUT burning retry budget
        acq = Acquirer(proj, cfg, args, steps=["candidates"])
        acq.candidates = {"T6": [{"url": "https://x/y.pdf", "version": "published"}]}
        acq.try_pdf = lambda *a, **k: "skip_fuse"
        assert acq.process("T6", rec("T6")) == "deferred"
        assert not acq.status["T6"]["step_state"].get("candidates", {}).get("tries"), \
            "a skip day must not burn budget"
        # ...while a real failed attempt does burn budget
        acq = Acquirer(proj, cfg, args, steps=["candidates"])
        acq.candidates = {"T7": [{"url": "https://x/y.pdf", "version": "published"}]}
        acq.try_pdf = lambda *a, **k: "fail"
        assert acq.process("T7", rec("T7")) == "retry: candidates"
        assert acq.status["T7"]["step_state"]["candidates"]["tries"] == 1
        # halt: project semantics forbid falling through; later steps untouched, no burn
        seen = []
        acq = Acquirer(proj, cfg, args, steps=["a", "b"])
        acq.extra_steps = {"a": lambda u, r: "halt", "b": lambda u, r: seen.append(1) or "ok"}
        assert acq.process("T8", rec("T8")) == "deferred"
        assert not seen and acq.status["T8"]["next_step"] == "a"

        # network wait-retry: a dropout pauses and probes instead of aborting the run
        acq = Acquirer(proj, cfg, args, steps=["a"])
        acq._nap = lambda s: None
        probes = {"n": 0}
        acq.offline = lambda: (probes.__setitem__("n", probes["n"] + 1) or probes["n"] < 3)
        assert acq.wait_for_network() is True and probes["n"] == 3
        acq = Acquirer(proj, {"limits": {"network_retry_max_s": 10, "network_retry_gap_s": 3},
                              "browser_enabled": False}, args, steps=["a"])
        slept = []
        acq._nap = slept.append
        acq.offline = lambda: True
        assert acq.wait_for_network() is False and sum(slept) >= 10

        # partial entitlement: a clean-landing PDF 403 is a per-paper verdict - the
        # browser step exhausts for THIS record and the publisher is never fused
        cfg_be = {"limits": {"browser_gap_s": [0, 0]}, "browser_enabled": True}
        args_be = SimpleNamespace(no_browser=False)
        acq = Acquirer(proj, cfg_be, args_be, steps=["browser"])

        class StubBrowser:
            def __init__(self, result):
                self.result = result

            def fetch(self, doi, dest):
                return self.result

        rec_be = {"uid": "T9", "DOI": "10.1080/x", "Title": "t"}
        acq.browser = StubBrowser(("no_entitlement", "HTTP 403", "https://x/y"))
        assert acq.process("T9", rec_be) == "not_found"      # sole step, deterministic verdict
        assert "tandf" not in acq.day.d["blocked"], "entitlement 403 must not fuse the publisher"
        assert acq.status["T9"].get("browser_verdict", "").startswith("no_entitlement")

        # script-challenged URL rides the browser as fallback and can acquire
        acq = Acquirer(proj, cfg_be, args_be, steps=["browser"])
        acq.status["T10"] = {"state": "pending", "browser_fallback": ["https://repo/x.pdf"]}
        acq.browser = StubBrowser(None)

        def stub_fetch_url(u, d):
            Path(d).write_bytes(b"%PDF-stub")
            return ("ok", "")
        acq.browser.fetch_url = stub_fetch_url
        acq.accept_pdf = lambda tmp, title, r=0.6: (True, "stub")
        assert acq.process("T10", rec("T10")) == "acquired"
        assert acq.status["T10"]["channel"] == "browser_fallback"
        assert acq.status["T10"].get("browser_fallback") == []

        # no_access (definitive landing evidence): same per-paper terminal
        # as no_entitlement - exhausts THIS record's browser step, never fuses emerald
        acq = Acquirer(proj, cfg_be, args_be, steps=["browser"])
        acq.browser = StubBrowser(("no_access", "landing says no access", "https://x/y"))
        rec_na = {"uid": "T11", "DOI": "10.1108/x", "Title": "t"}
        assert acq.process("T11", rec_na) == "not_found"
        assert "emerald" not in acq.day.d["blocked"], "no_access must not fuse the publisher"
        assert acq.status["T11"].get("browser_verdict", "").startswith("no_access")


def _browser_channel_tests():
    """Offline v0.4.3 semantics: state-based challenge detection, no_access evidence,
    SD PII derivation, navigation-first PDF acquisition, affordance-contextual 403
    verdicts, ctx-level pdf-stream capture for inline viewers."""
    import tempfile
    from types import SimpleNamespace
    from . import browser_channel as bc
    from .browser_channel import Browser

    # interstitial title is decisive
    assert bc.is_interstitial("Just a moment...", "<html><body>checking</body></html>")
    assert bc.is_interstitial("Request unsuccessful. Incapsula incident...", "<html/>")
    # a login-widget captcha inside a NORMAL article page is not a challenge
    long_article = ("<html><body>" + "<p>economic reform outcomes across countries text. </p>" * 300 +
                    "<div id='personal-login'>captcha for personal accounts</div></body></html>")
    assert not bc.is_interstitial("Reform outcomes - Journal", long_article)
    # a challenge-DOMINATED near-empty page still matches
    assert bc.is_interstitial("", "<html><body>please verify you are human captcha</body></html>")
    assert not bc.is_interstitial("Article", long_article.replace("captcha", "text"))

    # no_access evidence requires the text AND zero affordance
    assert bc.no_access_evidence("<p>You do not currently have access to this content</p>", False)
    assert not bc.no_access_evidence("<p>You do not currently have access to this content</p>", True)
    assert not bc.no_access_evidence("<p>full text available for this article</p>", False)

    # SD PII derives from the landing URL (both /pii/ and /abs/pii/ variants), never the DOI
    assert bc.sd_pdfft_from_landing(
        "https://www.sciencedirect.com/science/article/abs/pii/S0000000018000002") == \
        ("https://www.sciencedirect.com/science/article/pii/S0000000018000002"
         "/pdfft?download=true")   # v0.5.1: the ?isDTMRedir= form is dead since 2024-10
    assert bc.sd_pdfft_from_landing(
        "https://www.sciencedirect.com/science/article/pii/S0000000018000002?via%3Dihub")
    assert bc.sd_pdfft_from_landing("https://journals.sagepub.com/doi/10.1177/x") is None

    class _Resp:
        def __init__(self, status, body=b"", headers=None, url="https://stub/pdf"):
            self.status, self._b = status, body
            self.headers = headers or {}
            self.url = url

        def body(self):
            return self._b

    class _El:
        def __init__(self, attr, page=None, name=None):
            self._attr = attr
            self.clicked = 0
            self._page, self._name = page, name

        def get_attribute(self, _):
            return self._attr

        def click(self, timeout=None):
            self.clicked += 1
            if self._page is not None:
                self._page._clicked(self._name)

        def text_content(self):
            return self._attr

    class _Download:
        def __init__(self, body, url="https://stub/download.pdf"):
            self._b, self.url = body, url

        def save_as(self, path):
            Path(path).write_bytes(self._b)

        def delete(self):
            pass

    class _Page:
        """Landing-tab stand-in. dl_body: a click on any control fires a download with
        that body (v0.5.0: downloads are events on the tab, caught at ctx level);
        nav_download: {url-pattern: body} navigations that turn into a download (viewer
        off); click_hooks: {link-name: fn(page)} to model what a click changes."""

        def __init__(self, content="", title="", pdf_resp=None, meta=None, links=None,
                     raise_nav=False, by_url=None, dl_body=None,
                     url="https://www.emerald.com/insight/content/doi/10.1108/x/full/html",
                     emit=None, nav_download=None, click_hooks=None, json_state=None):
            self._c, self._t, self._resp = content, title, pdf_resp
            self._meta, self._links, self._raise = meta, links or {}, raise_nav
            self._by_url = by_url or {}
            self._dl_body = dl_body
            self._emit = list(emit or [])   # queued ctx responses, fired on wait
            self._nav_dl = nav_download or {}
            self._hooks = click_hooks or {}
            self._json = json_state
            self._dl_handlers = []
            self.ctx = None
            self.url = url
            self.gotos, self.clicks, self.fronts, self.closed = [], [], 0, False

        def on(self, ev, h):
            if ev == "download":
                self._dl_handlers.append(h)

        def remove_listener(self, ev, h):
            if ev == "download" and h in self._dl_handlers:
                self._dl_handlers.remove(h)

        def fire_download(self, body, url="https://stub/download.pdf"):
            for h in list(self._dl_handlers):
                h(_Download(body, url))

        def _clicked(self, name):
            self.clicks.append(name)
            if name in self._hooks:
                self._hooks[name](self)
            elif self._dl_body is not None:
                self.fire_download(self._dl_body)

        def bring_to_front(self):
            self.fronts += 1

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def goto(self, url, **kw):
            self.gotos.append(url)
            if url.startswith("https://doi.org/"):
                return _Resp(200)
            if self._raise:
                raise RuntimeError("nav timeout")
            for pat, body in self._nav_dl.items():
                if pat in url:
                    self.fire_download(body, url)
                    raise RuntimeError("Download is starting")
            for pat, r in self._by_url.items():
                if pat in url:
                    return r
            return self._resp

        def expect_download(self, timeout=None):
            body = self._dl_body
            if body is None:
                raise TimeoutError("no download event")

            class _Val:
                def save_as(self, path):
                    Path(path).write_bytes(body)

            class _DL:
                def __init__(self):
                    self.value = _Val()

            class _Ctx:
                def __enter__(self):
                    return _DL()

                def __exit__(self, *a):
                    return False
            return _Ctx()

        def title(self):
            return self._t

        def content(self):
            return self._c

        def query_selector(self, sel):
            if sel == 'meta[name="citation_pdf_url"]' and self._meta:
                return _El(self._meta)
            if sel == 'script[type="application/json"]':
                return _El(self._json) if self._json else None
            for pat, href in self._links.items():
                if pat in sel:
                    return _El(href, page=self, name=pat)
            return None

        def wait_for_timeout(self, ms):
            # fire one queued response only once a catcher handler is listening
            # (the settle poll must not consume the viewer-stream emit)
            if self._emit and self.ctx is not None and self.ctx.handlers:
                r, self._emit = self._emit[0], self._emit[1:]
                self.ctx.emit(r)

    class _Ctx:
        """Minimal ctx stub: request API + a response-event bus with handlers the
        pdf-stream catcher registers on (v0.4.3)."""

        def __init__(self, api_resp=None):
            self.handlers, self.page_handlers, self.api_calls = [], [], []

            def _get(url, **k):
                self.api_calls.append(url)
                return api_resp if api_resp is not None else _Resp(403, b"<html/>")
            self.request = SimpleNamespace(get=_get)

        def on(self, ev, h):
            if ev == "response":
                self.handlers.append(h)
            elif ev == "page":
                self.page_handlers.append(h)

        def remove_listener(self, ev, h):
            for hs in (self.handlers, self.page_handlers):
                if h in hs:
                    hs.remove(h)

        def emit_page(self, page):
            for h in list(self.page_handlers):
                h(page)

        def emit(self, resp):
            for h in list(self.handlers):
                h(resp)

    def bare(page, api_resp=None):
        b = object.__new__(Browser)
        b.page = page
        b.ctx = _Ctx(api_resp)
        b._drain_s = 0          # offline: drains check once and return immediately
        b._click_drain_s = 0
        page.ctx = b.ctx
        return b

    with tempfile.TemporaryDirectory() as td:
        dest = str(Path(td) / "x.pdf")

        # emerald no-access landing, zero affordance -> no_access, zero strikes
        pg = _Page(content="<p>You do not currently have access to this content</p>",
                   title="Access denied | Emerald")
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert (res, detail) == ("no_access", "landing says no access"), (res, detail)

        # no-access copy WITHOUT a visible entitlement control beats the meta tag
        # (Emerald pages carry citation_pdf_url even when locked)
        pg = _Page(content="<p>You do not currently have access to this content</p>",
                   title="t", meta="https://www.emerald.com/insight/content/doi/10.1108/x/full/pdf")
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert res == "no_access" and "no entitlement control" in detail, (res, detail)

        # a locked page's 'Article PDF' button is NOT an entitlement control: no
        # greedy text-matching on 'PDF' (it would match Emerald's
        # locked button and sage's /doi/reader/ link, skipping the no-access verdict)
        pg = _Page(content="<p>You do not currently have access to this content</p>",
                   title="t",
                   links={"Article PDF": "https://www.emerald.com/x/article-pdf/1/1/1/x.pdf"},
                   meta="https://www.emerald.com/x/article-pdf/1/1/1/x.pdf")
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert res == "no_access", (res, detail)

        # ...but a visible View PDF control IS the entitlement signal: proceed to ok
        pg = _Page(content="<p>You do not currently have access</p>", title="t",
                   pdf_resp=_Resp(200, b"%PDF-1.4 stub"),
                   links={"View PDF": "https://www.emerald.com/x/pdf"},
                   meta="https://www.emerald.com/meta.pdf")
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert res == "ok", (res, detail)

        # SD: the ?md5= link yields an empty body (viewer era); the plain ?download=true
        # form of the same PII is the last resort and captures it (v0.5.1 form)
        pii = "S0000000021000001"
        pg = _Page(title="t",
                   url=f"https://www.sciencedirect.com/science/article/pii/{pii}",
                   links={"View PDF": f"/science/article/pii/{pii}/pdfft?md5=c7x&pid=1.pdf"},
                   by_url={"md5=": _Resp(200, b"", headers={"content-type": "text/html"}),
                           "download=true": _Resp(200, b"%PDF-1.6 dl")})
        res, detail, url = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and url.endswith("/pdfft?download=true"), (res, detail, url)

        # fetch_url is navigation-first: the page identity loads what the API-context
        # GET can be refused on (SAGE); nav refusal -> blocked
        pg = _Page(title="t", pdf_resp=_Resp(200, b"%PDF-1.4 oa"))
        assert bare(pg).fetch_url("https://journals.sagepub.com/doi/pdf/10.1177/x", dest) \
            == ("ok", "inline")
        pg = _Page(title="t", pdf_resp=_Resp(403))
        assert bare(pg).fetch_url("https://journals.sagepub.com/doi/pdf/10.1177/x", dest) \
            == ("blocked", "HTTP 403 (page navigation)")

        # a 200 HTML without verdict evidence falls through to fail WITH diagnostics
        pg = _Page(title="t", pdf_resp=_Resp(200, b"<html><body>article preview relay</body></html>",
                                             headers={"content-type": "text/html"}),
                   meta="https://www.sciencedirect.com/science/article/pii/S0000000018000002/pdfft?md5=z")
        res, detail, _ = bare(pg).fetch("10.1016/x", dest)
        assert res == "fail" and "text/html" in detail and "preview relay" in detail, (res, detail)

        # T-B1 (v0.4.3): a navigation 403 NEXT TO a visible entitlement control is
        # challenge/noise class - fail, retryable - never a per-paper verdict
        # (entitlement-signal discipline)
        pg = _Page(title="t", pdf_resp=_Resp(403),
                   links={"View PDF": "https://www.sciencedirect.com/x/pdfft"})
        res, detail, _ = bare(pg).fetch("10.1016/x", dest)
        assert res == "fail" and "control visible" in detail, (res, detail)

        # T-B2 (v0.4.3): a hard 403 on the pdf endpoint with ZERO entitlement
        # control on the landing is a per-paper no_access verdict (e.g. Emerald, when
        # the settle-poll window misses a client-side access note)
        pg = _Page(title="t",
                   meta="https://www.emerald.com/example/article-pdf/1/1/1/1/x.pdf",
                   pdf_resp=_Resp(403))
        res, detail, _ = bare(pg).fetch("10.1108/EXAMPLE-04-2016-0001", dest)
        assert res == "no_access" and "pdf endpoint" in detail, (res, detail)

        # navigation yields nothing but the API-context request returns the PDF -> ok
        pg = _Page(title="t", pdf_resp=None, raise_nav=True,
                   links={"View PDF": "https://journals.sagepub.com/doi/pdf/10.1177/x"})
        res, detail, _ = bare(pg, api_resp=_Resp(200, b"%PDF-1.4 api")).fetch("10.1177/x", dest)
        assert res == "ok" and detail == "api-context", (res, detail)

        # a control click that fires a download event captures the PDF directly
        # (ScienceDirect can serve empty relays to goto on both variants)
        pg = _Page(title="t", dl_body=b"%PDF-1.4 click",
                   links={"View PDF": "https://www.sciencedirect.com/x/pdfft?md5=z"})
        res, detail, _ = bare(pg).fetch("10.1016/x", dest)
        assert res == "ok" and "control click" in detail, (res, detail)

        # T-A (v0.4.3/0.4.4): the click opens an inline viewer - no download event
        # fires, but the viewer's own pdf stream response is caught at ctx level
        # and drained after the click (ScienceDirect case: a chrome-extension
        # proxy resource, read via body() behind a diagnostic print)
        pg = _Page(title="t",
                   links={"View PDF": "https://www.sciencedirect.com/x/pdfft?md5=z"},
                   emit=[_Resp(200, b"%PDF-1.7 stream",
                               headers={"content-type": "application/pdf"},
                               url="chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/97c8ce50-x")])
        b = bare(pg)
        b._drain_s = b._click_drain_s = 2
        res, detail, _ = b.fetch("10.1016/x", dest)
        assert res == "ok" and "viewer stream (control click)" in detail, (res, detail)

        # T-A4 (v0.4.4): an http(s) captured stream is NEVER read from the live
        # response (body() has no timeout and can hang) - it is re-fetched bounded
        # through the context request API (shared cookies)
        pg = _Page(title="t",
                   links={"View PDF": "https://www.emerald.com/x/article-pdf/1/1/1/x.pdf"},
                   emit=[_Resp(200, b"%PDF-1.4 ignored-live-body",
                               headers={"content-type": "application/pdf"},
                               url="https://www.emerald.com/x/article-pdf/1/1/1/x.pdf")])
        b = bare(pg, api_resp=_Resp(200, b"%PDF-1.5 refetched",
                                    url="https://www.emerald.com/x/article-pdf/1/1/1/x.pdf"))
        b._drain_s = b._click_drain_s = 2
        res, detail, _ = b.fetch("10.1108/x", dest)
        assert res == "ok" and "viewer stream (control click)" in detail, (res, detail)
        assert Path(dest).read_bytes() == b"%PDF-1.5 refetched", "re-fetch body not used"

        # T-A2 (v0.4.3): no control (meta only); goto gets SD's empty relay and the
        # stream the viewer then fetches is drained after the nav chain fails
        # (v0.4.4: the http ref is re-fetched bounded via the request API)
        pg = _Page(title="t",
                   meta="https://www.sciencedirect.com/science/article/pii/"
                        "S0000000021000001/pdfft?md5=c7x",
                   by_url={"pdfft": _Resp(200, b"", headers={"content-type": "text/html"})},
                   emit=[_Resp(200, b"%PDF-1.6 vs",
                               headers={"content-type": "application/pdf"},
                               url="https://www.sciencedirect.com/science/article/pii/"
                                   "S0000000021000001/pdfft?md5=c7x")])
        b = bare(pg, api_resp=_Resp(200, b"%PDF-1.6 vs"))
        b._drain_s = 2
        res, detail, url = b.fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "viewer stream (goto)" in detail, (res, detail, url)

        # T-A3 (v0.4.3): a Content-Range (206-style partial) or non-200 pdf response
        # must NOT be captured - only complete 200 streams (torn-file guard)
        pg = _Page(title="t",
                   links={"View PDF": "https://www.sciencedirect.com/x/pdfft?md5=z"},
                   emit=[_Resp(200, b"%PDF-1.7 part",
                               headers={"content-type": "application/pdf",
                                        "content-range": "bytes 0-1023/819253"}),
                         _Resp(403, b"%PDF-1.7 x", headers={"content-type": "application/pdf"})])
        b = bare(pg)
        b._drain_s = 2
        dest3 = str(Path(td) / "x_ta3.pdf")
        res, detail, _ = b.fetch("10.1016/x", dest3)
        assert res == "fail", (res, detail)
        assert b._caught == [] or all("part" not in str(getattr(r, "url", "")) for r in b._caught), b._caught
        assert not Path(dest3).exists(), "partial captured"

        # sage: the citation meta may name the reader page (PDF inside an embedded
        # viewer); navigate the rewritten /doi/pdf/ endpoint instead
        pg = _Page(title="t",
                   url="https://journals.sagepub.com/doi/10.1177/00000000231000001",
                   meta="https://journals.sagepub.com/doi/reader/10.1177/00000000231000001",
                   by_url={"/doi/pdf/": _Resp(200, b"%PDF-1.5 sage")})
        res, detail, url = bare(pg).fetch("10.1177/00000000231000001", dest)
        assert res == "ok" and "/doi/pdf/" in url, (res, detail, url)

        # fetch_url: a no-access HTML body must not read as a challenge (no strike)
        b = bare(_Page())
        assert b.ctx.request.get("u") .status == 403  # harness sanity: api 403 default
        r1 = b.fetch_url("https://www.emerald.com/insight/content/doi/10.1108/x/full/pdf", dest)
        assert r1 == ("blocked", "HTTP 403"), r1
        b2 = bare(_Page(), api_resp=_Resp(200, b"<html><body>You do not currently have "
                                                b"access to this content</body></html>"))
        r2 = b2.fetch_url("https://www.emerald.com/x", dest)
        assert r2 == ("fail", "no access page (not a challenge)"), r2
        b3 = bare(_Page(), api_resp=_Resp(200, b"<html><body>please verify you are human"
                                                b" captcha</body></html>"))
        r3 = b3.fetch_url("https://www.repo/x.pdf", dest)
        assert r3 == ("blocked", "challenge page"), r3

        # T-U1 (v0.4.6): the fb path renders the pdf in Chromium's inline viewer -
        # nav fails, the viewer's own stream is caught and drained BEFORE the
        # api-context request (which can fail with a bare "request error")
        pg = _Page(title="t", pdf_resp=_Resp(200, b"<html><body>relay shell</body></html>",
                                            headers={"content-type": "text/html"}),
                   emit=[_Resp(200, b"%PDF-1.6 fbstream",
                               headers={"content-type": "application/pdf"},
                               url="chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/u1")])
        bu = bare(pg)
        bu._drain_s = 2
        res, detail = bu.fetch_url("https://pmc.ncbi.nlm.nih.gov/articles/x/pdf/main.pdf", dest)
        assert res == "ok" and "viewer stream (url fetch)" in detail, (res, detail)

        # T-U2 (v0.4.6): request-path failures carry the real exception class AND the
        # preserved nav outcome (no more bare "request error"); drain budget 0 here
        pg = _Page(title="t")            # nav yields no response
        bu = bare(pg)

        def _boom(url, **k):
            raise RuntimeError("x-tls")

        bu.ctx.request = SimpleNamespace(get=_boom)
        res, detail = bu.fetch_url("https://pmc.ncbi.nlm.nih.gov/articles/x/pdf/main.pdf", dest)
        assert res == "fail" and "RuntimeError" in detail and "no response" in detail, (res, detail)

        # the visible View PDF link wins over the
        # citation meta (SD serves the ?isDTMRedir= variant to clicks, not ?md5=)
        pg = _Page(title="t", pdf_resp=_Resp(200, b"%PDF-1.5 stub"),
                   url="https://www.sciencedirect.com/science/article/pii/S0000000021000001",
                   links={"View PDF": "/science/article/pii/S0000000021000001/pdfft"
                                      "?isDTMRedir=true&download=true"},
                   meta="https://www.sciencedirect.com/science/article/pii/S0000000021000001/pdfft?md5=c7x")
        res, detail, url = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "isDTMRedir" in url, (res, detail, url)

        # emerald's article-pdf endpoint serves a 200 HTML shell when not entitled:
        # that body is per-paper no_access evidence (never a strike, never a fuse)
        pg = _Page(title="t",
                   pdf_resp=_Resp(200, b"<html><body>You do not currently have access "
                                       b"to this content</body></html>",
                                  headers={"content-type": "text/html"}),
                   meta="https://www.emerald.com/example/article-pdf/1/1/1/1/x.pdf")
        res, detail, _ = bare(pg).fetch("10.1108/EXAMPLE-04-2016-0001", dest)
        assert res == "no_access" and "pdf endpoint" in detail, (res, detail)

    # script-channel fuse namespace: publisher-host script challenges must fuse
    # '<pub>_direct', never the browser's bare publisher cap
    from .engine import Acquirer as _Acq
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        cfg = {"limits": {}, "browser_enabled": False, "contact_email": "", "privacy_markers": []}
        args = SimpleNamespace(no_browser=True)
        acq = _Acq(proj, cfg, args, steps=["a"])
        assert acq._script_cap("sage") == "sage_direct"
        assert acq._script_cap("elsevier") == "elsevier_direct"
        assert acq._script_cap("elsevier_api") == "elsevier_api"
        assert acq._script_cap("sage_direct") == "sage_direct"
        assert acq._script_cap("orca.cardiff.ac.uk") == "orca.cardiff.ac.uk"

        # wiring: try_pdf hands download the script-namespace cap
        seen = {}

        def _cap_download(url, cap, **kw):
            seen["cap"] = cap
            return ("fail", None, "stub")
        acq.download = _cap_download
        rec_x = {"uid": "T12", "DOI": "10.1177/x", "Title": "t"}
        acq.try_pdf("T12", rec_x, "oa_content", "oa", "https://journals.sagepub.com/doi/pdf/x",
                    "sage", "published")
        assert seen["cap"] == "sage_direct", seen

        # mechanics: a script-channel challenge fuses only the _direct namespace
        acq2 = _Acq(proj, cfg, args, steps=["a"])

        class _ChalResp:
            status = 200
            status_code = 200
            def iter_content(self, n):
                return [b"<html><body>please verify you are human captcha</body></html>"]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _ChalSess:
            def __init__(self):
                self.headers = {}
            def get(self, url, **kw):
                return _ChalResp()
            def head(self, url, **kw):
                raise RuntimeError("offline probe unused here")

        acq2.s = _ChalSess()
        res, http, x = acq2.download("https://journals.sagepub.com/doi/pdf/10.1177/x", "sage_direct")
        assert res == "defer" and x == "challenge page", (res, x)
        assert "sage_direct" in acq2.day.d["blocked"], acq2.day.d["blocked"]
        assert "sage" not in acq2.day.d["blocked"], "script challenge fused the browser cap"

        # v0.4.5 transport-fail fallback (Python's
        # TLS chokes on hosts Edge fetches fine, e.g. PMC): a RequestException fail
        # (http is None) rides the browser identity; HTTP-status fails do not; keyed
        # URLs and header-keyed API channels (api_semantics) never do. Distinct uids:
        # the Acquirer persists status in the shared temp project.
        rec_ssl = {"uid": "S", "DOI": "10.1155/x", "Title": "t"}

        def _ssl_acq():
            return _Acq(proj, cfg, args, steps=["a"])

        a1 = _ssl_acq()
        a1.download = lambda url, cap, **kw: ("fail", None, "SSLError")
        u1 = "https://pmc.ncbi.nlm.nih.gov/articles/PMC10560016/pdf/main.pdf"
        assert a1.try_pdf("S1", rec_ssl, "oa_content", "oa", u1,
                          "pmc.ncbi.nlm.nih.gov", "published") == "fail"
        assert (a1.status.get("S1", {}).get("browser_fallback") or []) == [u1], a1.status.get("S1")

        a2 = _ssl_acq()
        a2.download = lambda url, cap, **kw: ("fail", None, "SSLError")
        u2 = "https://api.example.org/content/x?token=SECRET"
        a2.try_pdf("S2", rec_ssl, "oa_content", "oa", u2, "api.example.org", "published")
        assert not (a2.status.get("S2", {}).get("browser_fallback") or []), "keyed URL entered fallback"

        a3 = _ssl_acq()
        a3.download = lambda url, cap, **kw: ("fail", 200, "HTTP 200, not a PDF")
        a3.try_pdf("S3", rec_ssl, "oa_content", "oa", "https://host/x.pdf", "host", "published")
        assert not (a3.status.get("S3", {}).get("browser_fallback") or []), "HTTP-status fail entered fallback"

        a4 = _ssl_acq()
        a4.download = lambda url, cap, **kw: ("defer", 200, "challenge page")
        u4 = "https://repo.example.org/x.pdf"
        a4.try_pdf("S4", rec_ssl, "oa_content", "oa", u4, "repo.example.org", "published")
        assert (a4.status.get("S4", {}).get("browser_fallback") or []) == [u4], "defer-challenge append regressed"

        a5 = _ssl_acq()
        a5.download = lambda url, cap, **kw: ("fail", None, "SSLError")
        a5.try_pdf("S5", rec_ssl, "publisher", "elsevier_api",
                   "https://api.elsevier.com/content/article/doi/x", "elsevier_api",
                   "published", api_semantics=True)
        assert not (a5.status.get("S5", {}).get("browser_fallback") or []), \
            "header-keyed API channel entered fallback"

        # v0.4.7 abandoned sources (e.g. a repository host that does not open from the
        # operator's network even in a real browser).
        # Pipeline coverage mirrors every skip_dead consumer: download, the four
        # candidate-step branches, step_publisher's direct return (via DEFER_RESULTS),
        # the browser_fallback loop, and any project adapter (imported constant).
        from . import engine as _eng
        assert "skip_dead" in _eng.DEFER_RESULTS and "skip_dead" in _eng.SKIP_RESULTS

        def _dead_acq():
            a = _ssl_acq()
            a.cfg["abandoned_sources"] = ["pmc.ncbi.nlm.nih.gov"]
            return a

        # T-D1: download skip_deads with zero bookkeeping (real download, no stub)
        a = _dead_acq()
        res = a.download("https://pmc.ncbi.nlm.nih.gov/articles/x/pdf/main.pdf",
                         "pmc.ncbi.nlm.nih.gov")
        assert res == ("skip_dead", None, "abandoned source"), res
        assert not a.day.d.get("count", {}).get("pmc.ncbi.nlm.nih.gov"), a.day.d["count"]

        # T-D2: browser_fallback loop skips the dead URL - retained, never attempted.
        # Dedicated cfg with browser_enabled True + no_browser False:
        # the shared harness cfg would make step_browser defer at the first guard and
        # the fb loop would never run - vacuous green. With this cfg, a missing dead
        # check would really sleep 25-40s in _browser_gap and crash on the stub
        # browser - a true pin.
        a = _Acq(proj, {**cfg, "browser_enabled": True,
                        "abandoned_sources": ["pmc.ncbi.nlm.nih.gov"]},
                 SimpleNamespace(no_browser=False), steps=["a"])
        a.browser = object()                      # skip real browser init
        a.day.block("elsevier", "test")           # part (b) defers without a gap sleep
        u_dead = "https://pmc.ncbi.nlm.nih.gov/articles/x/pdf/main.pdf"
        a.status["S-D2"] = {"browser_fallback": [u_dead]}
        rec_el = {"uid": "S-D2", "DOI": "10.1016/x", "Title": "t"}
        assert a.step_browser("S-D2", rec_el) == "defer"
        assert (a.status["S-D2"].get("browser_fallback") or []) == [u_dead], "dead URL consumed"
        assert not a.day.d.get("browser_total"), a.day.d.get("browser_total")

        # T-D3: a non-abandoned host passes the check untouched (routed as usual)
        a = _dead_acq()
        a.download = lambda url, cap, **kw: ("fail", None, "SSLError")
        a.try_pdf("S-D3", rec_ssl, "oa_content", "oa", "https://hdl.handle.net/x.pdf",
                  "hdl.handle.net", "published")
        assert (a.status.get("S-D3", {}).get("browser_fallback") or []) == ["https://hdl.handle.net/x.pdf"]

        # T-D4: an all-dead candidate list defers with zero budget burn and zero calls
        # (real download: the dead check must fire BEFORE the session; the session
        # sentinel fails the test if any network call is attempted)
        a = _dead_acq()

        class _SentinelSession:
            def get(self, url, **kw):
                raise AssertionError(f"network call for an abandoned source: {url}")

        a.s = _SentinelSession()
        a.candidates["S-D4"] = [{"url": "https://pmc.ncbi.nlm.nih.gov/y.pdf", "version": "published"}]
        rec_d4 = {"uid": "S-D4", "DOI": "10.1155/x", "Title": "t"}
        assert a.step_candidates("S-D4", rec_d4) == "defer"
        assert not (a.status.get("S-D4", {}).get("step_state") or {}), "budget burned"

        # T-D5: step_publisher's direct return path - skip_dead flows through and
        # DEFER_RESULTS makes process() treat it as defer (zero burn)
        a = _ssl_acq()
        a.download = lambda url, cap, **kw: ("skip_dead", None, "abandoned source")
        rec_sp = {"uid": "S-D5", "DOI": "10.1007/x", "Title": "t"}
        assert a.step_publisher("S-D5", rec_sp) == "skip_dead"

        # T-D6: a publisher-TOKEN entry matches the URL's own cap_key
        # in the script channel too - "springer" + link.springer.com (cap_key ->
        # "springer"; the passed cap is the springer_direct namespace and would miss)
        a = _dead_acq()
        a.cfg["abandoned_sources"] = ["springer"]
        res = a.download("https://link.springer.com/content/pdf/10.1007/x.pdf", "springer_direct")
        assert res == ("skip_dead", None, "abandoned source"), res
        assert not a.day.d.get("count", {}).get("springer_direct"), a.day.d["count"]

        # v0.4.8 reload-and-repoll (ScienceDirect renders the View PDF control
        # per LOAD - one load can miss it for half a minute while a fresh load shows
        # it within seconds): after the first empty-relay nav the ARTICLE page is reloaded
        # once and the control re-polled; otherwise the existing chain runs.
        art_url = "https://www.sciencedirect.com/science/article/pii/S0000000021000001"
        pii_url = art_url + "/pdfft?download=true"

        class _ReloadPage(_Page):
            def __init__(self, control_on_reload=True, **kw):
                super().__init__(**kw)
                self.reloads = 0
                self.control_on_reload = control_on_reload

            def goto(self, url, **kw):
                if "/science/article/pii/" in url and "pdfft" not in url:
                    self.reloads += 1
                    self.url = url
                    if self.control_on_reload:
                        self._links["View PDF"] = "/science/article/pii/S0000000021000001/pdfft?md5=x&pid=y.pdf"
                return super().goto(url, **kw)

        # T-L1/L2/L3 use their own dest: this block lives in the engine-test
        # tempdir scope; the browser-suite `dest` name still points into the
        # already-exited first TemporaryDirectory (a stale-dest bug).
        dest_l = str(Path(td) / "x_reload.pdf")
        # T-L1: first load = pii-only (control lost the race) -> nav empty relay ->
        # reload -> control -> click -> ok; the reload target is the ARTICLE page
        pg = _ReloadPage(url=art_url,
                         by_url={"pdfft": _Resp(200, b"", headers={"content-type": "text/html"})},
                         dl_body=b"%PDF-1.8 reload")
        res, detail, rurl = bare(pg).fetch("10.1016/j.example.2021.100001", dest_l)
        assert res == "ok" and "after reload" in detail, (res, detail)
        assert pg.reloads == 1, pg.reloads
        assert rurl == art_url and "pdfft" not in rurl, rurl
        # v0.5.0: the reload comes BEFORE any pdf navigation (no dead-end empty relay)
        assert not any("pdfft" in u for u in pg.gotos), pg.gotos

        # T-L2: both loads lose the race -> honest fail, chain preserved
        pg = _ReloadPage(control_on_reload=False, url=art_url,
                         by_url={"pdfft": _Resp(200, b"", headers={"content-type": "text/html"})})
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest_l)
        assert res == "fail" and "empty body" in detail, (res, detail)
        assert pg.reloads == 1, pg.reloads

        # T-L3: the control appears on reload but the click fails ->
        # honest fall-through to the existing chain, no false ok
        pg = _ReloadPage(url=art_url,
                         by_url={"pdfft": _Resp(200, b"", headers={"content-type": "text/html"})})
        b = bare(pg)
        b._drain_s = 0
        res, detail, _ = b.fetch("10.1016/j.example.2021.100001", dest_l)
        assert res == "fail" and "after reload" not in detail, (res, detail)
        assert pg.reloads == 1, pg.reloads
    # v0.5.0: PDFs download instead of opening the viewer; downloads are caught in every
    # tab; nothing re-navigates before the catcher is drained
    with tempfile.TemporaryDirectory() as td:
        dest = str(Path(td) / "n.pdf")
        sd_art = "https://www.sciencedirect.com/science/article/pii/S0000000021000001"
        sd_ctl = "/science/article/pii/S0000000021000001/pdfft?md5=x&pid=1-s2.0-S0000000021000001-main.pdf"

        # T-N1: page.goto returns on linkinghub's "Redirecting" hop; the landing is
        # judged only after the redirect, so the institution activation click lands on
        # the REAL page and the View PDF control it reveals is clicked (the old flow
        # clicked activation on the redirector, found nothing, and never retried)
        class _HopPage(_Page):
            def __init__(self, hops=2, **kw):
                super().__init__(**kw)
                self.url = "https://linkinghub.elsevier.com/retrieve/pii/S0000000021000001"
                self._t, self._hops, self.waits = "Redirecting", hops, 0

            def wait_for_timeout(self, ms):
                self.waits += 1
                if self.url.startswith("https://linkinghub") and self.waits >= self._hops:
                    self.url, self._t = sd_art, "Article - ScienceDirect"
                    self._links["Access through"] = "/user/institution/login"
                super().wait_for_timeout(ms)

        def _activate(page):
            page._links.pop("Access through", None)
            page._links["View PDF"] = sd_ctl
        pg = _HopPage(dl_body=b"%PDF-1.7 n1", click_hooks={"Access through": _activate})
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "download (control click)" in detail, (res, detail)
        assert pg.clicks == ["Access through", "View PDF"], pg.clicks
        # ...on the FIRST judgement: no reload was needed to recover from the hop
        assert "after reload" not in detail and pg.gotos == ["https://doi.org/10.1016/j.example.2021.100001"], \
            (detail, pg.gotos)

        # T-N2: SD's View PDF opens a NEW tab; with the viewer off the PDF arrives as a
        # download there. It is caught through the ctx 'page' event (the old wait
        # listened on the landing tab only), and the tab is closed after the attempt
        popups = []

        def _open_popup(page):
            pop = _Page(url="about:blank")
            popups.append(pop)
            page.ctx.emit_page(pop)
            pop.fire_download(b"%PDF-1.7 popup", "https://pdf.sciencedirectassets.com/x/main.pdf")
        pg = _Page(title="t", url=sd_art, links={"View PDF": sd_ctl}, click_hooks={"View PDF": _open_popup})
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "download (control click)" in detail and "sciencedirectassets" in detail, \
            (res, detail)
        assert Path(dest).read_bytes() == b"%PDF-1.7 popup"
        assert popups and popups[0].closed and not pg.closed, "popup left open or working tab closed"

        # T-N3: a pdf navigation that turns into a download (viewer off) is caught by the
        # drain, and the tab is NOT navigated a second time (the old immediate second
        # navigation destroyed a PDF that arrived after a JS relay)
        emerald_pdf = "https://www.emerald.com/insight/content/doi/10.1108/x/full/pdf"
        pg = _Page(title="t", meta=emerald_pdf, nav_download={"/full/pdf": b"%PDF-1.5 navdl"})
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert res == "ok" and "download (goto)" in detail, (res, detail)
        assert sum("/full/pdf" in u for u in pg.gotos) == 1, pg.gotos

        # T-N3b: the PDF lands only after the JS relay settles, and any second
        # navigation of the tab cancels it - the drain must come first (the old flow
        # re-navigated at once and lost exactly these PDFs: 72 empty relays on record)
        class _LatePage(_Page):
            pending = None

            def goto(self, url, **kw):
                self.pending = None                      # a navigation cancels a pending pdf
                if "pdfft" in url:
                    self.gotos.append(url)
                    self.pending = b"%PDF-1.6 late"
                    return _Resp(200, b"", headers={"content-type": "text/html"})
                return super().goto(url, **kw)

            def expect_download(self, timeout=None):
                raise TimeoutError("no download event")  # the re-navigation cancelled it

            def wait_for_timeout(self, ms):
                if self.pending:
                    body, self.pending = self.pending, None
                    self.fire_download(body, "https://pdf.sciencedirectassets.com/late.pdf")
                super().wait_for_timeout(ms)
        pg = _LatePage(title="t", meta="https://www.sciencedirect.com/science/article/pii/"
                                        "S0000000021000001/pdfft?md5=c7x")
        b = bare(pg)
        b._drain_s = 2
        res, detail, _ = b.fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "download (goto)" in detail, (res, detail)
        assert len(pg.gotos) == 2 and "pdfft" in pg.gotos[1], pg.gotos   # doi.org + ONE pdf navigation

        # T-N4: a driver without download events keeps the pre-0.5.0 path: the
        # navigation is repeated inside a download wait on the landing tab
        class _LegacyPage(_Page):
            on = None   # page.on() unavailable: no download listeners can be armed
        pg = _LegacyPage(title="t", meta=emerald_pdf, dl_body=b"%PDF-1.4 legacy")
        res, detail, _ = bare(pg).fetch("10.1108/x", dest)
        assert (res, detail) == ("ok", "download"), (res, detail)
        assert sum("/full/pdf" in u for u in pg.gotos) == 2, pg.gotos

        # T-N5: each caught stream is processed once per attempt (the drain used to
        # re-fetch every caught http stream once a second for the whole window)
        art_pdf = "https://www.emerald.com/x/article-pdf/1/1/1/x.pdf"
        pg = _Page(title="t", meta=art_pdf,
                   emit=[_Resp(200, b"%PDF-1.4 live", headers={"content-type": "application/pdf"},
                               url=art_pdf)])
        b = bare(pg)                 # the request API answers 403: the re-fetch fails
        b._drain_s = 2
        res, detail, _ = b.fetch("10.1108/x", dest)
        assert res == "fail", (res, detail)
        assert b.ctx.api_calls.count(art_pdf) == 2, b.ctx.api_calls   # one re-fetch + the final request

        # T-N6: SD's WAF refuses API-context requests (v0.4.1), so the SD failure path
        # sends none; T-N9: the failure detail carries compact diagnostics up front
        pg = _Page(title="t", url=sd_art, links={"View PDF": sd_ctl},
                   by_url={"pdfft": _Resp(200, b"", headers={"content-type": "text/html"})})
        b = bare(pg)
        res, detail, _ = b.fetch("10.1016/j.example.2021.100001", dest)
        assert res == "fail" and not b.ctx.api_calls, (res, b.ctx.api_calls)
        assert detail.startswith("no pdf via navigation or request [ctl=yes dl=0 st=0]"), detail

        # T-N7: the profile pref that turns the inline viewer off
        prof = Path(td) / "profile"
        pfile = prof / "Default" / "Preferences"
        assert bc.ensure_pdf_download_pref(prof, True) == "set"
        assert json.loads(pfile.read_text(encoding="utf-8"))["plugins"]["always_open_pdf_externally"] is True
        assert bc.ensure_pdf_download_pref(prof, True) == "already set"
        pfile.write_text(json.dumps({"profile": {"name": "x"}, "plugins": {"a": 1}}), encoding="utf-8")
        assert bc.ensure_pdf_download_pref(prof, True) == "set"
        merged = json.loads(pfile.read_text(encoding="utf-8"))
        assert merged == {"profile": {"name": "x"}, "plugins": {"a": 1, "always_open_pdf_externally": True}}
        assert bc.ensure_pdf_download_pref(prof, False) == "set"      # viewer wanted again
        assert json.loads(pfile.read_text(encoding="utf-8"))["plugins"]["always_open_pdf_externally"] is False
        prof2 = Path(td) / "profile2"
        assert bc.ensure_pdf_download_pref(prof2, False) == "already set" and not prof2.exists()
        pfile.write_text("{broken", encoding="utf-8")
        assert "untouched" in bc.ensure_pdf_download_pref(prof, True)
        assert pfile.read_text(encoding="utf-8") == "{broken", "an unreadable Preferences file was rewritten"

        # T-N8: the working tab is brought to front for every fetch (the login gate
        # opens its tabs on top of it)
        pg = _Page(title="t", pdf_resp=_Resp(200, b"%PDF-1.4 x"))
        bare(pg).fetch_url("https://repo.example.org/x.pdf", dest)
        assert pg.fronts == 1, pg.fronts
        pg = _Page(title="t", links={"View PDF": "https://www.emerald.com/x/pdf"},
                   pdf_resp=_Resp(200, b"%PDF-1.4 x"))
        bare(pg).fetch("10.1108/x", dest)
        assert pg.fronts == 1, pg.fronts

        # T-N10: the reload is judged like a landing: no-access copy on the reloaded SD
        # page is the per-paper verdict, reached without any pdf navigation
        class _NoAccessOnReload(_Page):
            def goto(self, url, **kw):
                if url == sd_art:
                    self._c = "<p>Purchase this article</p>"
                return super().goto(url, **kw)
        pg = _NoAccessOnReload(title="t", url=sd_art + "?via%3Dihub")
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "no_access", (res, detail)
        assert not any("pdfft" in u for u in pg.gotos), pg.gotos

        # T-N11 (v0.5.1): every attempt leaves a trace of event classes and timings -
        # never a URL, a title or a host outside the fixed publisher classes
        pg = _Page(title="Secret Article Title - ScienceDirect", url=sd_art, links={"View PDF": sd_ctl},
                   dl_body=b"%PDF-1.7 tr")
        b = bare(pg)
        res, _, _ = b.fetch("10.1016/j.example.2021.100001", dest)
        evs = [e["ev"] for e in b.last_trace]
        assert res == "ok" and evs[0] == "start" and evs[-1] == "end", evs
        assert {"landing", "settled", "affordance", "click", "download"} <= set(evs), evs
        blob = json.dumps(b.last_trace)
        assert "://" not in blob and "Secret" not in blob and "pii" not in blob.lower(), blob
        assert bc.host_class("https://idp.example-university.edu/sso") == "other"
        assert bc.host_class("https://pdf.sciencedirectassets.com/x.pdf?X-Amz=1") == "sd-assets"
        assert bc.host_class("chrome-extension://mhjf/x") == "viewer"
        b.fetch_url("https://repo.example.org/x.pdf", dest)
        assert b.last_trace[0] == {"t": b.last_trace[0]["t"], "ev": "start", "kind": "url"}

        # T-N12 (v0.5.1): no control rendered, but the page JSON names the exact View
        # PDF link (Zotero ScienceDirect.js step 5): it is opened first, with no reload
        sd_json = json.dumps({"article": {"pdfDownload": {"urlMetadata": {
            "path": "science/article/pii", "pdfExtension": "/pdfft", "pii": "S0000000021000001",
            "queryParams": {"md5": "c7x", "pid": "1-s2.0-S0000000021000001-main.pdf"}}}}})
        assert bc.sd_json_pdf_url(sd_art, sd_json) == \
            sd_art + "/pdfft?md5=c7x&pid=1-s2.0-S0000000021000001-main.pdf"
        assert bc.sd_json_pdf_url(sd_art, "{}") is None and bc.sd_json_pdf_url(sd_art, "not json") is None
        pg = _Page(title="t", url=sd_art + "?via%3Dihub", json_state=sd_json,
                   nav_download={"md5=c7x": b"%PDF-1.7 json"})
        res, detail, url = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "ok" and "(page json)" in detail and "md5=c7x" in url, (res, detail, url)
        assert pg.gotos == ["https://doi.org/10.1016/j.example.2021.100001", url], pg.gotos

        # T-N13: a block page at the landing strikes at once (it never clears)
        pg = _Page(title="ScienceDirect", url=sd_art,
                   content="<html>" + "word " * 2000 + "There was a problem providing the content "
                           "you requested</html>")
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert (res, detail) == ("blocked", "landing is a block page"), (res, detail)

        # T-N14: a 403 block page from the pdf endpoint is challenge class, never a
        # per-paper no_access verdict (which would exhaust the paper's browser step)
        pg = _Page(title="t", url=sd_art + "?via%3Dihub",
                   by_url={"pdfft": _Resp(403, b"<html>There was a problem providing the content you "
                                               b"requested</html>", headers={"content-type": "text/html"})})
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert res == "blocked" and "block page" in detail, (res, detail)

        # T-N15 (v0.5.2): a title read that fails while a hop commits is not a settled
        # landing; the block page that loads next is judged, not the redirector page
        class _HopPage(_Page):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._script = ["hop", "raise", "land"]

            def title(self):
                step = self._script.pop(0) if self._script else "land"
                if step == "raise":
                    raise RuntimeError("Execution context was destroyed")
                if step == "land":
                    self.url, self._t = sd_art, "ScienceDirect"
                    self._c = "<html>There was a problem providing the content you requested</html>"
                return self._t
        pg = _HopPage(title="Redirecting", url="https://doi.org/10.1016/j.example.2021.100001",
                      content="<p>Redirecting...</p>")
        res, detail, _ = bare(pg).fetch("10.1016/j.example.2021.100001", dest)
        assert (res, detail) == ("blocked", "landing is a block page"), (res, detail)

        # T-N16 (v0.5.2): content() refusing while the page navigates is looked at
        # again, not read as "no block page"
        class _NavPage(_Page):
            refuse = 1

            def content(self):
                if self.refuse:
                    self.refuse -= 1
                    raise RuntimeError("Unable to retrieve content because the page is navigating")
                return self._c
        b = bare(_NavPage(title="ScienceDirect", url=sd_art,
                          content="<html>There was a problem providing the content you requested</html>"))
        assert b._block_page() is True


def cmd_selftest(a):
    """Offline self-test: no network, no project writes outside a temp dir."""
    import tempfile
    import zipfile
    from . import engine
    from .packer import marker_patterns, scan_zip, PrivacyLeak

    assert engine.DEFAULT_STEPS == ["inhand", "oa_content", "oa_vor", "publisher", "browser", "wp"], \
        "waterfall order changed"
    for pub, cap in (("10.1016/x", "elsevier"), ("10.1111/x", "wiley"), ("10.1080/x", "tandf")):
        assert engine.publisher_of_doi(pub) == cap
    cfg = {"contact_email": "somebody@example.org", "privacy_markers": ["SecretUni", "Hidden Lab"]}
    pats = marker_patterns(cfg)
    assert len(pats) == 4
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "t.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("clean.txt", "nothing identifiable here\n")
        assert scan_zip(zp, pats) == []
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("leak.txt", "affiliation: SECRETUNI press\nmailto somebody@example.org\n")
        assert scan_zip(zp, pats) == ["leak.txt"], "privacy gate failed to catch markers"
    _state_machine_tests()
    _browser_channel_tests()
    print("selftest PASS: waterfall order, publisher mapping, privacy-gate fuse, "
          "v0.4 state machine (fall-through, budgets, migration), "
          "v0.4.8 engine+browser (state-based challenges, no_access, SD PII, nav-first, "
          "pdf-stream capture with bounded re-fetch, affordance-contextual 403 verdicts, "
          "transport-fail fallback, url-fetch catcher + diagnostics, abandoned sources, "
          "reload-and-repoll), v0.5.0 browser (redirect settle, download catch in any tab, "
          "drain before re-navigation, reload before pdf navigation, viewer-off pref)")


def cmd_reopen(a):
    from .repair import run
    proj = Path(a.project)

    def under_project(x):
        return None if not x else (Path(x) if Path(x).is_absolute() else proj / x)
    run(under_project(a.status), step=a.step,
        uids=[u.strip() for u in a.uids.split(",") if u.strip()] if a.uids else None,
        records=under_project(a.records), doi_prefix=a.doi_prefix, note=a.note, apply=a.apply)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paper_acquire", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="project directory (holds inputs/, config.json, ...)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").add_argument("--records")
    for name in ("init", "status", "pack"):
        pass
    p = sub.add_parser("fetch-day")
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("pack")
    p.add_argument("--max-mb", type=float, default=8.0)
    sub.add_parser("selftest")
    p = sub.add_parser("reopen")
    p.add_argument("--status", required=True, help="status file, relative to --project")
    p.add_argument("--step", default="browser")
    p.add_argument("--uids", help="comma-separated uids (default: every qualifying record)")
    p.add_argument("--records", help="file with uid and DOI columns (needed for --doi-prefix)")
    p.add_argument("--doi-prefix", help="e.g. 10.1016/ for Elsevier")
    p.add_argument("--note", help="appended to each record's reopened history")
    p.add_argument("--apply", action="store_true", help="write (after a no-overwrite backup)")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        cmd_selftest(a)
    elif a.cmd == "reopen":
        cmd_reopen(a)
    else:
        {"init": cmd_init, "status": cmd_status, "fetch-day": cmd_fetch_day, "pack": cmd_pack}[a.cmd](a)


if __name__ == "__main__":
    main()
