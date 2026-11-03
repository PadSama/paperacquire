"""Acquisition engine: a retry-budget state machine with fall-through defers.

Waterfall per record (default; the step list and extra steps are injectable so a
project adapter can keep its own step names and state files):
inhand -> oa_content -> oa_vor -> publisher -> browser -> wp.

Semantics:
- A deferred step (daily stop, daily limit, retry backoff) does not abort the record:
  the waterfall falls through to later steps, so a stopped API never starves the
  browser channel. A record is terminal only when every step is exhausted or not
  applicable; "not_found" is never produced by a transient deferral.
- Step failures consume per-step retry budgets (STEP_TRIES) instead of closing a step
  for good. Deterministic entitlement refusals (an API 403) exhaust that channel only;
  the browser step keeps its own attempts.
- Publisher login entitlement and API-key entitlement stay independent.
- Challenge pages: the browser channel waits adaptively for them to clear and stops a
  publisher for the day only after N consecutive unresolved challenges
  (browser_block_stop, default 2), never on one.
- Publishers without entitlement can be declared in config publisher_entitlement
  (e.g. {"wiley": "no_sso_entitlement"}) and are skipped honestly instead of lingering.
- Telemetry: only real network attempts are logged; daily-stop and limit skips return
  skip_fuse/skip_cap and stay out of the ledger.

Default limits (config "limits", all overridable): at most 50 requests per publisher
family per day, counted across the browser, API and direct channels (stop keys stay per
channel); at most 150 browser attempts per day; randomized gaps of 15-30 s between
downloads and 25-40 s between browser attempts; 1.5 s between metadata API calls; an
OpenAlex spend meter of $0.95 per day (search $0.001, content PDF $0.01, checked before
every paid call); a publisher API stops for the day after 5 consecutive 403s; a metadata
429 waits 60-90 s and retries once, and a second 429 stops that host for the day.
Metadata is cached only on a definitive answer, and the open-access steps defer instead
of burning budget when metadata is missing.

Layout injection: a project adapter passes a dict of paths so this engine reads and
writes the project's existing state files with no conversion. Legacy records
(steps_done lists, api_403 flags) are migrated lazily on first process(): each legacy
done-step becomes one spent try that stays open.
"""
import json
import os
import random
import re
import time
import urllib.parse
from pathlib import Path

DEFAULT_STEPS = ["inhand", "oa_content", "oa_vor", "publisher", "browser", "wp"]
STEP_TRIES = {"inhand": 1, "oa_content": 2, "oa_vor": 2, "candidates": 2, "publisher": 3,
              "browser": 3, "wp": 3, "cloud_vor": 3, "cloud_wp": 3}
DEFER_RESULTS = ("defer", "skip_fuse", "skip_cap", "skip_dead")
SKIP_RESULTS = ("skip_fuse", "skip_cap", "skip_dead")
VERSION_MAP = {"publishedVersion": "published", "acceptedVersion": "accepted", "submittedVersion": "WP"}
PUBLISHER_BY_PREFIX = {
    "10.1016": "elsevier", "10.1111": "wiley", "10.1002": "wiley", "10.1007": "springer",
    "10.1057": "springer", "10.1186": "springer", "10.1038": "springer", "10.1177": "sage",
    "10.1080": "tandf", "10.1108": "emerald", "10.1017": "cambridge", "10.1093": "oxford",
    "10.3390": "mdpi", "10.1257": "aea", "10.1504": "inderscience", "10.1142": "worldscientific",
    "10.1515": "degruyter", "10.3389": "frontiers", "10.1371": "plos",
}
PUBLISHERS = frozenset(PUBLISHER_BY_PREFIX.values())
# OpenAlex cost per call (help.openalex.org, "Example costs"): keyword search $1/1000,
# content PDF $10/1000; single lookups by ID or DOI are free. The daily spend is metered
# against limits.openalex_daily_usd (default $0.95).
OPENALEX_USD = {"search": 0.001, "content": 0.01}
CHALLENGE = re.compile(rb"just a moment|captcha|are you a robot|unusual traffic|verify you are human|"
                       rb"cf-chl|px-captcha|bot detection|request unsuccessful\. incapsula", re.I)
HOST_TOKENS = [
    (r"sciencedirect|elsevier", "elsevier"), (r"wiley", "wiley"),
    (r"springer|biomedcentral|nature\.com", "springer"), (r"sagepub", "sage"),
    (r"tandfonline", "tandf"), (r"emerald\.com|emeraldinsight", "emerald"),
    (r"cambridge\.org", "cambridge"), (r"academic\.oup|(^|\.)oup\.com", "oxford"), (r"mdpi", "mdpi"),
    (r"aeaweb", "aea"), (r"inderscience", "inderscience"), (r"worldscientific", "worldscientific"),
    (r"degruyter", "degruyter"), (r"frontiersin", "frontiers"), (r"plos\.org", "plos"), (r"ssrn", "ssrn"),
    (r"content\.openalex\.org", "openalex_content"),
]


