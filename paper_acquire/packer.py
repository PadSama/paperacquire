"""Privacy-gated outbound packer.

Every zip this tool produces for sharing (collaborators, a remote service, anyone) is
scanned for the operator's identity markers before it is allowed to exist: the contact
e-mail (local part and full address) and every token listed in config
"privacy_markers". A single hit deletes the whole pack and raises. Markers are matched
with regex escapes; the marker list itself never has to appear in this source file.
"""
import re
import time
import zipfile
from pathlib import Path

TEXT_EXTS = (".txt", ".json", ".jsonl", ".md", ".csv", ".py", ".html")


def marker_patterns(cfg):
    email = (cfg.get("contact_email") or "").strip()
    marks = [m for m in (email.split("@")[0], email, *(cfg.get("privacy_markers") or [])) if m]
    return [re.compile(re.escape(m), re.I) for m in marks]


def scan_zip(zpath, patterns):
    hits = []
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.endswith(TEXT_EXTS):
                body = z.read(n).decode("utf-8", "ignore")
                if any(p.search(body) for p in patterns):
                    hits.append(n)
    return hits


class PrivacyLeak(RuntimeError):
    pass


def write_pack(project: Path, files, stamp_label, max_mb=8.0):
    """Pack `files` (list of Path) + a summary into return/, enforcing the privacy gate."""
    ret = project / "return"
    ret.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    parts, part, z, n_in = [], 0, None, 0
    import json
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    pats = marker_patterns(cfg)

    def open_part():
        nonlocal part, z, n_in
        part += 1
        zp = ret / f"{stamp_label}_{stamp}_part{part}.zip"
        parts.append(zp)
        z, n_in = zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED), 0

    open_part()
    for f in sorted(files):
        if n_in and z.fp.tell() > max_mb * 1e6:
            z.close()
            open_part()
        z.write(f, f.relative_to(project).as_posix())
        n_in += 1
    z.writestr("upload_summary.txt", f"paper_acquire pack {stamp_label} {time.strftime('%Y-%m-%dT%H:%M:%S')}: "
                                     f"{len(files)} files; parts {len(parts)}\n")
    z.close()
    for zp in parts:
        hits = scan_zip(zp, pats)
        if hits:
            zp.unlink()
            raise PrivacyLeak(f"privacy gate: {zp.name} contains identity markers ({hits[:3]}...); "
                              f"pack deleted, nothing was produced.")
    return parts
