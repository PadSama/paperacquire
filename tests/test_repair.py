"""The reopen repair (paper_acquire.repair): selection, dry run, backup, isolation.

Synthetic status files in tmp_path only; no real project state is touched.
"""
import json

from paper_acquire import __main__ as cli
from paper_acquire import repair

STATUS = {
    "E-pend": {"state": "pending", "next_step": "publisher", "steps_done": [],
               "step_state": {"browser": {"status": "open", "tries": 2}}},
    "E-nf": {"state": "not_found", "next_step": None, "steps_done": ["browser", "wp"],
             "step_state": {"browser": {"status": "exhausted", "tries": 3}, "wp": {"status": "exhausted", "tries": 3}},
             "reopened": "r3-20261030 earlier repair"},
    "E-verdict": {"state": "pending", "browser_verdict": "no_access: landing says no access",
                  "step_state": {"browser": {"status": "exhausted", "tries": 1}}},
    "E-fresh": {"state": "pending", "step_state": {}},
    "E-done": {"state": "acquired", "channel": "browser", "step_state": {"browser": {"status": "ok", "tries": 1}}},
    "W-pend": {"state": "pending", "step_state": {"browser": {"status": "open", "tries": 2}}},
}
RECORDS = [{"uid": u, "DOI": ("10.1111/x" if u.startswith("W") else "10.1016/x")} for u in STATUS]


def _files(tmp_path):
    status = tmp_path / "status.json"
    status.write_text(json.dumps(STATUS, indent=1), encoding="utf-8")
    records = tmp_path / "records.jsonl"
    records.write_text("\n".join(json.dumps(r) for r in RECORDS), encoding="utf-8")
    return status, records


def test_selection_rules():
    plan = repair.plan_reopen(STATUS, "browser")
    assert plan == ["E-pend", "E-nf", "W-pend"]   # no verdict, no fresh, no acquired


def test_doi_prefix_and_uid_filters(tmp_path):
    _, records = _files(tmp_path)
    dois = repair.load_dois(records)
    assert repair.plan_reopen(STATUS, "browser", dois=dois, doi_prefix="10.1016/") == ["E-pend", "E-nf"]
    assert repair.plan_reopen(STATUS, "browser", uids={"E-nf"}) == ["E-nf"]


def test_dry_run_writes_nothing(tmp_path):
    status, records = _files(tmp_path)
    before = status.read_bytes()
    plan = repair.run(status, records=records, doi_prefix="10.1016/", out=lambda *_: None)
    assert plan == ["E-pend", "E-nf"]
    assert status.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_apply_resets_only_the_plan_and_keeps_a_backup(tmp_path):
    status, records = _files(tmp_path)
    before = status.read_bytes()
    repair.run(status, records=records, doi_prefix="10.1016/", note="r5 test", apply=True, out=lambda *_: None)
    after = json.loads(status.read_text(encoding="utf-8"))
    assert after["E-pend"]["step_state"]["browser"] == {"status": "open", "tries": 0}
    assert after["E-nf"]["state"] == "pending" and after["E-nf"]["next_step"] == "browser"
    assert after["E-nf"]["steps_done"] == ["wp"]
    assert after["E-nf"]["reopened"] == "r3-20261030 earlier repair; r5 test"
    assert after["E-nf"]["step_state"]["wp"] == {"status": "exhausted", "tries": 3}
    for uid in ("E-verdict", "E-fresh", "E-done", "W-pend"):
        assert after[uid] == STATUS[uid], uid
    backups = list(tmp_path.glob("status.backup-reopen-browser-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before


def test_backup_never_overwrites(tmp_path):
    status, _ = _files(tmp_path)
    first = repair.backup_path(status, "browser")
    first.write_text("earlier backup", encoding="utf-8")
    second = repair.backup_path(status, "browser")
    assert second != first and not second.exists()


def test_cli_dry_run(tmp_path, capsys):
    status, records = _files(tmp_path)
    cli.main(["--project", str(tmp_path), "reopen", "--status", status.name,
              "--records", records.name, "--doi-prefix", "10.1016/"])
    out = capsys.readouterr().out
    assert "2 record(s) qualify" in out and "dry run" in out
