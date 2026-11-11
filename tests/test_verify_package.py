"""Wave 3 / G2 regression: staged package layout verification.

Why this suite exists
---------------------
``ov_models/`` is a dual-purpose directory name in a onedir package:

    <pkg>/ov_models/            writable USER DATA (recognition weights,
                                downloaded on demand, never touched by update)
    <pkg>/_internal/ov_models/  read-only SHIPPED resources (Silero VAD model
                                and mel filters, added via --add-data)

An early G2 rule in ``tools/verify_package.py`` banned the *name* from
``_internal/``, which contradicted ``build_release.py`` (STATIC_DATA +
REQUIRED_FILES), ``app.py::_find_vad_model()`` and ``tests/test_layout.py``.
Everything passed CI because the release workflow was never run; the first
real Windows build assembled a 198 MB ZIP and then failed at the verify step.

This suite exercises ``check_layout()`` directly plus a full ``main()`` run
against a fabricated staged package, so both the rule and its wiring are
covered without PyInstaller or a 200 MB artifact.

    python tests/test_verify_package.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import release_manifest  # noqa: E402
import verify_package as vp  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _make_stage(root: Path) -> Path:
    """Build a minimal but complete onedir stage (sparse file for size gate)."""
    pkg = root / "release_pkg"
    stage = pkg / "QwenASR-WebView"
    internal = stage / "_internal"
    for p in (stage / "ov_models", stage / "subtitles", stage / "longform_state",
              internal / "ov_models", internal / "webview_assets",
              internal / "openvino" / "libs"):
        p.mkdir(parents=True, exist_ok=True)
    (stage / "ov_models" / ".keep").write_text("", encoding="utf-8")
    (stage / "subtitles" / ".keep").write_text("", encoding="utf-8")
    (stage / "longform_state" / ".keep").write_text("", encoding="utf-8")

    (stage / "QwenASR-WebView.exe").write_bytes(b"MZ" + b"\0" * (2 * 1024 * 1024))
    for rel in ("longform_runner.py", "downloader.py", "models.json",
                "prompt_template.json", "webview_assets/index.html"):
        (internal / rel).write_text("stub", encoding="utf-8")
    (internal / "ov_models" / "silero_vad_v4.onnx").write_bytes(b"onnx" * 100)
    # Wave 7 slimming contract: CPU path present, GPU/NPU frontends absent.
    for name in ("openvino_intel_cpu_plugin.dll", "openvino.dll",
                 "openvino_ir_frontend.dll"):
        (internal / "openvino" / "libs" / name).write_bytes(b"dll")
    (internal / "ov_models" / "mel_filters.npy").write_bytes(b"npy" * 100)
    # _internal/ must exceed 150 MB; a sparse file keeps the fixture cheap.
    with (internal / "big_runtime.dll").open("wb") as f:
        f.truncate(160 * 1024 * 1024)

    # 启动关键模块（2026-11-11 崩溃后新增的硬性要求）
    for rel in ("ui_text.py", "webview_backend.py", "webview_server.py",
                "cluster_core.py"):
        (internal / rel).write_text("# stub", encoding="utf-8")
    (stage / "使用说明.txt").write_text("stub", encoding="utf-8")

    (pkg / "archive_verify.json").write_text(json.dumps({
        "方式": "onedir 目录文件检查", "状态": "通过", "命中": [], "缺失": [],
    }, ensure_ascii=False), encoding="utf-8")
    (stage / "build_manifest.json").write_text(json.dumps({
        "随附 Python 源码": ["longform_runner.py"],
    }, ensure_ascii=False), encoding="utf-8")
    write_zip(pkg / "QwenASR-WebView_test_windows-x64.zip", stage)
    release_manifest.build_manifest(stage, version="test", sha="local")
    return stage


# 与 tools/verify_package.check_zip_entries 的必备条目保持一致
ZIP_MUST = [
    "QwenASR-WebView/QwenASR-WebView.exe",
    "QwenASR-WebView/manifest.json",
    "QwenASR-WebView/使用说明.txt",
    "QwenASR-WebView/_internal/ui_text.py",
    "QwenASR-WebView/_internal/webview_backend.py",
    "QwenASR-WebView/_internal/webview_server.py",
    "QwenASR-WebView/_internal/cluster_core.py",
    "QwenASR-WebView/_internal/openvino/libs/openvino.dll",
    "QwenASR-WebView/_internal/openvino/libs/openvino_intel_cpu_plugin.dll",
]


ZIP_ROOT = "QwenASR-WebView_test_windows-x64"      # 与 build_release 的命名规则一致


def write_zip(zip_path: Path, stage: Path, skip=(), extra=()) -> Path:
    """按真实发行包的布局写一个最小 ZIP（跳过 / 追加指定条目用于反例）。"""
    import zipfile
    names = [n for n in ZIP_MUST if n not in skip]
    with zipfile.ZipFile(zip_path, "w") as z:
        for rel in names:
            src = stage / rel[len("QwenASR-WebView/"):]
            if src.is_file():
                z.write(src, rel)
            else:                       # stage 里没有的给个占位内容
                z.writestr(rel, b"stub")
        for rel in extra:
            z.writestr(rel, b"stub")
    return zip_path


def main() -> int:
    r = Result()

    print("\n[check_layout] valid dual-purpose layout is accepted")
    with tempfile.TemporaryDirectory() as td:
        stage = _make_stage(Path(td))
        errs = vp.check_layout(stage)
        r.check("no errors for a well-formed package", errs == [], str(errs))
        r.check("shipped VAD resource is present",
                (stage / "_internal/ov_models/silero_vad_v4.onnx").is_file())
        r.check("user model dir is beside the launcher",
                (stage / "ov_models").is_dir())

    print("\n[single source of truth] whitelist follows build_release.STATIC_DATA")
    import build_release
    from pathlib import Path as _Path
    declared = {_Path(src).name for src, dst in build_release.STATIC_DATA
                if dst == "ov_models"}
    r.check("STATIC_DATA really declares ov_models resources", bool(declared),
            str(sorted(declared)))
    r.check("shipped_ov_assets() contains every declared resource",
            declared <= vp.shipped_ov_assets(), str(sorted(vp.shipped_ov_assets())))
    r.check("legacy VAD alias is still tolerated",
            "silero_vad.onnx" in vp.shipped_ov_assets())

    print("\n[check_layout] genuine regressions are still rejected")
    cases = [
        ("user data dir missing", lambda s: __import__("shutil").rmtree(s / "subtitles"),
         "缺少用户数据目录 subtitles/"),
        ("subtitles/ leaked into _internal",
         lambda s: (s / "_internal/subtitles").mkdir(),
         "subtitles/ 不应出现在 _internal/ 内"),
        ("longform_state/ leaked into _internal",
         lambda s: (s / "_internal/longform_state").mkdir(),
         "longform_state/ 不应出现在 _internal/ 内"),
        ("ffmpeg/ leaked into _internal",
         lambda s: (s / "_internal/ffmpeg").mkdir(),
         "ffmpeg/ 不应出现在 _internal/ 内"),
        ("recognition weights shipped in _internal/ov_models",
         lambda s: (s / "_internal/ov_models/qwen3_asr_0p6b.bin").write_bytes(b"w"),
         "不是允许的随包只读资源"),
        ("model bundle subdirectory in _internal/ov_models",
         lambda s: (s / "_internal/ov_models/qwen3_asr_int8").mkdir(),
         "是子目录"),
        ("VAD model absent from _internal/ov_models",
         lambda s: (s / "_internal/ov_models/silero_vad_v4.onnx").unlink(),
         "缺少随包 VAD 模型"),
        ("_internal/ov_models/ entirely absent",
         lambda s: __import__("shutil").rmtree(s / "_internal/ov_models"),
         "缺少 _internal/ov_models/"),
    ]
    for name, mutate, expected in cases:
        with tempfile.TemporaryDirectory() as td:
            stage = _make_stage(Path(td))
            mutate(stage)
            errs = vp.check_layout(stage)
            r.check(name, any(expected in e for e in errs), str(errs))

    print("\n[check_layout] the old false positive does NOT fire")
    with tempfile.TemporaryDirectory() as td:
        stage = _make_stage(Path(td))
        errs = vp.check_layout(stage)
        r.check("no blanket 'ov_models/ 不应出现在 _internal/ 内' error",
                not any("ov_models/ 不应出现在 _internal/ 内" in e for e in errs),
                str(errs))

    print("\n[main] end-to-end verification of a fabricated package")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stage = _make_stage(root)
        original_root = vp.ROOT
        vp.ROOT = root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = vp.main()
            out = buf.getvalue()
            r.check("verify_package exits 0 on the real built layout", code == 0,
                    out.strip()[:200])
            r.check("verification reports success", "校验通过" in out)
            r.check("manifest check ran", "manifest：" in out)

            # A leaked user-data directory must be caught by check_layout,
            # before the manifest check, with a clear Chinese error.
            (stage / "_internal/subtitles").mkdir()
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                code2 = vp.main()
            out2 = buf2.getvalue()
            r.check("verify_package exits 1 on leaked user data", code2 == 1)
            r.check("error names the leaked directory",
                    "subtitles/ 不应出现在 _internal/ 内" in out2, out2.strip()[:200])
        finally:
            vp.ROOT = original_root

    print("\n[zip entries] 逐个 ZIP 校验（主包 + noffmpeg 变体）")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stage = _make_stage(root)
        pkg = root / "release_pkg"
        z1 = pkg / "QwenASR-WebView_test_windows-x64.zip"

        r.check("主包条目检查通过", vp.check_zip_entries(z1) == [],
                str(vp.check_zip_entries(z1)))

        # 第二个 ZIP：noffmpeg 变体，只影响 ffmpeg/ 目录，不该影响必备模块
        z2 = write_zip(pkg / "QwenASR-WebView_test_windows-x64-noffmpeg.zip", stage)
        r.check("noffmpeg 变体条目检查通过", vp.check_zip_entries(z2) == [])

        # 缺 ui_text.py —— 2026-11-11 那次崩溃的形态，必须被拦住
        z3 = write_zip(root / "broken.zip", stage,
                       skip=("QwenASR-WebView/_internal/ui_text.py",))
        errs3 = vp.check_zip_entries(z3)
        r.check("ZIP 缺 ui_text.py 被拦下", any("ui_text.py" in e for e in errs3),
                str(errs3))

        # scipy 混进来（69 MB 白送）
        z4 = write_zip(root / "fat.zip", stage,
                       extra=("QwenASR-WebView/_internal/scipy/__init__.py",))
        errs4 = vp.check_zip_entries(z4)
        r.check("ZIP 含 scipy 被拦下", any("scipy" in e for e in errs4), str(errs4))

        # 两个 ZIP 同时存在时，main() 必须都接受（曾死板要求恰好 1 个）
        original_root = vp.ROOT
        vp.ROOT = root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = vp.main()
            out = buf.getvalue()
            r.check("两个 ZIP 并存时 main() 仍通过", code == 0, out.strip()[:200])
            r.check("两个 ZIP 都被点名检查", out.count("ZIP 条目检查通过") == 2,
                    str(out.count("ZIP 条目检查通过")))
        finally:
            vp.ROOT = original_root

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
