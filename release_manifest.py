"""release_manifest.py - G3 content-addressed manifest, delta update, rollback.

Layout contract (see docs/架构说明.md §2)
----------------------------------------
    QwenASR-WebView.exe     launcher (replaceable)
    _internal/              program payload (replaceable)
    manifest.json           this file (replaceable)
    ov_models/ subtitles/ longform_state/ ffmpeg/ crispasr/ chatllm/
    GPUModel/ settings.json app.lock     USER DATA - never touched by update

An update only replaces the three replaceable items. Everything else stays in
place, which is the whole point of the onedir layout (G2). Before replacing,
the current launcher + _internal/ are copied to ``app.prev/`` so ``rollback``
can restore a known-good state without a reinstall.

Commands
--------
    python release_manifest.py build   --root <pkg> --version V --sha S
    python release_manifest.py verify  --root <pkg>
    python release_manifest.py diff    --old <pkg> --new <pkg>
    python release_manifest.py apply   --new <pkg> --install <dir>
    python release_manifest.py rollback --install <dir>

``build`` is called by build_release.py; ``verify`` by CI (G7).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

MANIFEST_NAME = "manifest.json"
PREV_DIR = "app.prev"
LAUNCHER = "QwenASR-WebView.exe"
PAYLOAD_DIR = "_internal"

# Top-level entries that belong to the user, not to the release. An update
# must never delete, overwrite or even enumerate them.
USER_DATA = frozenset({
    "ov_models", "subtitles", "longform_state", "ffmpeg", "crispasr",
    "chatllm", "GPUModel", "app.prev", "app.lock", "settings.json",
    "使用说明.txt", "build_manifest.json", MANIFEST_NAME,
    "_offscreen_out",
})

# Only these three items are managed content.
MANAGED = (LAUNCHER, PAYLOAD_DIR)

# Directories that must exist after an install even when the target folder was
# empty; the app writes into them on first run.
USER_DIRS = ("ov_models", "subtitles", "longform_state", "ffmpeg")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _relative_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts[0] in USER_DATA:
            continue
        if rel.parts[0] not in (LAUNCHER, PAYLOAD_DIR):
            continue
        out.append(p)
    return out


def build_manifest(root: Path, version: str = "dev", sha: str = "local") -> dict:
    root = Path(root).resolve()
    if not (root / LAUNCHER).is_file():
        raise FileNotFoundError(f"launcher missing: {root / LAUNCHER}")
    if not (root / PAYLOAD_DIR).is_dir():
        raise FileNotFoundError(f"payload missing: {root / PAYLOAD_DIR}")
    files = {}
    total = 0
    for p in _relative_files(root):
        rel = p.relative_to(root).as_posix()
        size = p.stat().st_size
        files[rel] = {"sha256": _sha256(p), "bytes": size}
        total += size
    manifest = {
        "schema": 1,
        "layout": "onedir",
        "version": version,
        "sha": sha,
        "createdAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "launcher": LAUNCHER,
        "payloadDir": PAYLOAD_DIR,
        "fileCount": len(files),
        "totalBytes": total,
        "files": files,
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def read_manifest(root: Path) -> dict:
    p = Path(root) / MANIFEST_NAME
    if not p.is_file():
        raise FileNotFoundError(f"manifest missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("layout") != "onedir":
        raise ValueError("unsupported manifest schema / layout")
    return data


def verify_manifest(root: Path) -> dict:
    """Return ``{ok, checked, missing, changed, extra}``."""
    root = Path(root).resolve()
    m = read_manifest(root)
    missing, changed = [], []
    for rel, meta in m["files"].items():
        p = root / rel
        if not p.is_file():
            missing.append(rel)
            continue
        if p.stat().st_size != meta["bytes"] or _sha256(p) != meta["sha256"]:
            changed.append(rel)
    present = {p.relative_to(root).as_posix() for p in _relative_files(root)}
    extra = sorted(present - set(m["files"]))
    return {"ok": not missing and not changed and not extra,
            "checked": len(m["files"]), "missing": missing,
            "changed": changed, "extra": extra,
            "version": m.get("version"), "totalBytes": m.get("totalBytes")}


def diff_manifests(old_root: Path, new_root: Path) -> dict:
    old = read_manifest(old_root)["files"]
    new = read_manifest(new_root)["files"]
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new)
                     if old[k]["sha256"] != new[k]["sha256"])
    unchanged = sorted(set(old) & set(new) - set(changed))
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged": unchanged,
            "changedBytes": sum(new[k]["bytes"] for k in added + changed),
            "totalBytes": read_manifest(new_root)["totalBytes"]}


def _copy_managed(src: Path, dst: Path):
    shutil.copy2(src / LAUNCHER, dst / LAUNCHER)
    target = dst / PAYLOAD_DIR
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src / PAYLOAD_DIR, target)


def apply_update(new_root: Path, install_dir: Path) -> dict:
    """Install a new payload, backing up the old one to app.prev/.

    User data is never enumerated or touched. Fails before any modification if
    the new package does not verify.
    """
    new_root, install_dir = Path(new_root).resolve(), Path(install_dir).resolve()
    check = verify_manifest(new_root)
    if not check["ok"]:
        raise ValueError(f"new package failed verification: {check}")
    install_dir.mkdir(parents=True, exist_ok=True)
    for d in USER_DIRS:
        (install_dir / d).mkdir(exist_ok=True)
    prev = install_dir / PREV_DIR
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir()
    had_prev = False
    launcher = install_dir / LAUNCHER
    payload = install_dir / PAYLOAD_DIR
    if launcher.is_file():
        shutil.copy2(launcher, prev / LAUNCHER)
        had_prev = True
    if payload.is_dir():
        shutil.copytree(payload, prev / PAYLOAD_DIR)
        had_prev = True
    old_manifest = install_dir / MANIFEST_NAME
    if old_manifest.is_file():
        shutil.copy2(old_manifest, prev / MANIFEST_NAME)
    try:
        _copy_managed(new_root, install_dir)
        shutil.copy2(new_root / MANIFEST_NAME, install_dir / MANIFEST_NAME)
    except Exception:
        rollback(install_dir)
        raise
    return {"ok": True, "backup": had_prev, "prev": str(prev),
            "version": read_manifest(install_dir)["version"]}


def rollback(install_dir: Path) -> dict:
    """Restore launcher + _internal/ + manifest from app.prev/."""
    install_dir = Path(install_dir).resolve()
    prev = install_dir / PREV_DIR
    if not prev.is_dir():
        raise FileNotFoundError(f"no backup to roll back to: {prev}")
    if (prev / LAUNCHER).is_file():
        shutil.copy2(prev / LAUNCHER, install_dir / LAUNCHER)
    if (prev / PAYLOAD_DIR).is_dir():
        target = install_dir / PAYLOAD_DIR
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(prev / PAYLOAD_DIR, target)
    if (prev / MANIFEST_NAME).is_file():
        shutil.copy2(prev / MANIFEST_NAME, install_dir / MANIFEST_NAME)
    return {"ok": True, "restored": True}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="onedir release manifest / delta / rollback")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--root", required=True)
    b.add_argument("--version", default="dev"); b.add_argument("--sha", default="local")
    v = sub.add_parser("verify"); v.add_argument("--root", required=True)
    d = sub.add_parser("diff"); d.add_argument("--old", required=True); d.add_argument("--new", required=True)
    a = sub.add_parser("apply"); a.add_argument("--new", required=True); a.add_argument("--install", required=True)
    rb = sub.add_parser("rollback"); rb.add_argument("--install", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "build":
        m = build_manifest(Path(args.root), args.version, args.sha)
        print(json.dumps({"ok": True, "fileCount": m["fileCount"],
                          "totalBytes": m["totalBytes"]}, ensure_ascii=False))
    elif args.cmd == "verify":
        res = verify_manifest(Path(args.root))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    elif args.cmd == "diff":
        print(json.dumps(diff_manifests(Path(args.old), Path(args.new)),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "apply":
        print(json.dumps(apply_update(Path(args.new), Path(args.install)),
                         ensure_ascii=False))
    elif args.cmd == "rollback":
        print(json.dumps(rollback(Path(args.install)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
