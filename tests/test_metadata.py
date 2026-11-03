"""Metadata cache and OA-step deferral (paper_acquire 0.4.9).

A metadata lookup that got no definitive answer (429, other refusal, 5xx, transport
error, budget or day stop) must not be cached, and the OA steps must defer rather than
burn retry budget on candidates they never saw. Offline: stub session in tmp_path.
"""
import json

from stubs import StubSession, make_acquirer

RECORD = {"uid": "M1", "DOI": "10.1016/j.x.2021.000001", "Title": "offline stub"}


def test_failed_lookup_is_not_cached(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    for status in (503, 403, 429):
        acq.s = StubSession(status, b"")
        m = acq.meta("M1", RECORD)
        assert m.get("_incomplete"), status
        assert not (acq.L.meta / "M1.json").exists(), status


def test_definitive_answers_are_cached(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.s = StubSession(script=[(200, b'{"id": "W1"}'), (404, b"")])
    m = acq.meta("M1", RECORD)
    assert m["openalex"] == {"id": "W1"} and "unpaywall" not in m and not m.get("_incomplete")
    cached = json.loads((acq.L.meta / "M1.json").read_text(encoding="utf-8"))
    assert cached["openalex"] == {"id": "W1"} and cached["_checked"]
    acq.meta("M1", RECORD)
    assert acq.s.calls == 2, "a stamped cache is served without new lookups"


def test_stale_empty_cache_is_fetched_once(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.L.meta.mkdir(parents=True, exist_ok=True)
    (acq.L.meta / "M1.json").write_text("{}", encoding="utf-8")   # written by the pre-0.4.9 rule
    acq.s = StubSession(404, b"")
    m = acq.meta("M1", RECORD)
    assert acq.s.calls == 2 and m.get("_checked") and not m.get("_incomplete")
    acq.meta("M1", RECORD)
    assert acq.s.calls == 2


def test_oa_steps_defer_on_incomplete_metadata(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.s = StubSession(503, b"")
    assert acq.step_oa_content("M1", RECORD) == "defer"
    assert acq.step_oa_vor("M1", RECORD) == "defer"


def test_oa_step_fails_on_a_definitive_empty_answer(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.s = StubSession(404, b"")
    assert acq.step_oa_content("M1", RECORD) == "fail"


def test_wp_step_defers_when_the_search_is_refused(tmp_path, monkeypatch):
    acq = make_acquirer(tmp_path, monkeypatch)
    acq.s = StubSession(404, b"")
    acq.meta("M1", RECORD)                                     # definitive and empty: cached
    acq.day.d["openalex_usd"] = 0.95                           # budget spent: the search is refused
    calls = acq.s.calls
    assert acq.step_wp("M1", RECORD) == "defer"
    assert acq.s.calls == calls
