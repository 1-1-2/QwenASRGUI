"""Wave 7 regression: OpenVINO slimming must shrink the package without it.

Two failure modes matter more than the size itself:

  1. deleting too much  -> ``import openvino`` fails at runtime, the app does
     not start at all (CPU plugin / core DLL / IR frontend are mandatory);
  2. deleting nothing    -> the ZIP silently keeps ~40 MB of GPU/NPU/foreign
     frontends, i.e. the "slimming" is theatre.

This drives the real ``slim_openvino`` against a synthetic tree shaped like the
Windows wheel, and the real ``check_openvino_slim`` against the result.

    python tests/test_openvino_slim.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_release as br          # noqa: E402
import verify_package as vp         # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


# Sizes mirror the real win_amd64 wheel (openvino 2025.3.0).
LIBS = {
    "openvino_intel_cpu_plugin.dll": 40_500_000,
    "openvino_intel_gpu_plugin.dll": 30_900_000,
    "openvino.dll": 14_800_000,
    "openvino.lib": 7_700_000,
    "cache.json": 8_800_000,
    "openvino_tensorflow_frontend.dll": 4_100_000,
    "openvino_intel_npu_plugin.dll": 4_000_000,
    # 2026.x wheel 新增：npu_compiler 单个 96 MB，比 CPU 插件还大；
    # 旧规则只写 *intel_npu_plugin* 会把它整个漏掉。
    "openvino_intel_npu_compiler.dll": 96_000_000,
    "openvino_gguf_frontend.dll": 1_300_000,
    "openvino_onnx_frontend.dll": 4_000_000,
    "openvino_pytorch_frontend.dll": 2_600_000,
    "openvino_paddle_frontend.dll": 1_500_000,
    "openvino_tensorflow_lite_frontend.dll": 1_200_000,
    "openvino_jax_frontend.dll": 500_000,
    "openvino_auto_plugin.dll": 500_000,
    "openvino_hetero_plugin.dll": 500_000,
    "openvino_ir_frontend.dll": 400_000,
}


def make_tree(root: Path) -> Path:
    stage = root / "pkg"
    libs = stage / "_internal" / "openvino" / "libs"
    libs.mkdir(parents=True)
    for name, size in LIBS.items():
        # Write the real byte count (truncate) so the reported saving is
        # meaningful; the content is irrelevant to the deletion logic.
        f = libs / name
        with open(f, "wb") as fh:
            fh.truncate(size)
    front = stage / "_internal" / "openvino" / "frontend"
    (front / "onnx").mkdir(parents=True)
    (front / "onnx" / "py_onnx_frontend.pyd").write_bytes(b"\0" * 4096)
    # 真实 wheel 里的纯开发物：C++ 头文件（嵌套两级）与类型存根。
    inc = stage / "_internal" / "openvino" / "include" / "oneapi" / "tbb"
    inc.mkdir(parents=True)
    (inc / "flow_graph.h").write_bytes(b"\0" * 120_000)
    # 复现 2026-11-11 Windows 构建失败：PyInstaller 解析 --hidden-import openvino
    # 时，会把 libs/*.dll 另拷一份到 _internal/ 根。只删 openvino/libs/ 的版本
    # 会漏下这一整套（GPU/NPU 插件），包白白大几十 MB 还被判「未精简」。
    for name in ("openvino_intel_gpu_plugin.dll", "openvino_intel_npu_plugin.dll",
                 "openvino_tensorflow_frontend.dll", "openvino_intel_cpu_plugin.dll",
                 "openvino.dll", "openvino_ir_frontend.dll"):
        (stage / "_internal" / name).write_bytes(b"\0" * 1_000_000)
    (stage / "_internal" / "openvino" / "_pyopenvino" ).mkdir(parents=True, exist_ok=True)
    (stage / "_internal" / "openvino" / "_pyopenvino" / "__init__.pyi").write_bytes(b"\0" * 60_000)
    return stage


def main() -> int:
    r = Result()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage = make_tree(root)
        internal = stage / "_internal"

        r.check("baseline has GPU plugin", "openvino_intel_gpu_plugin.dll" in br_keep(internal))

        report = br.slim_openvino(internal)
        left = br_keep(internal)

        r.check("slimming reports success", not report["失败"], str(report["失败"][:3]))
        r.check("saved a large amount of data", report["节省字节"] > 40_000_000,
                f"{report['节省字节'] / 1e6:.1f} MB")

        for must in br.OPENVINO_MUST_KEEP:
            r.check(f"kept {must}", must in left)

        for gone in ("openvino_intel_gpu_plugin.dll", "openvino_intel_npu_plugin.dll",
                     "openvino_intel_npu_compiler.dll", "openvino_gguf_frontend.dll",
                     "openvino_tensorflow_frontend.dll", "openvino_onnx_frontend.dll",
                     "openvino_pytorch_frontend.dll", "openvino.lib", "cache.json"):
            r.check(f"removed {gone}", gone not in left)

        r.check("plugins still referenced by openvino.dll are kept",
                "openvino_auto_plugin.dll" in left and "openvino_hetero_plugin.dll" in left)
        r.check("python frontend bindings removed",
                not (internal / "openvino" / "frontend").exists())
        # 纯开发物：C++ 头文件与 .pyi 类型存根，运行时 import 不会碰。
        r.check("duplicate DLL copy at _internal/ root is also slimmed",
                not (internal / "openvino_intel_gpu_plugin.dll").exists() and
                not (internal / "openvino_intel_npu_plugin.dll").exists())
        r.check("essential DLL copy at root is preserved",
                (internal / "openvino_intel_cpu_plugin.dll").exists() and
                (internal / "openvino.dll").exists())
        r.check("C++ headers removed (nested include/)",
                not (internal / "openvino" / "include").exists())
        r.check("type stubs removed (*.pyi)",
                not list((internal / "openvino").rglob("*.pyi")))

        # The verifier must accept the slimmed tree...
        errors = vp.check_openvino_slim(stage)
        r.check("verifier accepts the slimmed package", not errors, "; ".join(errors[:3]))

        # ...and reject both regressions.
        # 真实缺失意味着两处副本都没了（libs/ 与 _internal/ 根）。
        # 只删 libs/ 那份不算缺失 —— 校验器现在扫全树，这正是期望行为。
        for p_cpu in internal.rglob("openvino_intel_cpu_plugin.dll"):
            p_cpu.unlink()
        errs = vp.check_openvino_slim(stage)
        r.check("verifier catches a missing CPU plugin",
                any("CPU" in e or "cpu_plugin" in e for e in errs), str(errs[:2]))

    with tempfile.TemporaryDirectory() as tmp2:
        stage2 = make_tree(Path(tmp2))
        errs = vp.check_openvino_slim(stage2)
        r.check("verifier rejects an unslimmed package", bool(errs), str(errs[:2]))

    # Safety valve: an unrecognised layout must be left untouched.
    with tempfile.TemporaryDirectory() as tmp3:
        ov = Path(tmp3) / "_internal" / "openvino" / "libs"
        ov.mkdir(parents=True)
        (ov / "something_else.bin").write_bytes(b"x")
        rep = br.slim_openvino(Path(tmp3) / "_internal")
        r.check("unknown layout is skipped, not damaged",
                rep["跳过原因"] and not rep["删除"], rep["跳过原因"])
        r.check("unknown layout keeps its files",
                (ov / "something_else.bin").exists())

    # The CLI must expose the escape hatch.
    src = (ROOT / "build_release.py").read_text(encoding="utf-8")
    r.check("--no-slim escape hatch exists", '"--no-slim"' in src)
    # ── 端到端：精简必须作用在**最终打包目录**上 ─────────────────────
    # 2026-11-11 连续三次 Windows 构建失败的真因：make_package() 把
    # dist/ 里的中间产物传给了 slim_openvino()，最终包里一个字节没少，
    # 但报告写着「省了 176 MB」。只测 slim_openvino() 本身永远抓不到。
    from unittest import mock as _mock
    e2e_root = Path(tempfile.mkdtemp()) / "e2e"
    produced = e2e_root / "dist" / "webview" / br.APP_NAME
    p_internal = produced / "_internal"
    (p_internal / "openvino" / "libs").mkdir(parents=True)
    (produced / (br.APP_NAME + ".exe")).write_bytes(b"MZ" + b"\0" * 100)
    for name, size in (("openvino_intel_cpu_plugin.dll", 2_000_000),
                       ("openvino.dll", 1_000_000),
                       ("openvino_ir_frontend.dll", 100_000),
                       ("openvino_intel_gpu_plugin.dll", 5_000_000),
                       ("openvino_intel_npu_compiler.dll", 9_000_000)):
        (p_internal / "openvino" / "libs" / name).write_bytes(b"\0" * size)
    (p_internal / "webview_assets").mkdir()
    (p_internal / "webview_assets" / "index.html").write_text("<html>", encoding="utf-8")
    (p_internal / "models.json").write_text('{"bundles": []}', encoding="utf-8")
    with _mock.patch.object(br, "ROOT", e2e_root):
        br.make_package("test", "abc1234", None, slim=True)
    staged = e2e_root / "release_pkg" / br.APP_NAME / "_internal"
    staged_names = {q.name for q in staged.rglob("*") if q.is_file()}
    r.check("end-to-end: final package has no GPU/NPU payload",
            "openvino_intel_gpu_plugin.dll" not in staged_names and
            "openvino_intel_npu_compiler.dll" not in staged_names,
            str(sorted(staged_names)[:6]))
    r.check("end-to-end: final package keeps the CPU path",
            {"openvino_intel_cpu_plugin.dll", "openvino.dll",
             "openvino_ir_frontend.dll"} <= staged_names)


    r.check("manifest records what was removed (manifest 记下了精简动作)", '"OpenVINO 精简"' in src)

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


def br_keep(internal: Path) -> set[str]:
    libs = internal / "openvino" / "libs"
    return {p.name for p in libs.glob("*")} if libs.is_dir() else set()


if __name__ == "__main__":
    raise SystemExit(main())
