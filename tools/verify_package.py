"""verify_package.py — 校验发行包完整性（简体中文）。

检查：
  1. 分层校验结果（release_pkg/archive_verify.json）没有缺失条目
  2. 构建清单包含 longform_runner.py（长音频流式模块）
  3. release_pkg/ 下恰好一个 ZIP
  4. 分层产物结构正确：启动器在包根，依赖在 _internal/，且 _internal/ 体积合理
     （onefile 时代用「EXE > 20MB」防空壳；onedir 下启动器本身只有几 MB，
      体积门槛改看 _internal/）
  5. 用户数据与随包只读资源各在其位（见 check_layout）

注意 ``ov_models/`` 是同名双用途目录：

  * 启动器旁 ``ov_models/``   -> 用户数据（按需下载的识别权重），更新时不动
  * ``_internal/ov_models/``  -> 随包只读资源（Silero VAD、mel filters），
    由 build_release.py 的 STATIC_DATA 经 ``--add-data`` 放进分层目录，
    app.py::_find_vad_model() 通过 resource_path() 在此查找。

G2 落地时的一条旧规则按名字一刀切，禁止 ``ov_models/`` 出现在 _internal/，
与构建脚本自相矛盾，导致 release 工作流打包成功后在校验步失败。这里改成
按「内容」判定：_internal/ov_models/ 允许且只允许随包资源白名单，纯用户
数据目录（subtitles/、longform_state/、ffmpeg/）仍然严禁进入 _internal/。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用户可写数据目录：必须在启动器旁，绝不能进会被整包替换的 _internal/。
USER_DATA_DIRS = ("ov_models", "subtitles", "longform_state")

# app.py::_find_vad_model() 额外接受的旧名，不在 STATIC_DATA 里但属随包资源。
LEGACY_VAD_NAMES = frozenset({"silero_vad.onnx"})


def shipped_ov_assets() -> frozenset[str]:
    """_internal/ov_models/ 允许出现的随包只读资源。

    单一事实源来自 build_release.STATIC_DATA，而不是在这里再抄一份名单：
    本次构建失败的根因正是「构建脚本说可以、校验脚本说不行」。以后往包里
    新增随包资源，只需改 STATIC_DATA，校验规则自动跟随，不会再自相矛盾。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from build_release import STATIC_DATA
    names = {Path(src).name for src, dst in STATIC_DATA if dst == "ov_models"}
    return frozenset(names | LEGACY_VAD_NAMES)


def check_layout(stage: Path, assets: frozenset[str] | None = None) -> list[str]:
    """返回包结构错误列表；空列表表示结构合法。

    单独抽出成纯函数，便于 tests/test_verify_package.py 用临时目录树直接
    覆盖合法布局与各类越界情形，无需真的打一个 200 MB 的包。``assets``
    仅供测试显式注入；正式运行一律用 shipped_ov_assets() 的派生结果。
    """
    errors: list[str] = []
    internal = stage / "_internal"
    allowed = shipped_ov_assets() if assets is None else assets

    for d in USER_DATA_DIRS:
        if not (stage / d).is_dir():
            errors.append(f"缺少用户数据目录 {d}/")

    # 纯用户数据目录一旦出现在 _internal/，整包更新会把它删掉。
    for d in ("subtitles", "longform_state", "ffmpeg"):
        if (internal / d).exists():
            errors.append(f"{d}/ 不应出现在 _internal/ 内")

    shipped = internal / "ov_models"
    if shipped.is_dir():
        for p in sorted(shipped.rglob("*")):
            if p.is_dir():
                # 随包资源是扁平的两个文件；任何子目录都意味着模型包被误收。
                errors.append(
                    f"{p.relative_to(stage).as_posix()}/ 是子目录，"
                    "但 _internal/ov_models/ 只允许扁平的 VAD / mel 资源文件")
                continue
            rel = p.relative_to(stage).as_posix()
            if p.name not in allowed:
                errors.append(
                    f"{rel} 不是允许的随包只读资源"
                    "（_internal/ov_models/ 只允许 VAD 与 mel filters）")
        vad = "silero_vad_v4.onnx"
        if not (shipped / vad).is_file() and vad in allowed:
            errors.append(f"_internal/ov_models/ 缺少随包 VAD 模型 {vad}")
    else:
        errors.append("缺少 _internal/ov_models/ 随包只读资源目录")

    return errors


