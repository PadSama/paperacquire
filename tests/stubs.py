"""Offline stand-ins shared by the test modules: an HTTP session, its responses, a browser.

Nothing here touches the network; an engine built with make_acquirer() works in a
pytest tmp_path and never sleeps.
"""
import json
from types import SimpleNamespace

from paper_acquire import engine

PDF = b"%PDF-1.4\n% offline stub\n"


class StubResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        yield self.body

    def json(self):
        return json.loads(self.body or b"{}")


class StubSession:
    """Stands in for requests.Session. Every GET answers (status, body), or the next
    entry of `script` while one is left; calls are counted."""

    def __init__(self, status=200, body=PDF, script=None, headers=None):
        self.status, self.body, self.calls, self.headers = status, body, 0, headers or {}
        self.script = list(script or [])

    def get(self, url, **kwargs):
        self.calls += 1
        if self.script:
            return StubResponse(*self.script.pop(0))
        return StubResponse(self.status, self.body, self.headers)

    def head(self, url, **kwargs):
        raise AssertionError("an offline test reached the network probe")


class StubBrowser:
    def __init__(self, result=("fail", "stub landing", None)):
        self.result, self.calls = result, 0

    def fetch(self, doi, dest):
        self.calls += 1
        return self.result

    def fetch_url(self, url, dest):
        self.calls += 1
        return self.result[0], self.result[1]

    def close(self):
        pass


def make_acquirer(tmp_path, monkeypatch, limits=None):
    """An engine on a fresh tmp project with a stub session. Randomized gaps are
    recorded in acq.gaps and never waited for; time.sleep is a no-op."""
    gaps = []
    monkeypatch.setattr(engine.random, "uniform", lambda lo, hi: gaps.append((lo, hi)) or 0.0)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    acq = engine.Acquirer(tmp_path / "project", {"limits": dict(limits or {}), "browser_enabled": True},
                          SimpleNamespace(no_browser=False), steps=["browser"])
    acq.s = StubSession()
    acq.gaps = gaps
    return acq
