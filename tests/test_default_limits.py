"""Default limits and stop rules: the engine is polite by default.

These tests pin the engine's built-in limits and the behaviour that enforces them: one
daily count per publisher family across the browser, API and direct channels, the
OpenAlex spend meter, the metadata 429 rule, the consecutive-403 stop, refusal and
challenge stops, and the randomized gaps. Each behavioural test runs against both limit
sources: the engine's own defaults (an empty limits block) and the example config.

A change to a default must be deliberate: update the expected values here, in
config.example.json and in the README in the same commit. Everything runs offline in
tmp_path: the HTTP session and the browser are stubs.
"""
import json
from pathlib import Path

import pytest

from paper_acquire import engine
from stubs import PDF, StubBrowser, StubSession, make_acquirer

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_LIMITS = {
    "per_publisher_daily": 50,
    "browser_daily_total": 150,
    "download_gap_s": [15, 30],
    "browser_gap_s": [25, 40],
    "api_gap_s": 1.5,
    "openalex_daily_usd": 0.95,
    "consecutive_403_stop": 5,
}
ENGINE_WATERFALL = ["inhand", "oa_content", "oa_vor", "publisher", "browser", "wp"]
OPENALEX_USD_PER_PDF = 0.01      # OpenAlex pricing: a content PDF costs $0.01
OPENALEX_USD_PER_SEARCH = 0.001  # OpenAlex pricing: a keyword search costs $0.001
OPENALEX_CONTENT_DAILY_CAP = 90  # Acquirer.cap_limit: count ceiling kept under the $0.95/day budget
BROWSER_BLOCK_STOP = 2           # default: 2 consecutive unresolved challenges stop a publisher
METADATA_429_WAIT_S = (60, 90)   # after a metadata 429: wait 60-90 s and retry once
SPRINGER_PDF = "https://link.springer.com/content/pdf/10.1007/{}.pdf"
SAGE_PDF = "https://journals.sagepub.com/doi/pdf/10.1177/{}?download=true"
SD_PDF = "https://www.sciencedirect.com/science/article/pii/S0000000000000000/pdfft"
ELSEVIER_API = "https://api.elsevier.com/content/article/doi/10.1016/x"
OPENALEX_DOI = "https://api.openalex.org/works/doi:10.1016/x"
TANDF_RECORD = {"uid": "G1", "DOI": "10.1080/0000", "Title": "offline stub"}
ELSEVIER_RECORD = {"uid": "G2", "DOI": "10.1016/j.x.2021.000001", "Title": "offline stub"}


def example_config_limits():
    return json.loads((REPO_ROOT / "paper_acquire" / "config.example.json").read_text(encoding="utf-8"))["limits"]


# -- configured values --------------------------------------------------------

def test_example_config_limits_are_the_defaults():
    limits = example_config_limits()
    assert limits and set(limits) <= set(DEFAULT_LIMITS), sorted(limits)
    assert limits == {k: DEFAULT_LIMITS[k] for k in limits}


def test_waterfall_order():
    assert engine.DEFAULT_STEPS == ENGINE_WATERFALL


def test_openalex_prices():
    assert engine.OPENALEX_USD == {"search": OPENALEX_USD_PER_SEARCH, "content": OPENALEX_USD_PER_PDF}


# -- enforcement --------------------------------------------------------------

@pytest.fixture(params=["example_config", "engine_defaults"])
def limits(request):
    """The example config's limits block, or an empty block so that the engine's
    built-in defaults apply."""
    if request.param == "example_config":
        return example_config_limits()
    return {}


@pytest.fixture
def acquirer(tmp_path, monkeypatch, limits):
    return make_acquirer(tmp_path, monkeypatch, limits)