# Wave 7 slimming: the package must ship the CPU execution path and must not
# carry the 40+ MB of GPU/NPU/foreign-frontend binaries it never loads.
OV_KEEP = ["openvino_intel_cpu_plugin.dll", "openvino.dll", "openvino_ir_frontend.dll"]
OV_MUST_NOT = ["openvino_intel_gpu_plugin.dll", "openvino_intel_npu_plugin.dll",
               "openvino_tensorflow_frontend.dll", "openvino_pytorch_frontend.dll"]


def check_openvino_slim(stage: Path) -> list[str]:
    """断言 OpenVINO 精简后仍可推理（CPU 插件在、用不到的不在）。

    缺失 CPU 插件会让 import openvino 直接失败 —— 那是致命的；残留 GPU/NPU
    插件只是浪费体积。所以分开报，前者一律判失败，后者同样判失败（说明精简
    步骤没生效，包会白白大出几十 MB）。
    """
    errors: list[str] = []
    libs = stage / "_internal" / "openvino" / "libs"
    if not libs.is_dir():
        return ["缺少 _internal/openvino/libs/（OpenVINO 未随包）"]
    present = {p.name for p in libs.glob("*")}
    for name in OV_KEEP:
        if name not in present:
            errors.append(f"缺少 OpenVINO 必需组件 {name}（CPU 推理会直接失败）")
    for name in OV_MUST_NOT:
        if name in present:
            errors.append(f"{name} 不应随包（未精简，白白增加体积）")
    return errors



def _force_utf8():
    """Windows 控制台默认 cp1252 会让中文 print 抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def main() -> int:
    _force_utf8()
    pkg = ROOT / "release_pkg"
    v = json.loads((pkg / "archive_verify.json").read_text(encoding="utf-8"))
    print("包内容验证状态：", v["状态"])
    print("命中：", v["命中"])
    print("缺失：", v["缺失"])
    print("验证方式：", v.get("方式", "-"))
    if v["状态"] not in ("通过", "存在缺失"):
        print(f"错误：包内容验证未能执行：{v['状态']}")
        return 1
    if v["缺失"]:
        print(f"错误：EXE 内缺少关键模块：{v['缺失']}")
        return 1

    m = json.loads((pkg / "QwenASR-WebView" / "build_manifest.json")
                   .read_text(encoding="utf-8"))
    src = m.get("随附 Python 源码", [])
    print(f"随包 .py 共 {len(src)} 个")
    if "longform_runner.py" not in src:
        print("错误：构建清单缺少 longform_runner.py")
        return 1

    zips = sorted(pkg.glob("*.zip"))
    if len(zips) != 1:
        print(f"错误：期望 1 个 ZIP，实际 {len(zips)}")
        return 1
    stage = pkg / "QwenASR-WebView"
    exe = stage / "QwenASR-WebView.exe"
    internal = stage / "_internal"
    if not exe.exists():
        print(f"错误：找不到启动器：{exe}")
        return 1
    if not internal.is_dir():
        print(f"错误：缺少分层目录：{internal}")
        return 1

    exe_size = exe.stat().st_size
    if exe_size < 1024 * 1024:
        print(f"错误：启动器仅 {exe_size/1024/1024:.1f} MB，疑似构建不完整")
        return 1
    internal_size = sum(p.stat().st_size for p in internal.rglob("*") if p.is_file())
    if internal_size < 150 * 1024 * 1024:
        print(f"错误：_internal/ 仅 {internal_size/1024/1024:.1f} MB，"
              f"疑似依赖未完整收集")
        return 1

    # 用户数据必须在启动器旁；_internal/ov_models/ 则是随包只读资源，
    # 两者同名但用途不同，按内容判定而不是按目录名一刀切。
    layout_errors = check_layout(stage)
    if layout_errors:
        for err in layout_errors:
            print(f"错误：{err}")
        return 1

    slim_errors = check_openvino_slim(stage)
    if slim_errors:
        for err in slim_errors:
            print(f"错误：{err}")
        return 1
    print("OpenVINO 精简检查：通过（CPU 插件在位，GPU/NPU 前端未随包）")

    # G7: verify the content-addressed manifest of the staged package.
    import sys
    sys.path.insert(0, str(ROOT))
    from release_manifest import verify_manifest
    mf = verify_manifest(stage)
    print(f"manifest：{mf['checked']} 個檔案，狀態 "
          f"{'通過' if mf['ok'] else '失敗'}")
    if not mf["ok"]:
        print(f"錯誤：manifest 校驗失敗 {mf}")
        return 1

    print(f"ZIP：{zips[0].name}  {zips[0].stat().st_size/1024/1024:.1f} MB")
    print(f"启动器：{exe_size/1024/1024:.1f} MB")
    print(f"_internal/：{internal_size/1024/1024:.1f} MB")
    print("校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
