"""Replica of publisher behaviour seen in real runs, served entirely by ctx.route - no
real site is touched.

Every hop is an in-page navigation, never an HTTP redirect: Playwright does not intercept
the request that follows a redirect it fulfilled, which would leak to the real site. The
test browser also gets a dead proxy (DEAD_PROXY), so anything not answered here fails.

ScienceDirect model (scenario encoded in the DOI suffix):
- doi.org answers like linkinghub: a "Redirecting" page whose JS hops to the article after 2 s
- article page renders View PDF client-side after `delay` ms (target=_blank pdfft link)
- article pages embed the View PDF link's parts as JSON (article.pdfDownload.urlMetadata,
  Zotero ScienceDirect.js step 5), unless `no_json`
- pdfft relay: an HTML page that JS-redirects to the signed assets URL - for any browser
  navigation (Zotero and other real-browser tools load pdfft directly). Scenario R keeps
  the pessimistic model: only a click (article Referer) gets the relay, a direct
  navigation gets an empty 200. The ?isDTMRedir= form gets ScienceDirect's 403 block
  page (dead since 2024-10, Zotero translators #3367)
- assets URL serves the PDF inline (application/pdf)
Generic publisher model (pub.example): citation_pdf_url meta -> relay page that
JS-redirects after `relay` ms to the PDF (no referer rule) - a late PDF after a relay."""
import json
import re

DEAD_PROXY = "http://127.0.0.1:9"   # nothing listens: a request the replica misses cannot leave

PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]"
       b"/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
       b"trailer<</Root 1 0 R>>\n%%EOF\n")

SCENARIOS = {
    # name: (description, options)
    "A": ("normal entitled load", dict(delay=1500)),
    "B": ("control renders late (8 s)", dict(delay=8000)),
    "C": ("control missing on first load, present on reload", dict(delay=1500, miss_first=True)),
    "D": ("institution activation click needed on the article page", dict(delay=1500, activation=True)),
    "E": ("generic publisher, PDF arrives 2 s after a JS relay", dict(generic=True, relay=2000)),
    "G": ("control rendered via requestAnimationFrame (hidden tab never renders)", dict(delay=1500, raf=True)),
    "N": ("generic publisher landing that says no access (no control)", dict(generic=True, no_access=True)),
    "J": ("View PDF never renders, page JSON present", dict(delay=1500, never_ctl=True)),
    "P": ("View PDF never renders, no page JSON (PII link only)", dict(delay=1500, never_ctl=True, no_json=True)),
    "R": ("relay answers clicks only (pessimistic model)", dict(delay=1500, strict_relay=True)),
    "K": ("block page instead of the article", dict(block=True)),
    "S": ("the PDF download stalls mid-transfer (served from 127.0.0.1)", dict(delay=1000, stall=True)),
}

BLOCK_HTML = ("<!doctype html><html><head><title>ScienceDirect</title></head><body>"
              "<h1>There was a problem providing the content you requested</h1>"
              "<p>Please contact our support team for more information.</p>"
              "<!-- ::CLOUDFLARE_ERROR_1000S_BOX:: --></body></html>")


def pii_of(s, n=0):
    """Distinct PII per scenario letter and instance number (several papers of one kind)."""
    return "S" + str(1000000000000 + ord(s) + 1000 * int(n or 0)).zfill(16)


def scenario_of(pii):
    return chr((int(pii[1:]) - 1000000000000) % 1000)