def test_per_publisher_daily_cap(acquirer):
    cap = "springer_direct"
    assert acquirer.cap_limit(cap) == DEFAULT_LIMITS["per_publisher_daily"]
    acquirer.day.d["count"][cap] = DEFAULT_LIMITS["per_publisher_daily"] - 1
    assert acquirer.download(SPRINGER_PDF.format("a"), cap)[0] == "ok"
    assert acquirer.day.used(cap) == DEFAULT_LIMITS["per_publisher_daily"]
    calls = acquirer.s.calls
    assert acquirer.download(SPRINGER_PDF.format("b"), cap)[0] == "skip_cap"
    assert acquirer.s.calls == calls, "a publisher at its daily cap must not reach the network"


def test_daily_cap_spans_all_channels_of_a_publisher(acquirer):
    """Browser, API and direct requests to one publisher share one daily count;
    stop keys stay per channel, and other families keep their own counts."""
    acquirer.day.d["count"].update({"elsevier": 45, "elsevier_api": 4})
    acquirer.s = StubSession(403, b"<html>not in subscription</html>")
    assert acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)[0] == "exhaust"  # the 50th
    calls = acquirer.s.calls
    assert acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)[0] == "skip_cap"
    assert acquirer.download(SD_PDF, "elsevier_direct")[0] == "skip_cap"
    acquirer.browser = StubBrowser()
    assert acquirer.step_browser("G2", ELSEVIER_RECORD) == "defer"
    assert acquirer.browser.calls == 0 and acquirer.s.calls == calls
    assert not acquirer.day.blocked("elsevier"), "API refusals must never stop the browser key"
    assert not acquirer.cap_full("springer_direct") and not acquirer.cap_full("repo.example.org")


def test_openalex_content_cap_keeps_the_daily_budget(acquirer):
    cap = acquirer.cap_limit("openalex_content")
    assert cap == OPENALEX_CONTENT_DAILY_CAP
    assert cap * OPENALEX_USD_PER_PDF <= DEFAULT_LIMITS["openalex_daily_usd"]


def test_openalex_spend_is_metered(acquirer):
    """Every paid OpenAlex call is charged before it is sent, and a call that
    would take the day past the budget is refused without a request."""
    budget = DEFAULT_LIMITS["openalex_daily_usd"]
    acquirer.day.d["openalex_usd"] = budget - 0.005
    calls = acquirer.s.calls
    assert acquirer.download("https://content.openalex.org/works/W1.pdf", "openalex_content")[0] == "skip_cap"
    assert acquirer.s.calls == calls
    acquirer.s = StubSession(200, b'{"results": []}')
    twins, skipped = acquirer.title_twins("G3", {"uid": "G3", "DOI": "10.1/x", "Title": "t"})
    assert twins == [] and not skipped and acquirer.s.calls == 1
    assert acquirer.day.d["openalex_usd"] == pytest.approx(budget - 0.005 + OPENALEX_USD_PER_SEARCH)
    acquirer.day.d["openalex_usd"] = budget
    twins, skipped = acquirer.title_twins("G3", {"uid": "G3", "DOI": "10.1/x", "Title": "t"})
    assert skipped and acquirer.s.calls == 1, "a search past the budget must not be sent"


def test_openalex_content_download_is_charged(acquirer):
    acquirer.download("https://content.openalex.org/works/W2.pdf", "openalex_content")
    assert acquirer.day.d["openalex_usd"] == pytest.approx(OPENALEX_USD_PER_PDF)


def test_metadata_429_waits_retries_once_then_stops_the_api(acquirer):
    """A 429 waits 60-90 s and retries once; a second 429 stops that host's
    metadata calls for the day, and other hosts carry on."""
    acquirer.s = StubSession(429, b"")
    assert acquirer.api(OPENALEX_DOI) is None
    assert acquirer.s.calls == 2 and METADATA_429_WAIT_S in acquirer.gaps
    assert acquirer.day.blocked("api:api.openalex.org") and acquirer.api_skipped
    assert acquirer.api(OPENALEX_DOI) is None and acquirer.s.calls == 2
    acquirer.s = StubSession(200, b"{}")
    assert acquirer.api("https://api.unpaywall.org/v2/10.1016/x").status_code == 200


