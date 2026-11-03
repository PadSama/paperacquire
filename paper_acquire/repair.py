"""State repair: give one waterfall step a fresh retry budget on chosen records.

For the case where a tool defect, not the paper, spent a step's tries (for example a
publisher-side change that made every attempt fail the same way). Once a fixed engine
has been verified, the operator can reset those budgets with this command. It is a
deliberate repair, never part of a run.

Rules: dry run unless apply=True. A record qualifies when it is pending or not_found, has
spent at least one try on the step, and (for the browser step) carries no per-paper
browser_verdict, which is terminal evidence that needs its own decision. Applying writes
a no-overwrite backup of the status file first, changes only the selected records, checks
that every other record is unchanged, and appends the note to each record's "reopened"
history.
"""
import csv
import json
import shutil
import time
from pathlib import Path


def load_dois(path):
    """uid -> DOI from a records file (.jsonl, .json list or .csv with uid and DOI columns)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
    elif path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        rows = json.loads(text)
    out = {}
    for r in rows:
        uid = r.get("uid") or r.get("UID")
        if uid:
            out[uid] = (r.get("DOI") or r.get("doi") or "").strip()
    return out


def plan_reopen(status, step, uids=None, dois=None, doi_prefix=None):
    """uids (in status order) whose `step` budget would be reset."""
    plan = []
    for uid, st in status.items():
        if not isinstance(st, dict):
            continue
        if uids is not None and uid not in uids:
            continue
        if doi_prefix and not (dois or {}).get(uid, "").lower().startswith(doi_prefix.lower()):
            continue
        if st.get("state") not in ("pending", "not_found"):
            continue
        if int(((st.get("step_state") or {}).get(step) or {}).get("tries") or 0) < 1:
            continue
        if step == "browser" and st.get("browser_verdict"):
            continue
        plan.append(uid)
    return plan


def apply_reopen(status, plan, step, note):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for uid in plan:
        st = status[uid]
        st.setdefault("step_state", {})[step] = {"status": "open", "tries": 0}
        if st.get("state") == "not_found":
            st["state"] = "pending"
            st["next_step"] = step
        st["steps_done"] = [s for s in (st.get("steps_done") or []) if s != step]
        st["reopened"] = f"{st['reopened']}; {note}" if st.get("reopened") else note
        st["updated"] = now
    return status


def backup_path(status_path, step):
    """A fresh backup name next to the status file; an existing backup is never reused."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = status_path.with_name(f"{status_path.stem}.backup-reopen-{step}-{stamp}.json")
    candidate, n = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.stem}-{n}.json")
        n += 1
    return candidate


def run(status_path, step="browser", uids=None, records=None, doi_prefix=None, note=None,
        apply=False, out=print):
    """Plan, and with apply=True perform, the reset. Returns the planned uids."""
    status_path = Path(status_path)
    raw = status_path.read_bytes()
    before = json.loads(raw.decode("utf-8"))
    if doi_prefix and not records:
        raise SystemExit("--doi-prefix needs --records (a file with uid and DOI)")
    dois = load_dois(records) if records else None
    plan = plan_reopen(before, step, set(uids) if uids else None, dois, doi_prefix)
    by_state = {}
    for uid in plan:
        by_state[before[uid]["state"]] = by_state.get(before[uid]["state"], 0) + 1
    out(f"reopen {step}: {len(plan)} record(s) qualify "
        f"({', '.join(f'{k} {v}' for k, v in sorted(by_state.items())) or 'none'})")
    if plan:
        out("  " + " ".join(plan))
    if not apply or not plan:
        out("  dry run: nothing written" if plan else "  nothing to do")
        return plan
    note = note or f"reopen-{time.strftime('%Y%m%d')} {step} budget reset (paper_acquire reopen)"
    backup = backup_path(status_path, step)
    shutil.copyfile(status_path, backup)
    after = apply_reopen(json.loads(raw.decode("utf-8")), plan, step, note)
    untouched = [u for u in before if u not in plan and before[u] != after.get(u)]
    if untouched or set(after) != set(before):
        raise SystemExit(f"FAIL_CLOSED: records outside the plan changed: {untouched[:5]}")
    status_path.write_text(json.dumps(after, ensure_ascii=False, indent=1), encoding="utf-8")
    out(f"  applied; backup {backup.name}")
    return plan