class Replica:
    def __init__(self, stall_url=None):
        self.loads = {}
        self.log = []
        self.stall_url = stall_url      # a local server that stalls mid-PDF (scenario S)

    def doi(self, s, n=0):
        if SCENARIOS[s][1].get("generic"):
            return f"10.9999/mock.{s}" + (f".{n}" if n else "")
        return f"10.1016/j.mock.2026.{s}" + (f".{n}" if n else "")

    def __call__(self, route):
        req = route.request
        u = req.url
        self.log.append(u[:110])
        if u.startswith("http://127.0.0.1"):     # the test's own local servers only
            return route.continue_()
        m = re.match(r"https://doi\.org/10\.1016/j\.mock\.2026\.(\w)(?:\.(\d+))?$", u)
        if m:   # the state page.goto returns on in real runs: linkinghub's "Redirecting" page
            target = f"https://www.sciencedirect.com/science/article/pii/{pii_of(m.group(1), m.group(2))}?via%3Dihub"
            return self._html(route, "Redirecting", f"<p>Redirecting...</p><script>setTimeout(function(){{location.replace({json.dumps(target)})}},2000)</script>")
        m = re.match(r"https://doi\.org/10\.9999/mock\.(\w)(?:\.\d+)?$", u)
        if m:   # generic publisher: the DOI resolves straight to the article
            return self._article(route, m.group(1))
        m = re.match(r"https://www\.sciencedirect\.com/science/article/pii/(S\d+)/pdfft", u)
        if m:
            if "isDTMRedir" in u:
                return route.fulfill(status=403, body=BLOCK_HTML.encode(), headers={"content-type": "text/html"})
            ref = req.headers.get("referer", "")
            strict = SCENARIOS[scenario_of(m.group(1))][1].get("strict_relay")
            if not strict or ("/science/article/pii/" in ref and "pdfft" not in ref):
                asset = f"https://pdf.sciencedirectassets.com/271/1-s2.0-{m.group(1)}-main.pdf?X-Amz-Security-Token=x"
                return self._html(route, "", f"<script>setTimeout(function(){{location.replace({json.dumps(asset)})}},300)</script>")
            return route.fulfill(status=200, body=b"", headers={"content-type": "text/html"})
        m = re.match(r"https://www\.sciencedirect\.com/science/article/pii/(S\d+)", u)
        if m:
            pii = m.group(1)
            s = scenario_of(pii)
            opt = SCENARIOS[s][1]
            if opt.get("block"):
                return route.fulfill(status=403, body=BLOCK_HTML.encode(), headers={"content-type": "text/html"})
            n = self.loads[pii] = self.loads.get(pii, 0) + 1
            href = (self.stall_url if opt.get("stall") and self.stall_url
                    else f"/science/article/pii/{pii}/pdfft?md5=abc&amp;pid=1-s2.0-{pii}-main.pdf")
            ctl = f'<a class="link-button" target="_blank" href="{href}"><span>View PDF</span></a>'
            show = not (opt.get("miss_first") and n == 1) and not opt.get("never_ctl")
            if opt.get("activation"):
                js = ("if(document.cookie.indexOf('inst=1')<0){document.getElementById('bar').innerHTML="
                      "'<button onclick=\"document.cookie=\\'inst=1; path=/\\';location.reload()\">Access through your organization</button>';}"
                      f"else{{setTimeout(function(){{document.getElementById('bar').innerHTML={json.dumps(ctl)}}},{opt['delay']});}}")
            elif not show:
                js = ""
            elif opt.get("raf"):
                js = (f"setTimeout(function(){{requestAnimationFrame(function(){{document.getElementById('bar').innerHTML={json.dumps(ctl)}}})}},{opt['delay']});")
            else:
                js = f"setTimeout(function(){{document.getElementById('bar').innerHTML={json.dumps(ctl)}}},{opt['delay']});"
            state = "" if opt.get("no_json") else (
                '<script type="application/json">' + json.dumps({"article": {"pdfDownload": {"urlMetadata": {
                    "path": "science/article/pii", "pdfExtension": "/pdfft", "pii": pii,
                    "queryParams": {"md5": "abc", "pid": f"1-s2.0-{pii}-main.pdf"}}}}}) + "</script>")
            body = f"<h1>Mock article</h1><p>{'text ' * 900}</p><div id='bar'></div>{state}<script>{js}</script>"
            return self._html(route, f"Mock article {s} - ScienceDirect", body)
        m = re.match(r"https://pdf\.sciencedirectassets\.com/", u)
        if m:
            return route.fulfill(status=200, body=PDF, headers={"content-type": "application/pdf", "content-disposition": "inline"})
        m = re.match(r"https://pub\.example/article/(\w)", u)
        if m:
            return self._article(route, m.group(1))
        m = re.match(r"https://pub\.example/pdf/(\w)", u)
        if m:
            relay = SCENARIOS[m.group(1)][1].get("relay", 0)
            return self._html(route, "", f"<p>Loading</p><script>setTimeout(function(){{location.replace('https://pub.example/file/{m.group(1)}.pdf')}},{relay})</script>")
        if u.startswith("https://pub.example/file/"):
            return route.fulfill(status=200, body=PDF, headers={"content-type": "application/pdf"})
        return route.fulfill(status=404, body=b"not in replica", headers={"content-type": "text/plain"})

    def _article(self, route, s):
        meta = f'<meta name="citation_pdf_url" content="https://pub.example/pdf/{s}">'
        note = "<p>You do not currently have access to this content.</p>" if SCENARIOS[s][1].get("no_access") else ""
        return self._html(route, f"Mock {s} - Pub", f"{note}<p>{'text ' * 900}</p>", head=meta)

    @staticmethod
    def _html(route, title, body, head=""):
        html = f"<!doctype html><html><head><title>{title}</title>{head}</head><body>{body}</body></html>"
        return route.fulfill(status=200, body=html.encode(), headers={"content-type": "text/html"})