def test_metadata_429_then_answer(acquirer):
    acquirer.s = StubSession(script=[(429, b""), (200, b"{}")])
    assert acquirer.api(OPENALEX_DOI).status_code == 200
    assert not acquirer.day.blocked("api:api.openalex.org")


def test_download_gap(acquirer):
    for name in ("a", "b"):
        acquirer.download(SPRINGER_PDF.format(name), "springer_direct")
    assert acquirer.gaps == [tuple(DEFAULT_LIMITS["download_gap_s"])] * 2


def test_tdm_api_stops_after_five_consecutive_403(acquirer):
    acquirer.s = StubSession(403, b"<html>not in subscription</html>")
    stop = DEFAULT_LIMITS["consecutive_403_stop"]
    for _ in range(stop - 1):
        assert acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)[0] == "exhaust"
    assert not acquirer.day.blocked("elsevier_api")
    assert acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)[0] == "exhaust"
    assert acquirer.day.blocked("elsevier_api")
    assert f"{stop} consecutive 403" in acquirer.day.d["blocked"]["elsevier_api"]


def test_tdm_api_403_streak_resets_on_success(acquirer):
    stop = DEFAULT_LIMITS["consecutive_403_stop"]
    acquirer.s = StubSession(403, b"denied")
    for _ in range(stop - 1):
        acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)
    acquirer.s.status, acquirer.s.body = 200, PDF
    assert acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)[0] == "ok"
    acquirer.s.status, acquirer.s.body = 403, b"denied"
    for _ in range(stop - 1):
        acquirer.download(ELSEVIER_API, "elsevier_api", api_semantics=True)
    assert not acquirer.day.blocked("elsevier_api"), "the fuse counts consecutive refusals only"


@pytest.mark.parametrize("status, body, reason", [
    (429, b"too many requests", "HTTP 429"),
    (200, b"<html><title>Just a moment...</title></html>", "challenge page"),
    (403, b"<html>forbidden</html>", "HTTP 403"),
])
def test_script_refusal_stops_the_publisher_for_the_day(acquirer, status, body, reason):
    """A 429, a challenge page or a non-TDM 403 stops that publisher for the day."""
    acquirer.s = StubSession(status, body)
    assert acquirer.download(SAGE_PDF.format("a"), "sage_direct")[0] == "defer"
    assert acquirer.day.blocked("sage_direct") and reason in acquirer.day.d["blocked"]["sage_direct"]
    calls = acquirer.s.calls
    assert acquirer.download(SAGE_PDF.format("b"), "sage_direct")[0] == "skip_fuse"
    assert acquirer.s.calls == calls, "a publisher stopped for the day must not be contacted again"


def test_browser_daily_total(acquirer):
    acquirer.browser = StubBrowser()
    acquirer.day.d["browser_total"] = DEFAULT_LIMITS["browser_daily_total"]
    assert acquirer.step_browser("G1", TANDF_RECORD) == "defer"
    assert acquirer.browser.calls == 0, "the browser must stay idle once the daily total is reached"
    acquirer.day.d["browser_total"] = DEFAULT_LIMITS["browser_daily_total"] - 1
    acquirer.step_browser("G1", TANDF_RECORD)
    assert acquirer.browser.calls == 1
    assert acquirer.day.d["browser_total"] == DEFAULT_LIMITS["browser_daily_total"]


def test_browser_gap_and_challenge_stop(acquirer):
    acquirer.browser = StubBrowser(("blocked", "landing is a challenge page", None))
    for strike in range(1, BROWSER_BLOCK_STOP + 1):
        assert acquirer.step_browser("G1", TANDF_RECORD) == "fail"
        assert acquirer.day.blocked("tandf") == (strike == BROWSER_BLOCK_STOP)
    assert acquirer.gaps == [tuple(DEFAULT_LIMITS["browser_gap_s"])] * BROWSER_BLOCK_STOP