def publisher_of_doi(doi):
    return PUBLISHER_BY_PREFIX.get((doi or "").split("/")[0].lower())


def cap_key(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for pat, key in HOST_TOKENS:
        if re.search(pat, host):
            return key
    return host or "unknown"


def family_of(cap):
    """Publisher family of a day-count key. A publisher's browser key (`elsevier`) and
    its channel namespaces (`elsevier_api`, `elsevier_direct`) form one family: the daily
    per-publisher limit (default 50) counts all channels together. Stop keys stay per
    namespace, so an API 403 still never stops the browser."""
    for suffix in ("_api", "_direct"):
        if cap.endswith(suffix) and cap[: -len(suffix)] in PUBLISHERS:
            return cap[: -len(suffix)]
    return cap


def diag_snippet(head, n=80):
    """A short, single-line, token-scrubbed excerpt of a response body for the attempts
    ledger: long alphanumeric runs (keys, tokens, signatures) become '~'."""
    text = " ".join((head or b"")[:4000].decode("latin-1", "ignore").split())
    return re.sub(r"[A-Za-z0-9_\-]{24,}", "~", text)[:n]


def norm_title(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def title_match_ratio(want, got):
    a, b = set(norm_title(want).split()), set(norm_title(got).split())
    return len(a & b) / max(1, len(a))


class NetworkDown(Exception):
    pass


class DayBook:
    """Per-day counters/fuses. Unknown existing keys are preserved on save."""

    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.d.setdefault("count", {})
        self.d.setdefault("blocked", {})
        self.d.setdefault("consec403", {})
        self.d.setdefault("consec_blocked", {})
        self.d.setdefault("browser_total", 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self):
        self.path.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")

    def used(self, k):
        return self.d["count"].get(k, 0)

    def add(self, k):
        self.d["count"][k] = self.used(k) + 1
        self.save()

    def block(self, k, why):
        if k not in self.d["blocked"]:
            self.d["blocked"][k] = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {why}"
            self.save()
            print(f"    ! {k} stopped for today: {why}")

    def blocked(self, k):
        return k in self.d["blocked"]


class Layout:
    def __init__(self, project: Path, override=None):
        p = dict(status=project / "work" / "status.json",
                 day=project / "work" / "day",
                 meta=project / "work" / "meta",
                 attempts=project / "logs" / "attempts.jsonl",
                 fulltext=project / "fulltext",
                 tmp=project / "work" / "tmp",
                 browser_profile=project / "work" / "browser_profile",
                 inhand=project / "inputs" / "inhand")
        p.update({k: Path(v) for k, v in (override or {}).items()})
        self.status, self.day, self.meta = p["status"], p["day"], p["meta"]
        self.attempts, self.fulltext, self.tmp = p["attempts"], p["fulltext"], p["tmp"]
        self.browser_profile, self.inhand = p["browser_profile"], p["inhand"]


class Acquirer:
    def __init__(self, project: Path, cfg, args, layout=None, steps=None, extra_steps=None, candidates=None):
        import requests
        self.requests = requests
        self.project, self.cfg, self.lim, self.args = project, cfg, cfg["limits"], args
        self.email = (cfg.get("contact_email") or "").strip()
        self.s = requests.Session()
        from . import __version__
        # tool and version only (no repository URL, which would tie every request to an
        # account); the operator's contact e-mail is added only when configured
        self.s.headers["User-Agent"] = (f"paper-acquire/{__version__} (non-commercial research" +
                                        (f"; mailto:{self.email}" if self.email else "") + ")")
        self.key = {k: os.environ.get(k, "") for k in
                    ("OPENALEX_API_KEY", "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN", "WILEY_TDM_TOKEN")}
        self.L = Layout(project, layout)
        self.L.fulltext.mkdir(parents=True, exist_ok=True)
        self.day = DayBook(self.L.day / f"{time.strftime('%Y-%m-%d')}.json")
        self.status = json.loads(self.L.status.read_text(encoding="utf-8")) if self.L.status.exists() else {}
        self.candidates = candidates or {}
        self.extra_steps = extra_steps or {}
        self.steps = list(steps or DEFAULT_STEPS)
        self.last_dl = 0.0
        self.browser = None
        self._nap = time.sleep

    # -- bookkeeping ----------------------------------------------------------
    def _script_cap(self, cap):
        """Script-channel downloads (oa/wp/cloud candidates) must fuse in the script
        namespace, never the browser's: otherwise a publisher WAF challenge to the
        script user agent would stop the bare publisher key and starve the browser
        channel for the same publisher."""
        return cap + "_direct" if cap in set(PUBLISHER_BY_PREFIX.values()) else cap

    def set_status(self, uid, **kw):
        s = self.status.setdefault(uid, {})
        s.update(kw)
        s["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.L.status.parent.mkdir(parents=True, exist_ok=True)
        self.L.status.write_text(json.dumps(self.status, ensure_ascii=False, indent=1), encoding="utf-8")

    def log(self, uid, step, source, url, cap, result, http=None, detail="", version=None):
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "uid": uid, "step": step, "source": source,
               "url": self.redact(url), "cap": cap, "result": result, "http": http,
               "version": version, "detail": self.redact(str(detail))[:200]}   # error texts can carry URLs
        self.L.attempts.parent.mkdir(parents=True, exist_ok=True)
        with self.L.attempts.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def redact(self, url):
        return re.sub(r"(api_key|apikey|token|mailto)=[^&]+", r"\1=***", url or "")

    def cap_limit(self, cap):
        return 90 if cap == "openalex_content" else int(self.lim.get("per_publisher_daily", 50))

    def cap_used(self, cap):
        """Requests counted today for the cap's whole publisher family."""
        fam = family_of(cap)
        return sum(n for k, n in self.day.d["count"].items() if family_of(k) == fam)

    def cap_full(self, cap):
        return self.cap_used(cap) >= self.cap_limit(cap)

    def _openalex_charge(self, kind):
        """Charge one OpenAlex call against today's budget. Returns False, and
        charges nothing, when the call would take the day over openalex_daily_usd. The
        day is the local day of the day book; OpenAlex resets its own allowance at
        midnight UTC."""
        cost = OPENALEX_USD[kind]
        spent = float(self.day.d.get("openalex_usd", 0.0))
        if spent + cost > float(self.lim.get("openalex_daily_usd", 0.95)) + 1e-9:
            return False
        self.day.d["openalex_usd"] = round(spent + cost, 6)
        self.day.save()
        return True

    def _budget(self, step):
        return int((self.lim.get("step_retry_budgets") or {}).get(step, STEP_TRIES.get(step, 2)))

    # -- state machine --------------------------------------------------------
    def _migrate_record(self, st):
        """Legacy steps_done list -> step_state. Each legacy done-step counts as one
        spent try but stays open, so a step killed by a transient failure gets exactly
        one fresh chance under the new budgets. api_403 flags are dropped (the gate is
        gone); tried_urls and terminal states are preserved untouched."""
        if "step_state" in st:
            return
        ss = {}
        for s in st.get("steps_done") or []:
            if s in self.steps:
                ss[s] = {"status": "open", "tries": 1}
        st["step_state"] = ss
        st.pop("api_403", None)

    def _derived_done(self, ss):
        return [s for s in self.steps if ss.get(s, {}).get("status") in ("exhausted", "na")]

    def process(self, uid, rec):
        st = self.status.get(uid, {})
        if st.get("state") == "acquired":
            return "acquired"
        st = self.status.setdefault(uid, {})
        self._migrate_record(st)
        ss = st.setdefault("step_state", {})
        deferred, retry_open = [], []
        for step in self.steps:
            cur = ss.get(step)
            if cur and cur.get("status") != "open":
                continue
            tries = (cur or {}).get("tries", 0)
            res = self.handler(step)(uid, rec)
            if res == "ok":
                ss[step] = {"status": "ok", "tries": tries}
                self.set_status(uid, state="acquired", next_step=None, steps_done=self._derived_done(ss))
                return "acquired"
            if res == "halt":
                # project semantics forbid falling through past this step (e.g. a record
                # that must wait for ITS own file, never substitute another version)
                self.set_status(uid, state="pending", next_step=step, steps_done=self._derived_done(ss))
                return "deferred"
            if res in DEFER_RESULTS:
                deferred.append(step)
                continue
            if res == "skip":
                ss[step] = {"status": "na", "tries": tries}
                continue
            tries += 1
            if res == "exhaust" or tries >= self._budget(step):
                ss[step] = {"status": "exhausted", "tries": tries}
            else:
                ss[step] = {"status": "open", "tries": tries}
                retry_open.append(step)
        open_steps = [s for s in self.steps if not ss.get(s) or ss[s].get("status") == "open"]
        if not open_steps:
            self.set_status(uid, state="not_found", next_step=None, steps_done=self._derived_done(ss))
            return "not_found"
        nxt = open_steps[0]
        self.set_status(uid, state="pending", next_step=nxt, steps_done=self._derived_done(ss))
        pub = publisher_of_doi(rec.get("DOI"))
        fused = [k for k in (pub, (pub or "") + "_api", (pub or "") + "_direct")
                 if k and k in self.day.d.get("blocked", {})]
        suffix = f" [{','.join(fused)} fused today]" if fused else ""
        if retry_open:
            return f"retry: {nxt}{suffix}"
        return f"deferred{suffix}"

    # -- http -----------------------------------------------------------------
    def api(self, url, params=None, headers=None, openalex=None):
        """Metadata GET, strictly serial and at least api_gap_s apart. A 429 waits 60-90 s
        and retries once; a second 429 stops that host's metadata calls for the day
        (limits: api_gap_s, metadata 429 rule). `openalex` names a paid OpenAlex call type
        (OPENALEX_USD) that is charged before each request. Returns the Response
        or None; self.api_skipped is True when no answer came because the call was
        refused locally (host stopped, budget spent) or the server refused it today."""
        self.api_skipped = False
        key = "api:" + (urllib.parse.urlparse(url).hostname or "")
        if self.day.blocked(key):
            self.api_skipped = True
            return None
        gap = float(self.lim.get("api_gap_s", 1.5))
        for attempt in (1, 2):
            if openalex and not self._openalex_charge(openalex):
                self.api_skipped = True
                return None
            time.sleep(max(0, gap - (time.time() - getattr(self, "_last_api", 0.0))))
            self._last_api = time.time()
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=60)
            except self.requests.RequestException:
                if self.offline():
                    raise NetworkDown()
                return None
            if r.status_code != 429:
                return r
            if attempt == 1:
                time.sleep(random.uniform(60, 90))
        self.day.block(key, "HTTP 429 twice")
        self.api_skipped = True
        return None

    def _abandoned(self):
        """Host keys the project has abandoned (config "abandoned_sources", e.g. a
        repository host that does not open from the operator's network even in a real
        browser). Exact cap_key equality: an entry that is a publisher
        token abandons that whole family. Entries are only ever matched against
        cap_key output (bare host names or tokens)."""
        return self.cfg.get("abandoned_sources") or []

    def download(self, url, cap, headers=None, api_semantics=False):
        # match on the URL's own cap_key (host -> token normalization), NOT the
        # passed cap: the passed cap is the post-_script_cap namespace
        # (elsevier_direct/springer_direct/...), so a publisher-token entry in
        # abandoned_sources would silently miss the script channel without this.
        # The passed cap continues to serve the fuse/cap logic below.
        if cap_key(url) in self._abandoned():
            return "skip_dead", None, "abandoned source"   # zero network, zero bookkeeping
        if self.day.blocked(cap):
            return "skip_fuse", None, "blocked today"
        if self.cap_full(cap):
            return "skip_cap", None, "daily cap reached"
        if cap == "openalex_content" and not self._openalex_charge("content"):
            return "skip_cap", None, "OpenAlex daily budget reached"
        lo, hi = self.lim.get("download_gap_s", [15, 30])
        wait = self.last_dl + random.uniform(float(lo), float(hi)) - time.time()
        if wait > 0:
            time.sleep(wait)
        self.day.add(cap)
        tmp = self.L.tmp / f"dl_{int(time.time() * 1000)}.bin"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        head, els_status = b"", None
        try:
            with self.s.get(url, headers=headers, timeout=120, stream=True) as r:
                http = r.status_code
                els_status = (getattr(r, "headers", None) or {}).get("X-ELS-Status")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                        if len(head) < 8192:
                            head += chunk[:8192]
        except self.requests.RequestException as e:
            self.last_dl = time.time()
            tmp.unlink(missing_ok=True)
            if self.offline():
                raise NetworkDown()
            return "fail", None, type(e).__name__
        self.last_dl = time.time()
        if http == 200 and head[:5] == b"%PDF-":
            self.day.d["consec403"][cap] = 0
            self.day.save()
            return "ok", http, tmp
        tmp.unlink(missing_ok=True)
        if http == 429:
            self.day.block(cap, "HTTP 429")
            return "defer", http, "HTTP 429"
        if CHALLENGE.search(head):
            self.day.block(cap, "challenge page")
            return "defer", http, "challenge page"
        if http == 401:
            self.day.block(cap, "HTTP 401 (key/token refused)")
            return "defer", http, "HTTP 401"
        if http == 403:
            if api_semantics:
                c = self.day.d["consec403"].get(cap, 0) + 1
                self.day.d["consec403"][cap] = c
                self.day.save()
                if c >= int(self.lim.get("consecutive_403_stop", 5)):
                    self.day.block(cap, f"{c} consecutive 403 (outside key entitlement or off-network)")
                # X-ELS-Status and the error body tell key entitlement and network
                # position apart
                why = "HTTP 403: not in subscription (this channel; browser independent)"
                if els_status:
                    why += f"; X-ELS-Status {els_status[:40]}"
                return "exhaust", http, f"{why}; body: {diag_snippet(head)}"
            self.day.block(cap, "HTTP 403")
            return "defer", http, "HTTP 403"
        return "fail", http, f"HTTP {http}, not a PDF"

    def offline(self):
        try:
            self.s.head("https://doi.org/", timeout=15, allow_redirects=False)
            return False
        except self.requests.RequestException:
            return True

    def wait_for_network(self, why="network down"):
        """Flaky proxies and networks are normal: a dropout must pause
        and probe, not abort the run. Returns True when the network is back; False only
        after the patience budget (limits.network_retry_max_s, default 1800s) is spent."""
        max_s = int(self.lim.get("network_retry_max_s", 1800))
        gap = float(self.lim.get("network_retry_gap_s", 30))
        t0, n = time.time(), 0
        print(f"    ~ {why} (doi.org unreachable): waiting and auto-retrying "
              f"(patience {max_s // 60} min; Ctrl+C still stops)")
        while time.time() - t0 < max_s:
            self._nap(5 if n == 0 else gap)
            n += 1
            if not self.offline():
                print(f"    ~ network back after {int(time.time() - t0)}s; resuming")
                return True
        print(f"    ~ network still down after {max_s}s: honest stop, nothing recorded as failed")
        return False

    def accept_pdf(self, tmp, title, min_ratio=0.8):
        try:
            try:
                import pymupdf as fitz      # current PyMuPDF name
            except ImportError:
                import fitz                 # older PyMuPDF releases
            doc = fitz.open(str(tmp))
            n = doc.page_count
            text = "".join(doc[i].get_text("text") for i in range(min(3, n)))
            doc.close()
        except Exception:
            return False, "not a readable pdf"
        r = title_match_ratio(title, text)
        return (r >= min_ratio), f"{'%.2f' % r} title match, {n} pages"

    def try_pdf(self, uid, rec, step, source, url, cap, version, api_semantics=False, headers=None,
                min_ratio=0.8, note=""):
        res, http, x = self.download(url, self._script_cap(cap), headers=headers, api_semantics=api_semantics)
        if res in SKIP_RESULTS:
            return res                    # no network call happened: keep the ledger silent
        if res == "ok":
            ok, why = self.accept_pdf(x, rec["Title"], min_ratio)
            self.log(uid, step, source, url, cap, "ok" if ok else "rejected", http, why, version)
            if ok:
                dest = self.L.fulltext / f"{uid}.pdf"
                dest.write_bytes(x.read_bytes())
                x.unlink()
                self.set_status(uid, state="acquired", channel=step, source=source, version=version,
                                file=str(dest), version_note=note or None)
                return "ok"
            x.unlink(missing_ok=True)
            return "fail"
        self.log(uid, step, source, url, cap, res, http, x, version)
        # http is None <=> the transport-exception path in download() (the only
        # ("fail", None, ...) producer); keep that invariant true if download()
        # ever grows new return shapes.
        if ((res == "defer" and x in ("challenge page", "HTTP 403"))
                or (res == "fail" and http is None and not api_semantics)) \
                and not re.search(r"(api_key|apikey|token)=", url or ""):
            # a script-channel challenge/403 - or a transport failure such as
            # SSLError, where Python's TLS stack chokes on a host Edge fetches
            # fine - is exactly what the browser identity can often pass: keep the
            # URL as a browser fallback for this record.
            # Keyed URLs never enter status.json, and header-keyed API channels
            # (elsevier_api/wiley_tdm, api_semantics=True) are excluded outright:
            # the browser has no key, so routing them can only burn browser
            # budget on a guaranteed-empty fetch.
            fb = self.status.get(uid, {}).get("browser_fallback") or []
            if url not in fb:
                fb.append(url)
                self.set_status(uid, browser_fallback=fb)
        return res

    # -- waterfall steps ------------------------------------------------------
    def step_inhand(self, uid, rec):
        pdf = self.L.inhand / f"{uid}.pdf"
        if pdf.exists():
            dest = self.L.fulltext / f"{uid}.pdf"
            dest.write_bytes(pdf.read_bytes())
            self.set_status(uid, state="acquired", channel="inhand", source="inhand", version="inhand",
                            file=str(dest))
            return "ok"
        return "skip"

    def meta(self, uid, rec):
        """OpenAlex and Unpaywall records for the DOI, cached per paper. Only definitive
        answers are cached (200, or 404/410 for an unknown DOI); a 429, any other refusal
        (401/403: OpenAlex requires a key since 2026-02), a 5xx, a failed or a locally
        refused call leaves no cache, so the next run asks again, and the record returned
        now carries "_incomplete" so the OA steps defer instead of burning retry budget
        on missing candidates. Caches are stamped "_checked"; an unstamped empty cache
        for a DOI dates from the pre-0.4.9 rule that cached failed lookups, and is
        fetched once more (single lookups by DOI are free)."""
        p = self.L.meta / f"{uid}.json"
        doi = rec.get("DOI")
        if p.exists():
            try:
                cached = json.loads(p.read_text(encoding="utf-8"))
                if cached or not doi:
                    return cached
            except Exception:
                pass
        p.parent.mkdir(parents=True, exist_ok=True)
        m, complete = {}, True
        if doi:
            oa_q = {"mailto": self.email} if self.email else {}
            if self.key["OPENALEX_API_KEY"]:
                oa_q["api_key"] = self.key["OPENALEX_API_KEY"]
            lookups = (("openalex", "https://api.openalex.org/works/doi:" + urllib.parse.quote(doi), oa_q),
                       ("unpaywall", "https://api.unpaywall.org/v2/" + urllib.parse.quote(doi),
                        {"email": self.email or "anonymous@example.org"}))
            for name, url, params in lookups:
                r = self.api(url, params=params)
                if r is not None and r.status_code == 200:
                    try:
                        m[name] = r.json()
                    except ValueError:
                        complete = False
                elif r is None or r.status_code not in (404, 410):
                    complete = False
        if not complete:
            m["_incomplete"] = True
            return m
        m["_checked"] = time.strftime("%Y-%m-%d")
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return m

    def oa_locations(self, m):
        out = []
        up = m.get("unpaywall") or {}
        for loc in [up.get("best_oa_location")] + (up.get("oa_locations") or []):
            if loc and loc.get("url_for_pdf"):
                out.append({"url": loc["url_for_pdf"], "version": VERSION_MAP.get(loc.get("version") or "")})
        oa = m.get("openalex") or {}
        for loc in [oa.get("best_oa_location")] + (oa.get("locations") or []):
            if loc and loc.get("pdf_url"):
                out.append({"url": loc["pdf_url"], "version": VERSION_MAP.get(loc.get("version") or "")})
        seen, uniq = set(), []
        for c in out:
            if c["url"] not in seen:
                seen.add(c["url"])
                uniq.append(c)
        return uniq

    def step_oa_content(self, uid, rec):
        m = self.meta(uid, rec)
        attempted, skipped = False, bool(m.get("_incomplete"))
        for c in self.oa_locations(m):
            res = self.try_pdf(uid, rec, "oa_content", "oa", c["url"], cap_key(c["url"]),
                               c.get("version") or "published", min_ratio=0.8)
            if res == "ok":
                return "ok"
            if res in SKIP_RESULTS:
                skipped = True
                continue
            attempted = True
        # no real network attempt happened (every candidate fused/capped): defer, never
        # burn retry budget; an empty candidate list is deterministic and does burn
        return "fail" if (attempted or not skipped) else "defer"

    def step_oa_vor(self, uid, rec):
        m = self.meta(uid, rec)
        attempted, skipped = False, bool(m.get("_incomplete"))
        for c in [c for c in self.oa_locations(m) if c.get("version") == "published"]:
            res = self.try_pdf(uid, rec, "oa_vor", "oa_vor", c["url"], cap_key(c["url"]), "published")
            if res == "ok":
                return "ok"
            if res in SKIP_RESULTS:
                skipped = True
                continue
            attempted = True
        return "fail" if (attempted or not skipped) else "defer"

    def step_candidates(self, uid, rec):
        attempted, skipped = False, False
        for c in (self.candidates.get(uid) or []):
            res = self.try_pdf(uid, rec, "candidates", "candidate_map", c["url"], cap_key(c["url"]),
                               c.get("version") or "published", min_ratio=0.6)
            if res == "ok":
                return "ok"
            if res in SKIP_RESULTS:
                skipped = True
                continue
            attempted = True
        return "fail" if (attempted or not skipped) else "defer"

    def step_publisher(self, uid, rec):
        doi, pub = rec.get("DOI"), publisher_of_doi(rec.get("DOI"))
        if not doi or pub not in ("elsevier", "wiley", "springer", "sage"):
            return "skip"
        if pub == "elsevier" and self.key["ELSEVIER_API_KEY"]:
            url = "https://api.elsevier.com/content/article/doi/" + urllib.parse.quote(doi)
            hdr = {"X-ELS-APIKey": self.key["ELSEVIER_API_KEY"], "Accept": "application/pdf"}
            if self.key["ELSEVIER_INSTTOKEN"]:
                hdr["X-ELS-Insttoken"] = self.key["ELSEVIER_INSTTOKEN"]
            return self.try_pdf(uid, rec, "publisher", "elsevier_api", url, "elsevier_api", "published",
                                api_semantics=True, headers=hdr,
                                note="Publisher version via the Elsevier Article Retrieval API.")
        if pub == "wiley" and self.key["WILEY_TDM_TOKEN"]:
            url = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/" + urllib.parse.quote(doi, safe="")
            return self.try_pdf(uid, rec, "publisher", "wiley_tdm_api", url, "wiley_api", "published",
                                api_semantics=True, headers={"Wiley-TDM-Client-Token": self.key["WILEY_TDM_TOKEN"]},
                                note="Publisher version via the Wiley TDM API.")
        url = (f"https://link.springer.com/content/pdf/{urllib.parse.quote(doi)}.pdf" if pub == "springer"
               else f"https://journals.sagepub.com/doi/pdf/{urllib.parse.quote(doi)}?download=true")
        return self.try_pdf(uid, rec, "publisher", f"{pub}_direct", url, f"{pub}_direct", "published",
                            note=f"Publisher version downloaded directly from {pub}.")

    def _write_trace(self, uid, source, cap, res):
        """Append the browser's trace of this attempt (browser_channel v0.5.1) to the day's
        <date>.browser_trace.json next to the day book; a project that uploads its day
        books ships it with no extra step. A trace holds event
        classes and timings only: no URL, title, or host outside a fixed publisher list.
        Config browser_trace: false switches it off. Never raises."""
        if not self.cfg.get("browser_trace", True):
            return
        events = getattr(self.browser, "last_trace", None)
        if not isinstance(events, list):
            return
        try:
            path = self.L.day / f"{time.strftime('%Y-%m-%d')}.browser_trace.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            data.setdefault("attempts", []).append({
                "time": time.strftime("%H:%M:%S"), "uid": uid, "source": source,
                "cap": cap if family_of(cap) in PUBLISHERS else "other", "res": res, "events": events})
            path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        except Exception:
            pass

    def _browser_gap(self):
        lo, hi = self.lim.get("browser_gap_s", [25, 40])
        wait = (getattr(self, "last_browser", 0.0)) + random.uniform(float(lo), float(hi)) - time.time()
        if wait > 0:
            time.sleep(wait)

    def _browser_strike(self, cap, why):
        c = self.day.d["consec_blocked"].get(cap, 0) + 1
        self.day.d["consec_blocked"][cap] = c
        self.day.save()
        if c >= int(self.lim.get("browser_block_stop", 2)):
            self.day.block(cap, f"{why} (x{c} consecutive)")

    def step_browser(self, uid, rec):
        if not self.cfg.get("browser_enabled", True) or getattr(self.args, "no_browser", False):
            return "defer"
        doi = rec.get("DOI")
        if not doi:
            return "skip"
        cap = publisher_of_doi(doi) or cap_key("https://" + (doi.split("/")[0]))
        if cap in (self.cfg.get("browser_skip_publishers") or []):
            return "skip"
        if (self.cfg.get("publisher_entitlement") or {}).get(cap) == "no_sso_entitlement":
            return "skip"                # declared zero entitlement: fall through honestly
        if self.day.d.get("browser_total", 0) >= int(self.lim.get("browser_daily_total", 150)):
            return "defer"
        if self.browser is None:
            from .browser_channel import Browser
            self.browser = Browser(self.cfg, self.L.browser_profile)
        # (a) script-challenged repository URLs ride the browser identity first: a
        #     script challenge used to end the day for that host without the browser
        #     ever seeing the URL. A URL is consumed only
        #     when actually attempted; fused/capped days keep it for later.
        fb = list(self.status.get(uid, {}).get("browser_fallback") or [])
        remaining = []
        for url in fb:
            fcap = cap_key(url)
            if fcap in self._abandoned():
                remaining.append(url)     # abandoned source: never attempted, never consumed
                continue
            if self.day.d.get("browser_total", 0) >= int(self.lim.get("browser_daily_total", 150)):
                remaining.append(url)
                continue
            if self.day.blocked(fcap) or self.cap_full(fcap):
                remaining.append(url)
                continue
            self._browser_gap()
            self.day.add(fcap)
            self.day.d["browser_total"] = self.day.d.get("browser_total", 0) + 1
            self.day.save()
            tmp = self.L.tmp / f"bf_{int(time.time() * 1000)}.pdf"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            res, detail = self.browser.fetch_url(url, tmp)
            self.last_browser = time.time()
            self.log(uid, "browser", "browser_fallback", url, fcap, res, None, detail, "published")
            self._write_trace(uid, "browser_fallback", fcap, res)
            if res == "ok":
                ok, why = self.accept_pdf(tmp, rec["Title"], 0.6)
                if ok:
                    dest = self.L.fulltext / f"{uid}.pdf"
                    dest.write_bytes(tmp.read_bytes())
                    tmp.unlink()
                    self.set_status(uid, state="acquired", channel="browser_fallback",
                                    source="browser_fallback", version="published", file=str(dest),
                                    version_note="Repository/OA copy fetched by the browser "
                                                 "after the script channel hit a challenge page "
                                                 "or a transport failure (TLS).",
                                    browser_fallback=remaining)
                    return "ok"
                tmp.unlink(missing_ok=True)
            elif res == "blocked":
                self._browser_strike(fcap, f"fallback: {detail}")
        if fb != remaining or remaining:
            self.set_status(uid, browser_fallback=remaining)
        # (b) publisher landing + PDF attempt
        if self.day.blocked(cap) or self.cap_full(cap):
            return "defer"
        self._browser_gap()
        self.day.add(cap)
        self.day.d["browser_total"] = self.day.d.get("browser_total", 0) + 1
        self.day.save()
        tmp = self.L.tmp / f"br_{int(time.time() * 1000)}.pdf"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        res, detail, url = self.browser.fetch(doi, tmp)
        self.last_browser = time.time()
        self.log(uid, "browser", "browser", url or doi, cap, res, None, detail, "published")
        self._write_trace(uid, "browser", cap, res)
        if res == "blocked":
            self._browser_strike(cap, f"browser: {detail}")
            return "fail"                # challenge-class: retryable, publisher fuses at N-strike
        if res == "no_entitlement":
            # per-paper verdict (SSO works, this content is not in the subscription):
            # deterministic for THIS paper, never fuses the publisher
            self.set_status(uid, browser_verdict=f"no_entitlement: {detail}")
            return "exhaust"
        if res == "no_access":
            # definitive landing-page evidence (e.g. Emerald's "You do not currently
            # have access"): same per-paper terminal as no_entitlement -
            # never strikes, never fuses the publisher
            self.set_status(uid, browser_verdict=f"no_access: {detail}")
            return "exhaust"
        if res == "ok":
            self.day.d["consec_blocked"][cap] = 0
            self.day.save()
            ok, why = self.accept_pdf(tmp, rec["Title"], 0.8)
            if ok:
                dest = self.L.fulltext / f"{uid}.pdf"
                dest.write_bytes(tmp.read_bytes())
                tmp.unlink()
                self.set_status(uid, state="acquired", channel="browser", source="browser",
                                version="published", file=str(dest),
                                version_note="Publisher version downloaded in the browser (subscription).")
                return "ok"
        tmp.unlink(missing_ok=True)
        return "fail"

    def title_twins(self, uid, rec):
        """Same-title OpenAlex works whose OA locations may hold a copy (repos, WPs).
        Returns (candidates, skipped); skipped means the search was not answered today
        (OpenAlex budget spent or the API stopped for the day), not that none exist."""
        out = []
        q = {"mailto": self.email} if self.email else {}
        if self.key["OPENALEX_API_KEY"]:
            q["api_key"] = self.key["OPENALEX_API_KEY"]
        r = self.api("https://api.openalex.org/works", params={**q, "per_page": 10,
                                                              "search": re.sub(r"[^\w\s]", " ", rec["Title"])},
                     openalex="search")
        if r is not None and r.status_code == 200:
            for w in r.json().get("results", []):
                if norm_title(w.get("title")) != norm_title(rec["Title"]):
                    continue
                for loc in [w.get("best_oa_location")] + (w.get("locations") or []):
                    if loc and loc.get("pdf_url"):
                        out.append({"url": loc["pdf_url"], "version": VERSION_MAP.get(loc.get("version") or "") or "WP"})
        return out, (r is None and self.api_skipped)

    def step_wp(self, uid, rec):
        m = self.meta(uid, rec)
        oa = (m.get("openalex") or {})
        cands = [c for c in self.oa_locations({"openalex": oa}) if c.get("version") not in ("published", "accepted")]
        if ((oa.get("best_oa_location") or {}).get("pdf_url") or (oa.get("primary_location") or {}).get("pdf_url")) \
                and self.key["OPENALEX_API_KEY"] and not self.day.blocked("openalex_content"):
            wid = (oa.get("id") or "").rsplit("/", 1)[-1]
            if wid:
                cands.append({"url": f"https://content.openalex.org/works/{wid}.pdf?api_key={self.key['OPENALEX_API_KEY']}",
                              "version": None, "source": "openalex_content", "cap": "openalex_content"})
        twins, twins_skipped = self.title_twins(uid, rec)
        cands += twins
        attempted, skipped = False, bool(m.get("_incomplete")) or twins_skipped
        for c in cands:
            res = self.try_pdf(uid, rec, "wp", c.get("source") or "openalex_title_twin",
                               c["url"], c.get("cap") or cap_key(c["url"]),
                               c.get("version") or "other", min_ratio=0.6)
            if res == "ok":
                return "ok"
            if res in SKIP_RESULTS:
                skipped = True
                continue
            attempted = True
        return "fail" if (attempted or not skipped) else "defer"

    # -- driver ---------------------------------------------------------------
    def handler(self, step):
        return self.extra_steps.get(step) or getattr(self, "step_" + step)

    def close(self):
        if self.browser is not None:
            self.browser.close()
