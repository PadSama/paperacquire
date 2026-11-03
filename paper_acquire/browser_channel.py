"""Browser entitlement channel: a real Chrome (or Edge) with a dedicated persistent profile.

The user's institutional login in this browser is the entitlement, not an API key. One
manual login pass per run (can be disabled for unattended runs); the profile persists
under <project>/work/browser_profile, and the login's storage state is re-injected on
later launches within its freshness window (the Playwright authentication pattern). No
CAPTCHA or bot challenge is ever solved by the program: challenge pages get an adaptive
wait to clear, then go to the human or stop the publisher for the day.

Driver: Playwright by default. Config browser_driver "patchright" selects patchright, a
Playwright fork that removes automation markers; see RESPONSIBLE_USE.md before using it.

How a PDF is obtained:
- A visible View PDF / Download PDF control is the entitlement signal, and its real
  click is the first acquisition path. Citation meta tags and locked "Article PDF"
  buttons are not signals (they exist for every DOI), and no-access notes can render
  late, so the landing gets an access-state poll before any meta-derived URL is trusted.
- PDFs download instead of opening the browser's built-in viewer (profile setting
  plugins.always_open_pdf_externally; config browser_pdf_external, default true). A
  start-up self-test on 127.0.0.1 proves it on the installed browser; when the viewer
  stays active (Edge as the system PDF reader ignores the setting) the channel falls back
  to capturing the viewer's PDF stream.
- Downloads are caught at context level in any tab (a View PDF button may open a new
  tab); tabs opened during an attempt are closed when it ends, and nothing re-navigates a
  tab before pending downloads are drained.
- ScienceDirect: the landing is judged after Elsevier's linkinghub redirect; the page's
  own data (article.pdfDownload.urlMetadata) names the PDF link when the button has not
  rendered; a page without a visible control is reloaded before any PDF navigation; the
  request API is not used as a second, non-browser path to the publisher.
- A download that shows no progress for 20 s, or exceeds 180 s, is cancelled; save_as is
  entered only for a finished download, and saved copies are removed from the
  downloads folder.
- Verdicts: definitive no-access text on the landing is a per-paper no_access verdict; a
  403 on a PDF endpoint is a verdict only when the landing shows no entitlement control;
  a block page ("There was a problem providing the content you requested") is a block (a
  strike toward the publisher's daily stop), never a per-paper verdict.
- Each attempt leaves a trace of event classes and timings (no URLs or titles).

Entitlement outcomes depend on the user's institution and network; nothing here can
obtain a PDF the user is not entitled to.
"""
import os
import re
import sys
import time

STORAGE_MAX_AGE_S = 48 * 3600
CHALLENGE_TXT = (
    "just a moment|captcha|are you a robot|unusual traffic|verify you are human|cf-chl|"
    "px-captcha|bot detection|request unsuccessful. incapsula"
)
# Decisive interstitial titles: a challenge page NAMES itself. Long normal pages
# whose login widget merely contains CHALLENGE_TXT words are NOT challenges.
CHALLENGE_TITLE = re.compile(
    r"just a moment|checking your browser|attention required|verify you are human|"
    r"are you a robot|unusual traffic|request unsuccessful\. incapsula|bot detection|"
    r"cf[\s_-]?chl|px-captcha", re.I)
# Verbatim, conservative no-access markers (the Emerald string is the page's exact text).
NO_ACCESS_TXT = (
    "you do not currently have access to this content",
    "you do not have access to this content",
    "purchase this article",
    "buy this article",
)
PDF_PATTERNS = [
    ("tandfonline", "/doi/pdf/{doi}?download=true"), ("sagepub", "/doi/pdf/{doi}?download=true"),
    ("wiley", "/doi/pdfdirect/{doi}?download=true"), ("emerald", "/insight/content/doi/{doi}/full/pdf"),
    ("springer", "/content/pdf/{doi}.pdf"), ("worldscientific", "/doi/pdf/{doi}?download=true"),
    ("cambridge", "/content/pdf/{doi}.pdf"), ("degruyter", "/document/doi/{doi}/pdf"),
    ("oup", "/doi/pdf/{doi}"),
]  # sciencedirect is NOT here: its PDF URL comes from the landing PII (see below)
# Interstitial hops that page.goto can return on before the article loads
# (doi.org -> linkinghub "Redirecting" -> sciencedirect after about 2 s).
REDIRECTOR_HOSTS = ("linkinghub.elsevier.com",)
REDIRECT_TITLE = re.compile(r"^\s*redirecting\W*$", re.I)
# Trace host classes (v0.5.1): a trace names only these classes, never a URL, a title or
# any other host - an SSO/IdP host would otherwise carry the institution out of the
# machine and trip the upload privacy gate.
TRACE_HOSTS = (
    ("pdf.sciencedirectassets.com", "sd-assets"), ("sciencedirect.com", "sd"),
    ("linkinghub.elsevier.com", "linkinghub"), ("cell.com", "cell"), ("doi.org", "doi"),
    ("wiley.com", "wiley"), ("tandfonline.com", "tandf"), ("sagepub.com", "sage"),
    ("emerald.com", "emerald"), ("springer.com", "springer"), ("nature.com", "springer"),
    ("cambridge.org", "cambridge"), ("oup.com", "oup"), ("inderscience.com", "inderscience"),
    ("worldscientific.com", "worldscientific"), ("degruyter.com", "degruyter"), ("mdpi.com", "mdpi"),
    ("ncbi.nlm.nih.gov", "pmc"),
)


def host_class(url):
    """Trace-safe class of a URL's host: a known publisher class, 'viewer' for the
    Chromium PDF viewer's internal stream, 'blank', or 'other'."""
    import urllib.parse
    url = url or ""
    if url.startswith("chrome-extension://"):
        return "viewer"
    if not url or url.startswith("about:"):
        return "blank"
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for suffix, cls in TRACE_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return cls
    return "other"


# Issuer tokens of public certificate authorities (v0.5.1). The trace records only
# "public" or "other": an "other" issuer on doi.org means a TLS-terminating proxy sits
# between the browser and the publishers, which changes the fingerprint sites see.
PUBLIC_CA_TOKENS = (
    "digicert", "let's encrypt", "isrg", "google trust", "sectigo", "comodo", "globalsign",
    "amazon", "cloudflare", "entrust", "godaddy", "starfield", "microsoft", "baltimore",
    "thawte", "geotrust", "rapidssl", "certum", "ssl.com", "zerossl", "buypass", "harica",
    "identrust", "quovadis", "actalis", "trustasia", "gdca", "cfca", "sheca", "wotrus",
)
# Rendering heartbeat (v0.5.1): "visible:<rAF frames in 0.5 s>" for a rendering page,
# "...:stalled" when requestAnimationFrame does not run (hidden/minimized windows).
VIS_JS = """() => new Promise(done => { let n = 0; const t0 = performance.now();
  const f = () => { n++; if (performance.now() - t0 < 500) requestAnimationFrame(f);
    else done(document.visibilityState + ":" + n); };
  requestAnimationFrame(f);
  setTimeout(() => done(document.visibilityState + ":" + n + ":stalled"), 1500); })"""
