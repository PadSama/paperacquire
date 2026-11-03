"""Engine diagnostics (0.5.1): API 403 details and per-attempt browser traces.

Offline: stub session and stub browser in tmp_path.
"""
import json

from stubs import StubBrowser, StubSession, make_acquirer

ELSEVIER_API = "https://api.elsevier.com/content/article/doi/10.1016/x"
ELSEVIER_RECORD = {"uid": "G2", "DOI": "10.1016/j.x.2021.000001", "Title": "offline stub"}
TRACE = [{"t": 0.0, "ev": "start", "kind": "fetch"}, {"t": 3.1, "ev": "end", "res": "fail"}]


def test_api_403_detail_names_els_status_and_scrubbed_body(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    secret = "0123456789abcdef0123456789abcdef"
    acq.s = StubSession(403, ('{"service-error":{"status":{"statusCode":"AUTHORIZATION_ERROR",'
                              '"statusText":"not entitled"}}} ref ' + secret).encode(),
                        headers={"X-ELS-Status": "AUTHORIZATION_ERROR"})
    res, http, detail = acq.download(ELSEVIER_API, "elsevier_api", api_semantics=True)
    assert res == "exhaust" and http == 403
    assert "X-ELS-Status AUTHORIZATION_ERROR" in detail and "statusText" in detail
    assert secret not in detail, "a token-like run reached the ledger"


def _traces(acq):
    files = list(acq.L.day.glob("*.browser_trace.json"))
    return json.loads(files[0].read_text(encoding="utf-8"))["attempts"] if files else []


def test_browser_attempt_trace_lands_next_to_the_day_book(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.browser = StubBrowser(("fail", "stub", None))
    acq.browser.last_trace = TRACE
    acq.step_browser("G2", ELSEVIER_RECORD)
    (a,) = _traces(acq)
    assert (a["uid"], a["source"], a["cap"], a["res"]) == ("G2", "browser", "elsevier", "fail")
    assert a["events"] == TRACE
    assert acq.day.path.exists(), "the day book itself must stay where it was"


def test_fallback_trace_masks_repository_hosts(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.browser = StubBrowser(("fail", "stub", None))
    acq.browser.last_trace = TRACE
    acq.status["G2"] = {"state": "pending", "browser_fallback": ["https://orca.cardiff.ac.uk/x.pdf"]}
    acq.day.block("elsevier", "test")          # part (b) defers; only the fallback runs
    acq.step_browser("G2", ELSEVIER_RECORD)
    (a,) = _traces(acq)
    assert a["source"] == "browser_fallback" and a["cap"] == "other"


def test_trace_switch_off(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.cfg["browser_trace"] = False
    acq.browser = StubBrowser(("fail", "stub", None))
    acq.browser.last_trace = TRACE
    acq.step_browser("G2", ELSEVIER_RECORD)
    assert _traces(acq) == []


def test_keys_in_error_texts_never_reach_the_attempts_log(tmp_path, monkeypatch):
    """Error texts from HTTP libraries quote the failing URL, query string included; the
    attempts log redacts key-like values in the detail field as it does in the URL."""
    acq = make_acquirer(tmp_path, monkeypatch)
    secret = "SECRET0123456789"
    url = f"https://content.openalex.org/works/W1.pdf?api_key={secret}"
    acq.log("G2", "wp", "openalex_content", url, "openalex_content", "fail", None,
            f"ConnectionError: Max retries exceeded with url: /works/W1.pdf?api_key={secret}&mailto=a@b.c")
    line = acq.L.attempts.read_text(encoding="utf-8")
    assert secret not in line and "a@b.c" not in line, line
    assert "api_key=***" in line
