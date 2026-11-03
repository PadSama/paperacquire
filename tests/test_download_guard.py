"""Browser._bounded_save, the stall guard around Download.save_as (v0.5.2).

save_as on an unfinished download blocks until the transfer ends; for a stalled server
that took 12-15 minutes on Windows (GitHub Actions), because the download
event arrives before Chrome creates the partial and 0.5.1 read the empty folder as
"finished". The guard therefore calls save_as only once Playwright's finished-download
file (<downloads_path>/<guid>) is there, or the partial has vanished (Chrome cancelled or
failed it), and cancels otherwise. Sizes come from os.stat: on Windows the directory
listing kept showing 30000 bytes for a partial that had grown to 570000 (same probe).

These tests drive the guard with a real temp folder that a fake page changes on each
wait, as Chrome would; the fake download records whether save_as or cancel ran.
"""
import time

import pytest

from paper_acquire.browser_channel import Browser

GUID = "a3ea03bc-8f3b-4009-9f1d-9030c86e7c02"
OLD = "c1a65aea-6f83-4e3b-80cd-c7e199f62566"


class FakeDownload:
    def __init__(self, folder):
        self.folder, self.saved, self.cancelled, self.deleted = folder, False, False, False

    def save_as(self, path):
        done = self.folder / GUID
        if not done.exists():
            raise RuntimeError("canceled")
        self.saved = True

    def cancel(self):
        self.cancelled = True

    def delete(self):
        (self.folder / GUID).unlink(missing_ok=True)
        self.deleted = True

    def path(self):
        return str(self.folder / GUID)


class FakePage:
    """Each wait advances the folder by one step of a scripted transfer."""
    def __init__(self, folder, steps):
        self.folder, self.steps, self.waits = folder, list(steps), 0

    def wait_for_timeout(self, ms):
        self.waits += 1
        time.sleep(0.02)
        if self.steps:
            self.steps.pop(0)(self.folder)


def write(name, size):
    return lambda f: (f / name).write_bytes(b"0" * size)


def rename(a, b):
    return lambda f: (f / a).rename(f / b)


def remove(name):
    return lambda f: (f / name).unlink()


def guard(tmp_path, steps, done=frozenset()):
    folder = tmp_path / "browser_downloads"
    folder.mkdir()
    b = Browser.__new__(Browser)
    b.downloads_dir, b._stall_s, b._download_max_s = folder, 0.3, 30
    b._dl_done = frozenset(done)
    b.page = FakePage(folder, steps)
    b._trace, b._trace_t0 = [], time.time()
    return b, FakeDownload(folder), folder


def test_finished_download_is_saved_at_once(tmp_path):
    b, d, folder = guard(tmp_path, [])
    (folder / GUID).write_bytes(b"%PDF-1.4")
    assert b._bounded_save(d, tmp_path / "out.part") is True
    assert d.saved and not d.cancelled and b.page.waits == 0
    assert d.deleted and not (folder / GUID).exists(), "v0.5.3: the saved copy is not kept"


def test_unsaved_download_is_not_deleted_by_the_guard(tmp_path):
    b, d, folder = guard(tmp_path, [])
    (folder / (GUID + ".crdownload")).write_bytes(b"0" * 10)
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert not d.deleted


def test_close_clears_leftover_downloads_only(tmp_path):
    b, _, folder = guard(tmp_path, [])
    for name in (GUID, OLD + ".crdownload"):
        (folder / name).write_bytes(b"x")
    (folder / "notes.txt").write_text("not Chrome's", encoding="utf-8")
    b._clear_downloads()
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt"]


def test_save_waits_until_the_transfer_has_finished(tmp_path):
    part = GUID + ".crdownload"
    b, d, _ = guard(tmp_path, [write(part, 100), write(part, 200), write(part, 300), rename(part, GUID)])
    (tmp_path / "browser_downloads" / part).write_bytes(b"0" * 10)
    assert b._bounded_save(d, tmp_path / "out.part") is True
    assert d.saved and not d.cancelled


def test_stalled_partial_is_cancelled_without_save(tmp_path):
    b, d, folder = guard(tmp_path, [])
    (folder / (GUID + ".crdownload")).write_bytes(b"%PDF-1.4" + b"0" * 2048)
    t0 = time.time()
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert d.cancelled and not d.saved and time.time() - t0 < 5
    assert any(e["ev"] == "download_stalled" and e["seen"] == "partial" for e in b._trace)


@pytest.mark.parametrize("name", [GUID + ".tmp", "Unconfirmed 123456.crdownload", ".org.chromium.Chromium.x1"])
def test_stall_is_caught_whatever_the_partial_is_named(tmp_path, name):
    """0.5.1 only recognised *.crdownload and went straight to save_as otherwise."""
    b, d, folder = guard(tmp_path, [])
    (folder / name).write_bytes(b"0" * 2048)
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert d.cancelled and not d.saved


def test_partial_created_after_the_download_event_is_waited_for(tmp_path):
    """The root cause on Windows (found with a probe in GitHub Actions): the download event
    arrives before Chrome has created the partial (about 0.5 s later). 0.5.1 read the
    empty folder as "finished" and blocked in save_as for the whole stalled transfer;
    the guard now waits, sees the partial stall, and cancels."""
    part = GUID + ".crdownload"
    b, d, _ = guard(tmp_path, [lambda f: None, write(part, 2057)])
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert d.cancelled and not d.saved


def test_late_partial_that_completes_is_saved(tmp_path):
    part = GUID + ".crdownload"
    b, d, _ = guard(tmp_path, [lambda f: None, write(part, 100), write(part, 200), rename(part, GUID)])
    assert b._bounded_save(d, tmp_path / "out.part") is True
    assert d.saved and not d.cancelled


def test_nothing_written_is_a_stall_not_a_blocking_save(tmp_path):
    b, d, _ = guard(tmp_path, [])
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert d.cancelled and not d.saved
    assert any(e["ev"] == "download_stalled" and e["seen"] == "nothing" for e in b._trace)


def test_vanished_partial_ends_the_wait_without_cancel(tmp_path):
    """Chrome removed the partial itself (failed or cancelled transfer): save_as returns
    at once (it raises here), no stall wait and no cancel."""
    part = GUID + ".crdownload"
    b, d, folder = guard(tmp_path, [remove(part)])
    (folder / part).write_bytes(b"0" * 10)
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert not d.cancelled and b.page.waits == 1


def test_finished_file_of_an_earlier_attempt_is_not_taken(tmp_path):
    b, d, folder = guard(tmp_path, [], done={OLD})
    (folder / OLD).write_bytes(b"%PDF-1.4 old")
    (folder / (GUID + ".crdownload")).write_bytes(b"0" * 10)
    assert b._bounded_save(d, tmp_path / "out.part") is False
    assert d.cancelled and not d.saved


def test_progress_resets_the_stall_clock(tmp_path):
    """A slow transfer that keeps growing is never cancelled; it is saved when done."""
    part = GUID + ".crdownload"
    grow = [write(part, 100 * (i + 1)) for i in range(40)]      # ~0.8 s of growth > _stall_s
    b, d, folder = guard(tmp_path, grow + [rename(part, GUID)])
    (folder / part).write_bytes(b"0")
    assert b._bounded_save(d, tmp_path / "out.part") is True
    assert d.saved and not d.cancelled