SELFTEST_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]"
                b"/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def tls_class(issuer):
    low = (issuer or "").lower()
    if not low:
        return "none"
    return "public" if any(t in low for t in PUBLIC_CA_TOKENS) else "other"


def title_class(title):
    t = title or ""
    if REDIRECT_TITLE.search(t):
        return "redirecting"
    if CHALLENGE_TITLE.search(t):
        return "challenge"
    return "page" if t.strip() else "empty"


# Block pages (v0.5.1): a hard refusal, not a challenge that clears. ScienceDirect serves
# a Cloudflare 403 with this text to clients it refuses (documented by other tools,
# e.g. Zotero's notes). A block is challenge-class: it strikes toward
# the publisher's day stop and is never a per-paper no-access verdict.
BLOCK_PAGE_TXT = (
    "there was a problem providing the content you requested",
    "cloudflare_error_1000s_box",
    "cf-error-details",
)


BROWSER_DRIVERS = ("playwright", "patchright")


def load_driver(name=None):
    """(driver name, sync_playwright) for config browser_driver. Playwright by default;
    patchright, a Playwright fork that removes automation markers, only when asked for
    (see RESPONSIBLE_USE.md). Both share one API, so nothing else depends on the choice."""
    name = (name or "playwright").strip().lower()
    if name not in BROWSER_DRIVERS:
        raise ValueError(f"browser_driver must be one of {', '.join(BROWSER_DRIVERS)}, not {name!r}")
    if name == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright
    return name, sync_playwright


def is_block_page(html):
    low = (html or "")[:300000].lower()
    return any(m in low for m in BLOCK_PAGE_TXT)


# Playwright stores a finished download as <downloads_path>/<guid>; anything else in that
# folder is a transfer in flight (Chrome's <guid>.crdownload) (v0.5.2)
DOWNLOAD_DONE_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def download_folder_state(dl_dir):
    """{name: size} of every entry in the downloads folder; never raises. Sizes come from
    os.stat, not the directory listing: on Windows a listing can show a stale size for a
    file another process is still writing."""
    out = {}
    try:
        names = os.listdir(dl_dir)
    except OSError:
        return out
    for name in names:
        try:
            out[name] = os.stat(os.path.join(dl_dir, name)).st_size
        except OSError:
            out[name] = -1
    return out


def is_interstitial(title, html):
    """True only for a page STATE that blocks access: a decisive interstitial
    title, or a challenge-DOMINATED body (near-empty text that still carries
    challenge markers). Presence of 'captcha' in a normal page's login widget
    (a bottom-of-page personal-account login) is not
    a challenge and must not fuse a publisher."""
    if title and CHALLENGE_TITLE.search(title):
        return True
    if not html:
        return False
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    return len(text.split()) < 600 and bool(re.search(CHALLENGE_TXT, html[:20000], re.I))


def no_access_evidence(html, has_affordance):
    """Definitive no-access evidence only when the page both SAYS no access and
    offers no PDF affordance (a no-access string next to a View PDF control is a
    mixed panel, not a verdict)."""
    if has_affordance or not html:
        return False
    low = html[:40000].lower()
    return any(s in low for s in NO_ACCESS_TXT)


def sd_article_from_landing(url):
    """Canonical ScienceDirect article URL from the PII segment of a page we are ON
    (/pii/<PII>/ and /abs/pii/<PII>/ variants, or a pdfft URL); a DOI never encodes it."""
    import urllib.parse
    u = urllib.parse.urlparse(url or "")
    if "sciencedirect" not in (u.hostname or "").lower():
        return None
    m = re.search(r"/(?:abs/)?pii/(S[0-9A-Za-z()%\-]{8,40})(?:[/?#]|$)", u.path or "")
    if not m:
        return None
    return f"{u.scheme}://{u.netloc}/science/article/pii/{m.group(1)}"


def sd_pdfft_from_landing(url):
    """ScienceDirect PDF URL from the PII segment of a page we are ON. v0.5.1: the
    ?download=true form; the ?isDTMRedir=true form stopped opening in browsers in
    2024-10 (Zotero translators #3367/#3368), which matches the dead "dtm-redir
    retry" on every failed attempt in the 2026-10 ledger."""
    art = sd_article_from_landing(url)
    return f"{art}/pdfft?download=true" if art else None


def sd_json_pdf_url(page_url, raw_json):
    """The exact View PDF link from ScienceDirect's page JSON
    (article.pdfDownload.urlMetadata: path, pdfExtension, pii, queryParams.md5/pid;
    Zotero ScienceDirect.js step 5). Present on networks with access even when the
    control has not rendered. None when absent or incomplete."""
    import json
    import urllib.parse
    try:
        md = json.loads(raw_json)["article"]["pdfDownload"]["urlMetadata"]
        q = md.get("queryParams") or {}
        pii, md5, pid = md.get("pii"), q.get("md5"), q.get("pid")
        if not (pii and md5 and pid):
            return None
        path = (md.get("path") or "science/article/pii").strip("/")
        ext = md.get("pdfExtension") or "/pdfft"
        query = urllib.parse.urlencode({"md5": md5, "pid": pid})
        return urllib.parse.urljoin(page_url, f"/{path}/{pii}{ext}?{query}")
    except Exception:
        return None


