"""Wave 3 / G3-G4-G7 regression: content-addressed manifest, delta update,
smooth upgrade (user data survives) and rollback. G6 selftest smoke check.

Builds small fake onedir packages in temp dirs; no PyInstaller, no Windows.

    python tests/test_release_manifest.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import release_manifest as rm  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def make_pkg(root: Path, version: str, payload: dict | None = None) -> Path:
    """Create a minimal onedir package plus user data."""
    root.mkdir(parents=True, exist_ok=True)
    (root / rm.LAUNCHER).write_bytes(b"MZ-launcher-" + version.encode())
    (root / rm.PAYLOAD_DIR).mkdir(exist_ok=True)
    files = payload or {"core.py": b"print('v1')\n", "data.json": b'{"a":1}\n'}
    for name, data in files.items():
        p = root / rm.PAYLOAD_DIR / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for d in ("ov_models", "subtitles", "longform_state", "ffmpeg"):
        (root / d).mkdir(exist_ok=True)
    (root / "settings.json").write_text('{"uiLang":"hant"}', encoding="utf-8")
    (root / "subtitles" / "lesson.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n",
                                                    encoding="utf-8")
    (root / "ov_models" / "model.bin").write_bytes(b"weights")
    (root / "使用说明.txt").write_text("readme", encoding="utf-8")
    (root / "build_manifest.json").write_text("{}", encoding="utf-8")
    rm.build_manifest(root, version, "abc1234")
    return root


def main() -> int:
    r = Result()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old = make_pkg(tmp / "old", "2.0.0")
        print("\n[G3] build + verify")
        m = rm.read_manifest(old)
        r.check("manifest records managed files only",
                all(k.startswith(rm.PAYLOAD_DIR + "/") or k == rm.LAUNCHER
                    for k in m["files"]), str(list(m["files"])[:4]))
        r.check("user data excluded from manifest",
                not any("subtitles/lesson.srt" in k or "settings.json" == k
                        for k in m["files"]))
        r.check("manifest has schema / layout / version",
                m["schema"] == 1 and m["layout"] == "onedir" and m["version"] == "2.0.0")
        v = rm.verify_manifest(old)
        r.check("verify passes on an intact package", v["ok"], str(v))
        r.check("verify counted every managed file", v["checked"] == len(m["files"]))

        print("\n[G3] tamper detection")
        tampered = make_pkg(tmp / "tampered", "2.0.0")
        (tampered / rm.PAYLOAD_DIR / "core.py").write_text("print('hacked')", encoding="utf-8")
        v2 = rm.verify_manifest(tampered)
        r.check("modified payload detected", not v2["ok"] and
                rm.PAYLOAD_DIR + "/core.py" in v2["changed"], str(v2))
        deleted = make_pkg(tmp / "deleted", "2.0.0")
        (deleted / rm.PAYLOAD_DIR / "data.json").unlink()
        v3 = rm.verify_manifest(deleted)
        r.check("missing payload detected", not v3["ok"] and
                rm.PAYLOAD_DIR + "/data.json" in v3["missing"])
        extra = make_pkg(tmp / "extra", "2.0.0")
        (extra / rm.PAYLOAD_DIR / "stray.dll").write_bytes(b"x")
        v4 = rm.verify_manifest(extra)
        r.check("unlisted payload detected", not v4["ok"] and
                rm.PAYLOAD_DIR + "/stray.dll" in v4["extra"])

        print("\n[G3] delta between releases")
        new = make_pkg(tmp / "new", "2.0.1", {
            "core.py": b"print('v2')\n",      # changed
            "data.json": b'{"a":1}\n',        # unchanged
            "new_lib.dll": b"dll",            # added
        })
        d = rm.diff_manifests(old, new)
        r.check("changed file identified", rm.PAYLOAD_DIR + "/core.py" in d["changed"] and
                len(d["changed"]) == 2, str(d["changed"]))
        r.check("added file identified", d["added"] == [rm.PAYLOAD_DIR + "/new_lib.dll"])
        r.check("unchanged file identified", d["unchanged"] == [rm.PAYLOAD_DIR + "/data.json"])
        r.check("launcher counted as changed", rm.LAUNCHER in d["changed"])
        r.check("delta bytes are smaller than the full package",
                0 < d["changedBytes"] < d["totalBytes"], str(d["changedBytes"]))

        print("\n[G4] smooth upgrade keeps user data")
        install = tmp / "install"
        rm.apply_update(old, install)     # first install
        # simulate real usage after install
        (install / "settings.json").write_text('{"uiLang":"en","vad":0.4}', encoding="utf-8")
        (install / "subtitles" / "my_lecture.srt").write_text("my subtitles", encoding="utf-8")
        (install / "ov_models" / "downloaded.bin").write_bytes(b"4GB-ish")
        (install / "longform_state" / "checkpoint.json").write_text('{"done":42}',
                                                                     encoding="utf-8")
        old_launcher = (install / rm.LAUNCHER).read_bytes()
        res = rm.apply_update(new, install)
        r.check("apply reports success + backup", res["ok"] and res["backup"])
        r.check("launcher replaced", (install / rm.LAUNCHER).read_bytes() != old_launcher)
        r.check("payload updated",
                (install / rm.PAYLOAD_DIR / "core.py").read_bytes() == b"print('v2')\n")
        r.check("settings preserved",
                json.loads((install / "settings.json").read_text(encoding="utf-8"))["uiLang"] == "en")
        r.check("subtitles preserved",
                (install / "subtitles" / "my_lecture.srt").read_text() == "my subtitles")
        r.check("downloaded models preserved",
                (install / "ov_models" / "downloaded.bin").read_bytes() == b"4GB-ish")
        r.check("checkpoints preserved",
                json.loads((install / "longform_state" / "checkpoint.json").read_text())["done"] == 42)
        r.check("installed manifest updated", rm.read_manifest(install)["version"] == "2.0.1")
        r.check("installed package verifies", rm.verify_manifest(install)["ok"])

        print("\n[G4] rollback")
        rb = rm.rollback(install)
        r.check("rollback succeeded", rb["ok"])
        r.check("launcher restored", (install / rm.LAUNCHER).read_bytes() == old_launcher)
        r.check("payload restored",
                (install / rm.PAYLOAD_DIR / "core.py").read_bytes() == b"print('v1')\n")
        r.check("user data still intact after rollback",
                (install / "subtitles" / "my_lecture.srt").read_text() == "my subtitles")
        r.check("manifest rolled back", rm.read_manifest(install)["version"] == "2.0.0")

        print("\n[G3] rejected update leaves install untouched")
        broken = make_pkg(tmp / "broken", "2.0.2")
        (broken / rm.PAYLOAD_DIR / "core.py").write_text("tampered", encoding="utf-8")
        snapshot = (install / rm.LAUNCHER).read_bytes()
        try:
            rm.apply_update(broken, install)
            r.check("unverified package refused", False)
        except ValueError:
            r.check("unverified package refused", True)
        r.check("failed update changed nothing",
                (install / rm.LAUNCHER).read_bytes() == snapshot)

        print("\n[G6/G7] CLI + build script wiring")
        out = subprocess.run([sys.executable, "release_manifest.py", "verify",
                              "--root", str(new)], cwd=ROOT,
                             capture_output=True, text=True)
        r.check("verify CLI exits 0 on a good package", out.returncode == 0,
                out.stdout.strip()[:80])
        r.check("verify CLI prints JSON", '"ok": true' in out.stdout)
        out2 = subprocess.run([sys.executable, "release_manifest.py", "verify",
                               "--root", str(tampered)], cwd=ROOT,
                              capture_output=True, text=True)
        r.check("verify CLI exits 1 on a tampered package", out2.returncode == 1)
        selftest = subprocess.run([sys.executable, "app_webview.py", "--selftest"],
                                  cwd=ROOT, capture_output=True, text=True, timeout=30)
        r.check("selftest runs headless", selftest.returncode == 0, selftest.stderr[:100])
        # 启动器的进度与 JSON 都走 stderr（stdout 留给 CLI 模式的数据输出），
        # 所以「干净 JSON」= stderr 里恰好有一行可解析的 JSON。
        payload = None
        for line in selftest.stderr.splitlines():
            if line.strip().startswith('{"ok"'):
                try:
                    payload = json.loads(line.strip())
                except ValueError:
                    payload = None
        r.check("selftest emits clean JSON",
                payload is not None and payload.get("ok") is True
                and "version" in payload, selftest.stderr.strip()[-160:])
        r.check("selftest 不往 stdout 写杂讯", selftest.stdout.strip() == "",
                selftest.stdout[:100])
        br = (ROOT / "build_release.py").read_text(encoding="utf-8")
        vp = (ROOT / "tools/verify_package.py").read_text(encoding="utf-8")
        r.check("build generates a manifest before zipping",
                "from release_manifest import build_manifest" in br and
                br.index("build_manifest(stage") < br.index("zipfile.ZipFile"))
        r.check("release verification checks the manifest",
                "from release_manifest import verify_manifest" in vp)
        r.check("manifest.json ships in the package", '"manifest.json"' in br)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