def ensure_pdf_download_pref(profile_dir, external=True):
    """Make the dedicated profile download PDFs instead of opening Chromium's inline
    viewer: pref plugins.always_open_pdf_externally, the setting behind Chrome's
    "Download PDFs" option and the AlwaysOpenPdfExternally policy. With the viewer on,
    a PDF navigation yields an empty or viewer-shell body and no download event
    in real runs; with it off, the PDF arrives as a download in whichever
    tab loaded it. Merges into an existing Preferences file, never rewrites one it
    cannot parse, and leaves an unset pref alone when the viewer is wanted. Write it
    before the browser starts. Returns a short status for the console."""
    import json
    import os
    from pathlib import Path
    pref = Path(profile_dir) / "Default" / "Preferences"
    try:
        data = json.loads(pref.read_text(encoding="utf-8")) if pref.exists() else {}
    except Exception:
        return "Preferences unreadable, left untouched"
    if not isinstance(data, dict):
        return "Preferences unreadable, left untouched"
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else {}
    current = plugins.get("always_open_pdf_externally")
    if bool(current) == bool(external):
        return "already set"
    plugins["always_open_pdf_externally"] = bool(external)
    data["plugins"] = plugins
    pref.parent.mkdir(parents=True, exist_ok=True)
    tmp = pref.with_name("Preferences.paper_acquire.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, pref)
    return "set"


class Browser:
    # pdf catcher state (v0.4.3 streams, v0.5.0 downloads); class defaults so stubs stay valid
    _caught = None
    _catch_handler = None
    _downloads = None
    _dl_handler = None
    _dl_pages = None
    _page_handler = None
    _popups = None
    _seen = None
    _drain_s = 15          # drain window after a pdf navigation
    _click_drain_s = 30    # drain window after a control click (replaces the old 30 s download wait)
    pdf_external = True
    _viewer_active = False  # startup self-test saw PDFs open in the viewer (v0.5.1)
    _selftest_error = None
    _dl_ids = None
    _ctx_dl = False
    downloads_dir = None
    _stall_s = 20          # a download that stops growing this long is cancelled
    _dl_done = frozenset()  # finished-download files already accounted for (v0.5.2)
    _download_max_s = 180
    _trace = None          # events of the attempt in progress (v0.5.1)
    _trace_t0 = 0.0
    last_trace = None      # events of the last finished attempt, read by the engine

    def __init__(self, cfg, profile_dir):
        # Driver: Playwright unless config browser_driver says "patchright". No stealth
        # arguments and no custom user agent are added here, whichever driver runs.
        self.driver, sync_playwright = load_driver(cfg.get("browser_driver"))
        self._pw = sync_playwright().start()
        kw = {"user_data_dir": str(profile_dir), "headless": False,
              "accept_downloads": True, "no_viewport": True}
        # Chrome by default: Edge ignores the download-PDFs setting when it is the
        # system PDF reader (the start-up self-test detects that and says so)
        channel = (cfg.get("browser_channel") or "chrome").strip()
        if channel:
            kw["channel"] = channel
        # No proxy option by default: use the OS network path as-is. Playwright has NO
        # "direct://" proxy value - setting one fails every navigation with
        # net::ERR_PROXY_CONNECTION_FAILED. Set config "browser_proxy" to a real URL if
        # a specific egress is ever required.
        bp = (cfg.get("browser_proxy") or "").strip()
        if bp:
            # loopback stays direct: the startup PDF self-test serves from 127.0.0.1
            kw["proxy"] = {"server": bp, "bypass": "127.0.0.1,localhost"}
        # a known downloads folder, so a stalled download can be seen and
        # cancelled (Chromium writes an in-flight download as <guid>.crdownload)
        self.downloads_dir = profile_dir.parent / "browser_downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        kw["downloads_path"] = str(self.downloads_dir)
        self.storage_path = profile_dir.parent / "browser_storage_state.json"
        # v0.5.0: PDFs download instead of opening the inline viewer (config switch
        # browser_pdf_external, default true; false restores the viewer)
        self.pdf_external = bool(cfg.get("browser_pdf_external", True))
        try:
            pref = ensure_pdf_download_pref(profile_dir, self.pdf_external)
        except Exception as e:
            pref = f"pref not written: {type(e).__name__}"
        print(f"  [browser] pdf handling: {'download' if self.pdf_external else 'inline viewer'} ({pref})")
        self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        self._inject_storage_state()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if self.pdf_external:
            verdict = self._pdf_selftest()
            self._viewer_active = verdict is False
            print("  [browser] pdf self-test: " + {True: "PDFs download (verified)",
                  False: "VIEWER ACTIVE - PDFs open in the viewer despite the setting (Edge ignores it "
                         "when it is the system PDF reader; use browser_channel chrome); falling back "
                         "to viewer-stream capture",
                  None: f"not verified ({self._selftest_error or 'no download and no PDF response'})"}[verdict])
        # Self-check: print whether this window can actually load a page before asking a
        # human to log in (a silently dead window wastes evenings). The interactive login
        # gate is skipped entirely when config "browser_login_gate" is false.
        if sys.stdin.isatty():
            ok, err = False, ""
            for _ in range(3):
                try:
                    r = self.page.goto("https://doi.org/", timeout=30000, wait_until="domcontentloaded")
                    ok = bool(r and r.status < 400)
                    if ok:
                        break
                    err = f"HTTP {r.status if r else None}"
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:90]}"
                self.page.wait_for_timeout(4000)
            print("  [browser] self-check " + ("OK (doi.org loaded)." if ok else f"FAILED ({err})."))
            if cfg.get("browser_login_gate", True):
                for u in (cfg.get("browser_login_urls") or []):
                    u = (u or "").strip()
                    if not u:
                        continue
                    try:
                        # programmatic tabs start at about:blank; a user-opened tab sits on the
                        # new-tab page first, which is unreachable on some networks and looks hung
                        t = self.ctx.new_page()
                        t.goto(u, timeout=30000, wait_until="domcontentloaded")
                    except Exception:
                        pass  # a dead login entry must not block the gate
                input("  [browser] window open (dedicated profile). Complete institutional login "
                      "(Institution -> your university SSO) in the OPENED tabs - or type a full "
                      "https:// URL into an existing tab; do NOT open new tabs yourself (the "
                      "new-tab page can hang on this network); solve any human-verification "
                      "once; keep the window open but do not minimize it during the run "
                      "(a minimized page may never render its PDF button); press Enter here "
                      "to continue...")
                self.save_storage_state()
        self._front()

    # -- login persistence (Playwright auth-docs pattern) ----------------------
    def _inject_storage_state(self):
        """Re-add saved cookies (incl. session cookies) on every launch so the login
        pass survives browser restarts within STORAGE_MAX_AGE_S. Absent/stale file: skip."""
        try:
            if not self.storage_path.exists():
                return
            if time.time() - self.storage_path.stat().st_mtime > STORAGE_MAX_AGE_S:
                return
            import json
            state = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if state.get("cookies"):
                self.ctx.add_cookies(state["cookies"])
        except Exception:
            pass  # a broken state file must never block the run

    def save_storage_state(self):
        try:
            self.ctx.storage_state(path=str(self.storage_path))
        except Exception:
            pass

    def _pdf_selftest(self, timeout_ms=8000):
        """Prove on THIS browser that a PDF navigation becomes a download: serve a tiny
        PDF from 127.0.0.1 and open it in a scratch tab. True = downloads, False = the
        viewer rendered it, None = could not tell. No publisher is contacted."""
        import http.server
        import threading
        body = SELFTEST_PDF

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv, tab, seen = None, None, []
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            tab = self.ctx.new_page()
            # a plain function: Playwright's on() tags the handler with an attribute,
            # which a builtin such as list.append cannot carry (found by the replica suite)
            tab.on("download", lambda d: seen.append(d))
            resp = None
            try:
                resp = tab.goto(f"http://127.0.0.1:{srv.server_address[1]}/selftest.pdf", timeout=timeout_ms)
            except Exception:
                pass                      # "Download is starting" / ERR_ABORTED by design
            deadline = time.time() + timeout_ms / 1000
            while not seen and time.time() < deadline:
                tab.wait_for_timeout(250)
            if seen:
                for f in (seen[0].cancel, seen[0].delete):   # v0.5.3: leave no copy behind
                    try:
                        f()
                    except Exception:
                        pass
                return True
            ct = ""
            try:
                ct = (resp.headers or {}).get("content-type", "") if resp is not None else ""
            except Exception:
                pass
            return False if "pdf" in ct.lower() else None
        except Exception as e:
            self._selftest_error = f"{type(e).__name__}: {str(e)[:100]}"
            return None
        finally:
            for f in ((lambda: tab.close()) if tab else None, (lambda: srv.shutdown()) if srv else None):
                if f:
                    try:
                        f()
                    except Exception:
                        pass

    def close(self):
        self.save_storage_state()
        for f in (self.ctx.close, self._pw.stop):
            try:
                f()
            except Exception:
                pass
        self._clear_downloads()

    def _clear_downloads(self):
        """v0.5.3: remove what Chrome left in the downloads folder (finished copies that were
        not saved, partials). Saved PDFs live in the project's fulltext folder; these are
        duplicates that would otherwise pile up, as PDF copies without a .pdf name."""
        dl_dir = self.downloads_dir
        if dl_dir is None:
            return
        for name in download_folder_state(dl_dir):
            stem = name[:-len(".crdownload")] if name.endswith(".crdownload") else name
            if DOWNLOAD_DONE_NAME.match(stem):
                try:
                    os.remove(os.path.join(dl_dir, name))
                except OSError:
                    pass

    # -- fetching ----------------------------------------------------------------
    def _challenged(self):
        try:
            title = self.page.title() or ""
        except Exception:
            title = ""
        try:
            html = self.page.content()
        except Exception:
            html = ""
        return is_interstitial(title, html)

    def _wait_challenge_clear(self, auto_s=25, human_s=90):
        """Adaptive challenge wait. Auto-phase first (most clear within seconds);
        when a challenge persists and a human is attached, surface ONE clickable
        prompt (checkbox-style challenges need a single human click - never solved
        programmatically) and keep watching for human_s seconds."""
        deadline = time.time() + auto_s
        while time.time() < deadline:
            if not self._challenged():
                return True
            self.page.wait_for_timeout(2500)
        if self._challenged() and sys.stdin.isatty():
            print(f"    [browser] challenge page needs ONE human click (checkbox) in the "
                  f"window - watching for {human_s}s...")
            deadline = time.time() + human_s
            while time.time() < deadline:
                self.page.wait_for_timeout(2500)
                if not self._challenged():
                    print("    [browser] challenge cleared; resuming")
                    return True
        return not self._challenged()

    def _access_through_click(self):
        """Some publisher pages (ScienceDirect, for example) only expose
        the entitled PDF after an explicit 'Access through <institution>' activation
        click on the landing page. This is plain navigation (NOT a CAPTCHA): click the
        first matching affordance once and let the entitlement redirect settle."""
        try:
            for text_pat in ("Access through", "access via your institution"):
                el = self.page.query_selector(f"a:has-text('{text_pat}'), button:has-text('{text_pat}')")
                if el:
                    el.click(timeout=5000)
                    self._ev("activation")
                    self.page.wait_for_timeout(5000)
                    return True
        except Exception:
            pass
        return False

    # -- attempt trace (v0.5.1) ------------------------------------------------
    def _trace_start(self, kind):
        self._trace, self._trace_t0 = [], time.time()
        self._ev("start", kind=kind)

    def _ev(self, ev, **data):
        """Record one trace event; a no-op outside an attempt. Values must be classes,
        numbers or short fixed labels - never URLs, titles or free text."""
        tr = self._trace
        if tr is None or len(tr) >= 80:
            return
        tr.append({"t": round(time.time() - self._trace_t0, 1), "ev": ev, **data})

    def _trace_end(self, res):
        self._ev("end", res=res)
        self.last_trace, self._trace = self._trace, None

    def _front(self):
        """Keep the working tab in front and its window restored: the login gate opens
        its tabs on top of it, and a page in a minimized window may not render (Windows
        marks it hidden). Best effort, never raises."""
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            try:
                w = cdp.send("Browser.getWindowForTarget")
                if (w.get("bounds") or {}).get("windowState") == "minimized":
                    cdp.send("Browser.setWindowBounds", {"windowId": w["windowId"],
                                                         "bounds": {"windowState": "normal"}})
                    self._ev("unminimized")
            finally:
                cdp.detach()
        except Exception:
            pass
        try:
            self.page.bring_to_front()
        except Exception:
            pass

    def _settle_redirects(self, max_s=15):
        """Wait until the tab has left an interstitial redirector before the landing is
        judged. page.goto returns on Elsevier's linkinghub "Redirecting" page, about 2 s
        before ScienceDirect loads; judged there, the institution
        activation click and the first polls ran on the wrong page. Returns seconds."""
        import urllib.parse
        t0 = time.time()
        while time.time() - t0 < max_s:
            host = (urllib.parse.urlparse(self.page.url or "").hostname or "").lower()
            try:
                title = self.page.title() or ""
            except Exception:
                # the title read fails while a hop is committing; that is not a settled
                # landing (on Windows, settling here judged a block page before it had
                # loaded)
                self.page.wait_for_timeout(500)
                continue
            if host not in REDIRECTOR_HOSTS and not REDIRECT_TITLE.search(title):
                break
            self.page.wait_for_timeout(1000)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        self._ev("settled", s=round(time.time() - t0, 1), host=host_class(self.page.url))
        return time.time() - t0

    def _poll_affordance(self, doi):
        """Access-state settle poll (v0.4.2). Returns (kind, url, el, verdict); verdict
        is a no_access result tuple when the landing says so and shows no control."""
        url, kind, el = None, None, None
        for _ in range(5):
            kind, url, el = self._pdf_affordance(doi)
            if kind == "control":
                break
            if url and self._no_access():
                return kind, url, el, ("no_access", "landing says no access (no entitlement control)",
                                       self.page.url)
            self.page.wait_for_timeout(2000)
        if not url and self._no_access():
            return kind, url, el, ("no_access", "landing says no access", self.page.url)
        self._ev("affordance", kind=kind or "none", host=host_class(self.page.url))
        return kind, url, el, None

    def _reload_and_poll(self, art, doi):
        """ScienceDirect renders View PDF per load (one load may show no control for
        half a minute while a fresh load shows it within seconds). Reload the
        article page once, judge it like a landing, and poll again. Best effort: a
        failed reload or an unresolved challenge falls through to the pdf chain."""
        self._ev("reload")
        try:
            self.page.goto(art, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            return None, None, None, None
        self._settle_redirects()
        if not self._wait_challenge_clear(auto_s=15, human_s=45):
            return None, None, None, None
        if self._access_through_click():
            self._settle_redirects()
            self._wait_challenge_clear(auto_s=15, human_s=45)
        return self._poll_affordance(doi)

    def _pdf_affordance(self, doi):
        """Page evidence of a PDF link, as (kind, url, element). kind 'control' = a
        visible View PDF/pdfft/Download-PDF element - an ENTITLEMENT signal whose
        real click is the acquisition path of first resort; kind 'meta'/'pii' =
        citation meta or the SD landing PII -
        these exist for EVERY DOI regardless of entitlement (Emerald article pages
        carry citation_pdf_url even when locked,
        SD serves the ?md5= variant to both the link href and the meta), so they are
        NOT entitlement signals and the landing no-access copy decides first.
        Returns (None, None, None) when nothing on the page names a PDF."""
        import urllib.parse
        try:
            for sel in ("a:has-text('View PDF')", "a[href*='pdfft']", "a:has-text('Download PDF')",
                        "a[href*='/doi/pdf/']", "a[href*='/pdfdirect/']"):
                el = self.page.query_selector(sel)
                if el:
                    href = el.get_attribute("href")
                    if href:
                        return "control", urllib.parse.urljoin(self.page.url, href), el
        except Exception:
            pass
        try:
            el = self.page.query_selector('meta[name="citation_pdf_url"]')
            href = el.get_attribute("content") if el else None
        except Exception:
            href = None
        if href:
            return "meta", urllib.parse.urljoin(self.page.url, href), None
        if sd_article_from_landing(self.page.url):
            for raw in self._json_scripts():
                u = sd_json_pdf_url(self.page.url, raw)
                if u:
                    return "json", u, None
        u = sd_pdfft_from_landing(self.page.url)
        if u:
            return "pii", u, None
        return None, None, None

    def _json_scripts(self, limit=5):
        """Text of the page's script[type="application/json"] blocks (at most `limit`)."""
        out = []
        try:
            els = self.page.query_selector_all('script[type="application/json"]')
        except Exception:
            try:
                el = self.page.query_selector('script[type="application/json"]')
                els = [el] if el else []
            except Exception:
                els = []
        for el in (els or [])[:limit]:
            try:
                out.append(el.text_content() or "")
            except Exception:
                pass
        return out

    def _click_get_pdf(self, el, dest):
        """Human-gesture acquisition: click the visible control, then drain the
        context-level catcher for the click window (downloads in ANY tab, viewer
        streams). ScienceDirect can serve an empty 200 text/html relay to plain
        page.goto on both pdfft variants while a human completes the same click.
        v0.5.0: the old download wait listened on the landing tab only, but SD's View
        PDF opens a new tab, so it never fired there and cost 30 s on every success.
        The legacy wait stays for a driver without download events. Needs the catcher
        armed (_start_pdf_catch). Returns (result, detail)."""
        from pathlib import Path
        if self._dl_handler is None:
            try:
                with self.page.expect_download(timeout=30000) as dl:
                    try:
                        el.click(timeout=8000)
                    except Exception:
                        return "fail", "control click failed"
                dl.value.save_as(str(dest))
                if Path(dest).read_bytes()[:5] == b"%PDF-":
                    return "ok", "download (control click)"
            except Exception:
                pass
            return self._drain_pdf_catch(dest, "control click", window=self._click_drain_s)
        try:
            el.click(timeout=8000)
        except Exception:
            self._ev("click", ok=False)
            return "fail", "control click failed"
        self._ev("click", ok=True)
        return self._drain_pdf_catch(dest, "control click", window=self._click_drain_s)

    def _pattern_pdf_url(self, doi):
        """Last-resort DOI-pattern URL for known hosts (kept for pages that render
        neither meta nor a visible control). Never used for ScienceDirect."""
        import urllib.parse
        cur = urllib.parse.urlparse(self.page.url)
        host = cur.hostname or ""
        for token, path in PDF_PATTERNS:
            if token in host:
                return f"{cur.scheme}://{host}" + path.format(doi=doi)
        return None

    def _nav_get_pdf(self, url, dest, kind=None, ctx_label="goto"):
        """Navigate the page itself to the PDF URL - the exact identity a human
        click carries (cookies, referer, sec-fetch) - and capture the inline
        response body or an attachment download. A page-navigation 403 is verdict
        evidence ONLY in affordance context: with zero
        entitlement control on the landing it is a per-paper no_access; with a
        control visible it is challenge/noise class (fail, retryable); with no
        context (fetch_url) it keeps the legacy no_entitlement mapping. A 200 that
        is HTML is page evidence too: read it for no-access copy (Emerald's
        article-pdf endpoint serves an HTML shell when not
        entitled) and keep a bounded content-type/snippet diagnostic for the ledger.
        Returns (result, detail) with result in ok | fail | blocked | no_entitlement | no_access."""
        from pathlib import Path
        resp = None
        try:
            resp = self.page.goto(url, timeout=45000)
        except Exception:
            # navigating to a direct PDF often surfaces as a download event instead
            resp = None
        if resp is not None:
            try:
                status = resp.status
            except Exception:
                status = None
            try:
                ctype = (resp.headers or {}).get("content-type", "")
            except Exception:
                ctype = ""
            self._ev("nav", host=host_class(url), status=status, ct=(ctype or "").split(";")[0][:40])
            if status == 403:
                try:
                    b403 = resp.body()[:300000].decode("latin-1", "ignore")
                except Exception:
                    b403 = ""
                if is_block_page(b403):
                    self._ev("block_page", where="pdf")
                    return "blocked", "HTTP 403 block page (pdf endpoint)"
                if kind == "control":
                    # an entitlement control is visible on the landing: a hard 403
                    # here is challenge/noise class, never a per-paper verdict
                    # (entitlement-signal discipline)
                    return "fail", "HTTP 403 (page navigation; entitlement control visible)"
                if kind is None:
                    # fetch_url context: no landing evidence; keep the legacy
                    # mapping (its consumer counts a strike and fuses at N)
                    return "no_entitlement", "HTTP 403 (page navigation)"
                return "no_access", "HTTP 403 (pdf endpoint; no entitlement control on landing)"
            if status == 429:
                return "blocked", "HTTP 429"
            try:
                body = resp.body()
            except Exception:
                body = b""
            self._ev("nav_body", kind="pdf" if body[:5] == b"%PDF-" else ("html" if body else "empty"))
            if body[:5] == b"%PDF-":
                Path(dest).write_bytes(body)
                return "ok", "inline"
            if body:
                text = body[:20000].decode("latin-1", "ignore")
                if any(s in text.lower() for s in NO_ACCESS_TXT):
                    return "no_access", f"pdf endpoint says no access (HTTP {status} HTML)"
                if is_interstitial(None, text):
                    return "blocked", "pdf link returned a challenge page"
                snip = " ".join(text[:400].split())
                detail = f"navigation did not yield a pdf (HTTP {status}, {ctype[:40]}, {snip[:150]})"
            else:
                detail = f"navigation did not yield a pdf (HTTP {status}, {ctype[:40]}, empty body)"
        else:
            detail = "navigation did not yield a pdf (no response)"
            self._ev("nav", host=host_class(url), status=None)
        # v0.5.0: drain BEFORE anything re-navigates this tab. A PDF that arrives after
        # a JS relay (a download with the viewer off, a viewer stream with it on) was
        # destroyed by the old immediate second navigation of the same URL.
        if self._catch_handler is not None or self._dl_handler is not None:
            dres, ddetail = self._drain_pdf_catch(dest, ctx_label)
            if dres == "ok":
                return dres, ddetail
        if self._dl_handler is None:
            # legacy path for a driver without download events: re-navigate inside a
            # download wait on this tab (pre-0.5.0 behaviour)
            try:
                with self.page.expect_download(timeout=30000) as dl:
                    try:
                        self.page.goto(url, timeout=30000)
                    except Exception:
                        pass
                dl.value.save_as(str(dest))
                if Path(dest).read_bytes()[:5] == b"%PDF-":
                    return "ok", "download"
                return "fail", "download not a pdf"
            except Exception:
                pass
        return "fail", detail

    def _start_pdf_catch(self):
        """Arm a ctx-level one-shot pdf-stream catcher (v0.4.3). The callback keeps
        Response REFERENCES only - resp.body() inside a sync-API event callback can
        deadlock, so bodies are read in the drain loop, and a read failure degrades
        to "not caught" and never raises through fetch. Only complete 200 responses
        without Content-Range are taken (range partials would be torn files); the
        content-type filter runs before any body is read. ctx-level so a pdf the
        click opens in a NEW tab is seen too.
        v0.5.0: also catches DOWNLOADS on every open tab and on any tab opened during
        the attempt (SD's View PDF opens one); those tabs are closed by
        _stop_pdf_catch. Each caught item is processed once per attempt."""
        self._caught, self._downloads, self._popups, self._seen = [], [], [], set()
        self._dl_pages, self._dl_handler, self._page_handler = [], None, None
        self._dl_ids, self._ctx_dl = set(), False
        # v0.5.2: finished downloads already in the folder (earlier attempts) are not
        # taken for this attempt's
        self._dl_done = frozenset(n for n in download_folder_state(self.downloads_dir)
                                  if DOWNLOAD_DONE_NAME.match(n)) if self.downloads_dir else frozenset()

        def _on_download(d):
            # one Download object may arrive twice: from its page and from the
            # context (Playwright >= 1.60 repeats page downloads on the context)
            try:
                if id(d) in self._dl_ids or len(self._downloads) >= 4:
                    return
                self._dl_ids.add(id(d))
                self._downloads.append(d)
            except Exception:
                pass

        def _on_page(p):
            try:
                self._ev("new_tab")
                self._popups.append(p)
                p.on("download", _on_download)
            except Exception:
                pass

        pages = []
        try:
            pages = list(self.ctx.pages)
        except Exception:
            pages = []
        if self.page is not None and self.page not in pages:
            pages.append(self.page)
        for p in pages:
            try:
                p.on("download", _on_download)
                self._dl_pages.append(p)
            except Exception:
                pass
        if self._dl_pages:
            self._dl_handler = _on_download
            try:
                self.ctx.on("page", _on_page)
                self._page_handler = _on_page
            except Exception:
                self._page_handler = None
            try:
                self.ctx.on("download", _on_download)   # every tab, race-free (v0.5.1)
                self._ctx_dl = True
            except Exception:
                self._ctx_dl = False

        def _on_resp(r):
            try:
                if len(self._caught) >= 4:
                    return
                h = r.headers or {}
                ct = h.get("content-type", "").lower()
                if "application/pdf" not in ct and "octet-stream" not in ct:
                    return
                if r.status != 200 or h.get("content-range"):
                    return
                self._caught.append(r)
            except Exception:
                pass
        try:
            self.ctx.on("response", _on_resp)
            self._catch_handler = _on_resp
        except Exception:
            self._catch_handler = None   # catcher unavailable: drains become no-ops

    def _stop_pdf_catch(self):
        h = self._catch_handler
        if h is not None:
            try:
                self.ctx.remove_listener("response", h)
            except Exception:
                pass
        self._catch_handler = None
        if self._page_handler is not None:
            try:
                self.ctx.remove_listener("page", self._page_handler)
            except Exception:
                pass
        self._page_handler = None
        if self._ctx_dl and self._dl_handler is not None:
            try:
                self.ctx.remove_listener("download", self._dl_handler)
            except Exception:
                pass
        self._ctx_dl = False
        d = self._dl_handler
        for p in list(self._dl_pages or []) + list(self._popups or []):
            if d is not None:
                try:
                    p.remove_listener("download", d)
                except Exception:
                    pass
        self._dl_handler, self._dl_pages = None, []
        # close the tabs this attempt opened (SD opens one per View PDF click; dozens
        # accumulated over a run); never the working tab or the login tabs
        for p in list(self._popups or []):
            if p is self.page:
                continue
            try:
                if not p.is_closed():
                    p.close()
            except Exception:
                pass
        self._popups = []

    def _drain_pdf_catch(self, dest, ctx_label="goto", window=None):
        """Wait up to the window for a caught download or pdf stream and save the
        first %PDF- body as the acquisition; it then flows through the engine's
        accept_pdf title gate like any other 'ok'. Downloads come
        first: the browser itself fetched the file (v0.5.0). Each caught item is
        processed once per attempt (v0.5.0: the drain used to re-fetch every caught
        http stream once a second for the whole window).
        Response.body() on the LIVE response
        has no timeout and froze the run on a stalled emerald viewer stream. http(s)
        refs are therefore never read via body() - they are re-fetched bounded
        through the context's request API (shared cookies, 15s timeout). Only
        chrome-extension:// refs (the Chromium viewer's internal proxy resource,
        not re-fetchable) still use body() and print
        a diagnostic line first so any future hang is identifiable from the log.
        Residuals, documented: an extension body() or a stalled download's save_as
        may still hang; stop and restart the run (the in-flight attempt is not
        recorded, no retry budget is lost)."""
        import os
        from pathlib import Path
        seen = self._seen if self._seen is not None else set()
        deadline = time.time() + (self._drain_s if window is None else window)
        # v0.5.1: with PDFs downloading, an http(s) PDF response is normally followed by
        # its download; re-fetching it through the request API (Node TLS, not the
        # browser) would be a second, non-browser request to the publisher. Such
        # responses wait for a final pass after the window, taken only if no download
        # arrived (an embedded PDF, say). The viewer's own stream needs no re-fetch.
        defer_http = self.pdf_external and not self._viewer_active
        final = False
        while True:
            for d in list(self._downloads or []):
                if id(d) in seen:
                    continue
                seen.add(id(d))
                part = Path(str(dest) + ".download")
                head = b""
                if self._bounded_save(d, part):
                    try:
                        head = part.read_bytes()[:5]
                    except Exception:
                        head = b""
                self._ev("download", host=host_class(getattr(d, "url", "")), pdf=head == b"%PDF-")
                if head == b"%PDF-":
                    os.replace(part, dest)
                    try:
                        d.delete()
                    except Exception:
                        pass
                    return "ok", f"download ({ctx_label}) ({(getattr(d, 'url', '') or '')[:80]})"
                try:
                    part.unlink()
                except Exception:
                    pass
            for r in list(self._caught or []):
                if id(r) in seen:
                    continue
                url = getattr(r, "url", "") or ""
                if url.startswith("http") and defer_http and (not final or self._downloads):
                    continue      # a download already carried this attempt's PDF (or failed)
                seen.add(id(r))
                body = None
                if url.startswith("http"):
                    try:
                        rr = self.ctx.request.get(url, timeout=15000,
                                                  fail_on_status_code=False)
                        if rr.status == 200:
                            body = rr.body()
                    except Exception:
                        body = None
                else:
                    print(f"    [browser] draining captured pdf stream "
                          f"({url.split('/')[2] if url.count('/') > 2 else url}); "
                          f"a stall here is the known extension-body residual")
                    try:
                        body = r.body()
                    except Exception:
                        body = None
                self._ev("stream", host=host_class(url), pdf=bool(body and body[:5] == b"%PDF-"))
                if body and body[:5] == b"%PDF-":
                    Path(dest).write_bytes(body)
                    return "ok", f"viewer stream ({ctx_label}) ({url[:100]})"
            if final:
                break
            if time.time() >= deadline:
                final = True
                continue
            self.page.wait_for_timeout(1000)
        self._ev("drained", ctx=ctx_label, found=False)
        return "fail", "no pdf download or stream captured"

    def _bounded_save(self, d, part):
        """save_as only for a download that has finished (v0.5.2). Playwright stores a
        finished download as <downloads_path>/<guid>; until a new file of that name
        appears the transfer is in flight (Chrome writes <guid>.crdownload there,
        after a temp file elsewhere). If the folder shows no progress for _stall_s, or
        _download_max_s passes, the download is cancelled and save_as is not entered:
        save_as on an unfinished download blocks until the transfer ends. On Windows the
        download event arrives ~0.5 s before the partial exists; the 0.5.1 guard read
        that empty folder as "finished", and a stalled server then held save_as for
        12-15 minutes in Windows CI. True if saved."""
        dl_dir = self.downloads_dir
        if dl_dir is not None:
            t0 = since = time.time()
            last, had_partial = None, False
            while True:
                state = download_folder_state(dl_dir)
                done = sorted(n for n in state if DOWNLOAD_DONE_NAME.match(n) and n not in self._dl_done)
                partial = {n: z for n, z in state.items() if not DOWNLOAD_DONE_NAME.match(n)}
                had_partial = had_partial or bool(partial)
                if not partial and (done or had_partial):
                    break     # finished (new final file) or failed/cancelled (its partial is gone)
                sig = (tuple(sorted(partial.items())), tuple(done))
                if sig != last:
                    last, since = sig, time.time()
                elif time.time() - since > self._stall_s or time.time() - t0 > self._download_max_s:
                    try:
                        d.cancel()
                    except Exception:
                        pass
                    self._ev("download_stalled", s=round(time.time() - t0),
                             seen="partial" if had_partial else "nothing")
                    return False
                self.page.wait_for_timeout(500)
        try:
            d.save_as(str(part))
            ok = True
        except Exception:
            ok = False
        try:
            from pathlib import Path
            self._dl_done = frozenset(self._dl_done) | {Path(d.path()).name}
        except Exception:
            pass
        if ok:
            try:
                d.delete()       # v0.5.3: the copy in browser_downloads/ is not kept
            except Exception:
                pass
        return ok

    def _vis(self):
        try:
            return str(self.page.evaluate(VIS_JS))[:24]
        except Exception:
            return "n/a"

    def fetch(self, doi, dest):
        """Returns (result, detail, pdf_url) with result in
        ok | fail | blocked | no_entitlement | no_access. Leaves the attempt's trace in
        self.last_trace (v0.5.1)."""
        self._trace_start("fetch")
        out = ("fail", "exception", None)
        try:
            out = self._fetch(doi, dest)
            return out
        finally:
            self._trace_end(out[0])

    def _fetch(self, doi, dest):
        from pathlib import Path
        import urllib.parse
        self._front()
        start = "https://doi.org/" + doi
        try:
            resp0 = self.page.goto(start, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return "fail", f"landing failed ({type(e).__name__})", start
        try:
            first_title = self.page.title()
        except Exception:
            first_title = ""
        try:
            issuer = ((resp0.security_details() or {}).get("issuer") or "") if resp0 is not None else ""
        except Exception:
            issuer = ""
        self._ev("landing", host=host_class(self.page.url), title=title_class(first_title),
                 tls=tls_class(issuer))
        # v0.5.0: judge the REAL landing. Wait out redirector hops first, so the
        # challenge check, the institution activation click and the poll run on the
        # article page, not on linkinghub's "Redirecting"
        self._settle_redirects()
        # v0.5.1: a block page does not clear - strike now instead of waiting for it
        if self._block_page():
            self._ev("block_page", where="landing")
            return "blocked", "landing is a block page", self.page.url
        # Adaptive challenge wait + one-click human window (see _wait_challenge_clear)
        if not self._wait_challenge_clear():
            return "blocked", "landing is a challenge page", self.page.url
        # Entitlement activation: SD-class pages hide View PDF until the institution
        # link is clicked; the click may bounce through the IdP, so settle and re-wait
        if self._access_through_click():
            self._settle_redirects()
            if not self._wait_challenge_clear(auto_s=15, human_s=60):
                return "blocked", "challenge after institution activation", self.page.url
        # PDF affordance + access-state settle poll (v0.4.2, _poll_affordance). A
        # visible control is an entitlement signal whose click is the acquisition
        # path; meta/PII alone is not (every DOI has them), and the
        # no-access copy may render LATE (emerald's access note is client-side), so
        # while no control is visible the poll keeps watching before any meta/PII url
        # is trusted.
        kind, url, el, verdict = self._poll_affordance(doi)
        self._ev("vis", v=self._vis())
        if verdict:
            return verdict
        tags = {"ctl": "yes" if kind == "control" else "no"}
        # v0.4.3/v0.5.0: arm the ctx-level catcher (viewer streams, and downloads in
        # any tab) for the whole acquisition attempt
        self._start_pdf_catch()
        try:
            res, detail, tried = "fail", "", set()
            art = sd_article_from_landing(self.page.url) if kind != "control" else None
            if art and kind == "json":
                # v0.5.1: the page JSON names the exact View PDF link although the
                # control has not rendered; with PDFs downloading, opening it in this
                # tab is the cheapest path, before any reload
                tags["json"] = "yes"
                res, detail = self._nav_get_pdf(url, dest, kind)
                tried.add(url)
                if res == "ok":
                    return res, f"{detail} (page json)", url
                if res != "fail":
                    return res, detail, url
            if art:
                # v0.5.0 reload BEFORE any pdf navigation: ScienceDirect renders View
                # PDF per load, and a pdfft navigation without the control yields an
                # empty relay
                k2, u2, e2, v2 = self._reload_and_poll(art, doi)
                if v2:
                    return v2
                if k2 == "control":
                    tags["reload"] = "ctl"
                    res, detail = self._click_get_pdf(e2, dest)
                    if res == "ok":
                        return res, f"{detail} (after reload)", art
                    kind, url, el = k2, u2, None   # click failed: the chain uses the control's href
                else:
                    tags["reload"] = "no-ctl"
                    if u2:
                        kind, url = k2, u2
            if not url:
                url = self._pattern_pdf_url(doi)
                kind = "pattern"
            if not url:
                return "fail", "no pdf link on landing page", self.page.url
            # a real click on the visible control is the human path; the href is the
            # navigation fallback (a goto on ScienceDirect's href can yield an empty
            # relay while the click downloads)
            if kind == "control" and el is not None:
                res, detail = self._click_get_pdf(el, dest)
                if res == "ok":
                    return res, detail, url
                if self._downloads:
                    # the click's download came from this very link and failed (stalled
                    # or not a PDF): navigating it again would repeat the same transfer
                    tried.add(url)
            if "/doi/reader/" in url:
                # sage's citation meta names the reader page (PDF inside an embedded
                # viewer); the direct /doi/pdf/ endpoint serves
                # the file when navigated from the article page
                url = url.replace("/doi/reader/", "/doi/pdf/")
            # page navigation first (it drains the catcher before giving up); the 403
            # verdict inside is affordance-contextual (see _nav_get_pdf). A URL this
            # attempt already navigated is not requested again.
            if url not in tried:
                res, detail = self._nav_get_pdf(url, dest, kind)
                tried.add(url)
            sd = "sciencedirect" in (urllib.parse.urlparse(url).hostname or "")
            if res == "fail" and sd:
                # the plain ?download=true form of the same PII as the last resort. The
                # trigger reads the AFFORDANCE url's host, never self.page.url: after a
                # failed PDF navigation the live page URL is unreliable
                alt = sd_pdfft_from_landing(url)
                if alt and alt not in tried:
                    res2, detail2 = self._nav_get_pdf(alt, dest, kind)
                    tried.add(alt)
                    if res2 != "fail":
                        return res2, detail2, alt
                    detail = f"{detail}; download-form retry: {detail2}"
                    url = alt
            if res != "fail":
                return res, detail, url
            if not sd and not self._downloads:
                # API-context request as the last path (rarely needed); skipped for
                # SD, whose WAF 403s API-context requests even under a fully entitled SSO
                # session (v0.4.1), and when the browser itself already received a download
                # this attempt - a stalled or bad transfer is not rescued by a second, non-
                # browser (Node TLS) request. 60 s bound (was 120 s).
                try:
                    r = self.ctx.request.get(url, timeout=60000, fail_on_status_code=False)
                    if r.status == 200 and r.body()[:5] == b"%PDF-":
                        Path(dest).write_bytes(r.body())
                        return "ok", "api-context", url
                except Exception:
                    pass
            # compact diagnostics, kept near the front so the ledger's 200-char cut
            # keeps them: control seen, reload outcome, downloads and streams caught
            tags["dl"] = len(self._downloads or [])
            tags["st"] = len(self._caught or [])
            tag = " ".join(f"{k}={v}" for k, v in tags.items())
            return "fail", f"no pdf via navigation or request [{tag}] ({detail})", url
        finally:
            self._stop_pdf_catch()

    def _block_page(self):
        # v0.5.2: content() refuses while the page is navigating; let it land and look
        # again instead of reading "no block page"
        for _ in range(3):
            try:
                return is_block_page(self.page.content())
            except Exception:
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    self.page.wait_for_timeout(500)
        return False

    def _no_access(self):
        try:
            return no_access_evidence(self.page.content(), False)
        except Exception:
            return False

    def fetch_url(self, url, dest):
        """Direct URL fetch for repository/OA copies (see _fetch_url). Leaves the
        attempt's trace in self.last_trace (v0.5.1)."""
        self._trace_start("url")
        out = ("fail", "exception")
        try:
            out = self._fetch_url(url, dest)
            return out
        finally:
            self._trace_end(out[0])

    def _fetch_url(self, url, dest):
        """Direct URL fetch for repository/OA copies whose script download hit a
        challenge page, an entitled-only 403, or a transport failure (v0.4.5).
        The page navigates first - the real browser identity; the API-context
        request is the secondary path only (SAGE has refused the API-context GET
        on a URL the browser itself can load). A
        paywalled/no-access HTML body is NOT a challenge and must never strike
        the publisher. PMC renders the
        pdf in Chromium's inline viewer exactly like SD - the catcher is armed
        for the whole attempt and drained before the API-context request (a
        viewer hit also saves the longest 120s call), and verdict-class returns
        stay immediate; request failures now carry the real exception class and
        the nav outcome instead of a bare "request error".
        Returns (result, detail) with result in ok | fail | blocked."""
        from pathlib import Path
        self._front()
        self._start_pdf_catch()
        try:
            # the navigation drains the catcher (downloads, viewer streams) itself
            # before it gives up (v0.5.0)
            res, detail = self._nav_get_pdf(url, dest, ctx_label="url fetch")
            if res == "ok":
                return "ok", detail
            if res in ("no_entitlement", "blocked"):
                return "blocked", detail      # refusal evidence: honest, N-strike fuse
            if res == "no_access":
                return "fail", "no access page (not a challenge)"
            if res != "fail":
                return res, detail
            if self._downloads:
                return "fail", f"download did not complete ({detail[:80]})"
            try:
                r = self.ctx.request.get(url, timeout=60000, fail_on_status_code=False)
                status, body = r.status, r.body()
            except Exception as e:
                return "fail", f"request error ({type(e).__name__}; nav: {detail[:80]})"
            if status == 200 and body[:5] == b"%PDF-":
                Path(dest).write_bytes(body)
                return "ok", "api-context"
            if status in (403, 429):
                return "blocked", f"HTTP {status}"
            text = body[:20000].decode("latin-1", "ignore")
            if body[:5] != b"%PDF-" and any(s in text.lower() for s in NO_ACCESS_TXT):
                return "fail", "no access page (not a challenge)"
            if is_interstitial(None, text):
                return "blocked", "challenge page"
            return "fail", f"HTTP {status}, not a PDF"
        finally:
            self._stop_pdf_catch()
