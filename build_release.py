"""build_release.py — CI 专用：构建 WebView **onedir 分层**发行包（简体中文注释）。

本文件为「一键发行包」改造新增，注释与产出说明统一使用简体中文。

为什么不直接复用 build_webview.bat
----------------------------------
原脚本把路径写死在 ``F:\\AIStudio\\QwenASR`` 与本机 venv，GitHub Actions 上
无法执行；更关键的是它手工列举 --add-data，遗漏了本次新增的
``longform_runner.py``。本项目大量模块是**函数内动态 import**（downloader、
longform_runner、ffmpeg_utils……），PyInstaller 静态分析抓不到，只能靠
``--hidden-import`` 或把 .py 当数据文件一起放进去，运行时由标准 PathFinder 加载。
一旦漏列，EXE 启动时不会报错，点到对应功能才 ImportError —— 长音频模式就会
这样静默失效。因此本脚本**自动扫描根目录所有 .py**，从结构上杜绝漏列。

为什么从 onefile 改成 onedir（Wave 3 / G2）
------------------------------------------
onefile 每次启动都要把约 200 MB 解压到 ``%TEMP%``，且每次更新都得整体替换。
去掉 pythonnet（G1）之后，当年迫使 onefile 的 MOTW 失败链已经不存在，
于是改为分层目录：

    QwenASR-WebView.exe      启动器 + 运行时引导（内容稳定，SmartScreen 信誉可累积）
    _internal/               全部依赖、原生库、Python 模块（由 PyInstaller 生成）
    ov_models/ subtitles/ longform_state/ ffmpeg/   用户数据与外带资源

用户数据一律留在 EXE 旁；只有**只读的随包资源**（models.json、前端、VAD、
icon、prompt_template.json）放进 ``_internal/``。两者的区别由 ``app.py`` 的
``resource_path()`` 统一表达，见 tests/test_layout.py。

仍用 ``--add-data`` 把 .py 放进去而没有改用 ``--hidden-import`` 的原因：数据文件
在 onedir 下是**实际文件**，出问题时能在 ``_internal/`` 里直接看到、比对、替换；
``--hidden-import`` 进的是 PYZ，只能靠 pyi-archive_viewer 间接确认。代价是包里
多存一份源码（约 1 MB），换来可自证。

用法
----
    python build_release.py --version 2.0.0-onedir --sha abc1234
    python build_release.py --dry-run          # 只打印 PyInstaller 命令，不执行

产物
----
    dist/webview/QwenASR-WebView/QwenASR-WebView.exe      PyInstaller onedir 输出
    dist/webview/QwenASR-WebView/_internal/               分层依赖
    release_pkg/QwenASR-WebView/                          解压即用的目录
    release_pkg/QwenASR-WebView_<ver>_windows-x64.zip
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "QwenASR-WebView"
EXCLUDE_PY = {
    "app-gpu.py",               # GPU 版独立入口，不进 CPU 包
    "generate_prompt_template.py",  # 开发期工具，需要 torch/transformers
    "app_webview.py",           # PyInstaller 入口本身由命令行指定
    "build_release.py",         # 本脚本
}

# 随包附带的小文件（路径, 包内目标目录）
STATIC_DATA = [
    ("assets/icon.ico", "."),
    ("prompt_template.json", "."),
    ("models.json", "."),          # Wave 2 catalog: read from EXE dir at runtime
    ("webview", "webview_assets"),
    ("ov_models/mel_filters.npy", "ov_models"),
    ("ov_models/silero_vad_v4.onnx", "ov_models"),
]

# Packages with native DLLs / data files that PyInstaller cannot fully trace.
# pywebview / pythonnet / clr_loader were removed in Wave 3 (G1): the default
# window is Edge --app, so no .NET assembly is ever loaded. That also removes
# the MOTW failure mode that forced the onefile build.
COLLECT_DIRS = ["opencc", "openvino", "kaldi_native_fbank"]
COLLECT_ALL = ["tokenizers"]
COLLECT_DATA = ["certifi"]

HIDDEN_IMPORTS = [
    "certifi", "proxy_tools", "bottle", "app", "webview_backend", "webview_server",
    "openvino", "openvino.runtime", "onnxruntime", "opencc", "customtkinter",
    "sounddevice", "soxr", "soundfile", "kaldi_native_fbank",
    "scipy", "scipy.cluster", "scipy.cluster.hierarchy",
    "scipy.spatial", "scipy.spatial.distance", "scipy._lib.messagestream",
    "crisp_engine", "subtitle_lines", "cli_mode", "version", "updater",
    "fa_aligner", "api_server", "endpoint_tab", "segno", "cf_tunnel",
    "proc_guard", "diarize", "audio_io", "processor_numpy", "downloader",
    "ffmpeg_utils", "model_catalog", "longform_runner", "app_paths",
]

# CPU + OpenVINO INT8 only. Exclude PyTorch / transformers, and the .NET
# window stack that is no longer used by the default Edge --app path.
EXCLUDE_MODULES = [
    "torch", "torchvision", "torchaudio", "transformers", "qwen_asr",
    "triton", "bitsandbytes",
    "webview", "webview.platforms", "pythonnet", "clr", "clr_loader",
]


def add_data_pair(src: str, dst: str) -> list[str]:
    sep = ";" if sys.platform == "win32" else ":"
    return ["--add-data", f"{ROOT / src}{sep}{dst}"]


def pkg_dir(module: str) -> Path:
    m = importlib.import_module(module)
    return Path(m.__file__).resolve().parent


def py_sources() -> list[str]:
    return sorted(p.name for p in ROOT.glob("*.py")
                  if p.name not in EXCLUDE_PY and not p.name.startswith("test_"))


def build_command(distpath: Path, resolve_deps: bool = True) -> list[str]:
    # onedir: PyInstaller writes <distpath>/<APP_NAME>/ with the launcher on top
    # and every dependency under _internal/. No --onefile: the 200 MB self
    # extraction at every start is exactly what G2 removes.
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onedir", "--windowed", "--noconfirm",
           "--name", APP_NAME,
           "--distpath", str(distpath),
           "--icon", str(ROOT / "assets/icon.ico"),
           "--runtime-hook", str(ROOT / "runtime_hook_utf8.py")]

    # 1) 所有根目录 .py 一律随包（动态 import 的兜底，详见文件头说明）
    for name in py_sources():
        cmd += add_data_pair(name, ".")

    # 2) 静态资源
    for src, dst in STATIC_DATA:
        cmd += add_data_pair(src, dst)

    # 3) 依赖包目录。dry-run 可能在没有装依赖的机器上执行，此时用占位符
    #    保留参数结构，真机构建（resolve_deps=True）缺包则直接报错。
    for mod in COLLECT_DIRS:
        if resolve_deps:
            cmd += add_data_pair(str(pkg_dir(mod)), mod)
        else:
            cmd += add_data_pair(f"<依赖目录:{mod}>", mod)
    for mod in COLLECT_ALL:
        cmd += ["--collect-all", mod]
    for mod in COLLECT_DATA:
        cmd += ["--collect-data", mod]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDE_MODULES:
        cmd += ["--exclude-module", mod]

    cmd.append(str(ROOT / "app_webview.py"))
    return cmd


def dep_versions() -> dict:
    out = {}
    for name, mod in [("python", None), ("pyinstaller", "PyInstaller"),
                      ("openvino", "openvino"), ("onnxruntime", "onnxruntime"),
                      ("numpy", "numpy"), ("pywebview", "webview")]:
        try:
            if mod is None:
                out[name] = sys.version.split()[0]
            else:
                m = importlib.import_module(mod)
                out[name] = getattr(m, "__version__", "未知")
        except Exception as e:
            out[name] = f"未取得（{type(e).__name__}）"
    return out


def readme_text(version: str, sha: str, has_ffmpeg: bool) -> str:
    ffmpeg_line = (
        "本包已随附 ffmpeg.exe，处理 MP4 / MKV 等视频可直接抽取音轨，无需联网。"
        if has_ffmpeg else
        "本包未随附 ffmpeg.exe：处理 WAV 不需要它；首次处理 MP4/MKV 等视频时，\n"
        "程序会自动下载 ffmpeg（约 100MB）。也可手动把 ffmpeg.exe 放到 ffmpeg\\ 目录。")
    return f"""QwenASR 长音频流式版 · Windows 使用说明
=========================================
版本：{version}
构建提交：{sha}
构建时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}

【这是什么】
基于 Qwen3-ASR 的纯 CPU 字幕工具，使用 OpenVINO INT8 权重推理。
不依赖 PyTorch / transformers，不需要 NVIDIA 显卡；Intel 集显机器按纯 CPU 运行。
已集成长音频流式模式：分段增量落盘、断点续跑、取消保留已完成字幕。

【怎么用】
1. 解压整个文件夹到任意位置（路径尽量不要带中文和空格）。
2. 双击 QwenASR-WebView.exe 启动。无需安装 Python。
   窗口使用系统自带的 Microsoft Edge（--app 无痕模式），不依赖 .NET、
   WebView2 SDK 或 pythonnet；因此也不会出现「文件带网络标记导致组件拒绝加载」
   的问题。若系统没有 Edge，会自动回退到默认浏览器。
3. 首次使用需要在「模型」页下载识别模型：
     · 0.6B  INT8：约 1.2GB，速度快，内存占用低
     · 1.7B  INT8：约 4.4GB，中文准确率更高，段长上限 10 秒
   下载会存到本目录 ov_models\\ 下。国内网络可在设置页把下载源切到
   hf-mirror.com 镜像。也可以离线把现成 IR 放到：
     ov_models\\qwen3_asr_int8\\            （0.6B）
     ov_models\\qwen3_asr_1p7b_kv_int8\\    （1.7B）
   「模型」页下方的下载中心会列出每组模型需要哪些文件、各自状态与校验结果；
   点「输出下载清单」可得到一份 .txt，含完整链接与可直接粘贴的 aria2c 命令，
   弱网时可先用专业下载器下好，再用「导入并验证」放进来。未实测的大小与
   SHA256 会明确显示「未知」，不会用估计值冒充。
4. 把网课音频/视频拖进界面开始转写，字幕输出到 subtitles\\。

【为什么模型没有内嵌】
INT8 权重单份就有 1.2~4.4GB，内嵌会让安装包变成数 GB，且不同用户需要的
精度不同。因此保留程序内「按需下载」机制，安装包装的只有程序本体与小型资源
（前端、Mel 滤波器、Silero VAD 模型）。装好模型之后即可完全离线使用。

【ffmpeg】
{ffmpeg_line}

【长音频（1~5 小时网课）】
所有输入统一走同一条流式路径：边读边分段、每组立刻写 SRT，不再按时长
区分两套逻辑；中途取消或崩溃都不丢已完成字幕，对同一文件再次开始可从
断点续跑。断点与逐段记录保存在 longform_state\\。取消与续跑都在语音组
边界生效，结果可与完整跑逐条一致。
如需从头重跑（源码版）：
    python app_webview.py transcribe 课程.mp4 --no-resume --json
注意：本 EXE 是无控制台的窗口版，命令行子命令请用源码版运行。

识别页可勾选「只试跑前 5 分钟」先验准确率（另存 .trial.srt，不影响正式任务），
也可先跑「切分预检」（同样只看前 5 分钟、不跑识别）确认 VAD 参数是否合理。
切分长度只影响上下文完整度与总耗时，不影响单段内存；内存主要由模型决定
（1.7B 约 6GB 常驻，系统另计约 1~2GB 的浏览器与文件缓存）。
1.7B 的段长上限是 10 秒，填更大值会被夹紧并在界面显示实际生效值。

流式路径的功能边界：词级时间轴（逐字高亮）、说话人分离、ForcedAligner
精确对齐暂不支持，界面会把这几个开关置灰说明；短音频同样走流式路径。

【关于自动更新】
本仓库是 dseditor/QwenASRMiniTool 的 fork。程序内「检查更新」已指向本仓库
（1-1-2/QwenASRGUI）的 Releases 页面，不会再用上游包覆盖本版本。

【目录】
  QwenASR-WebView.exe   主程序（启动器，只负责拉起程序本体）
  _internal\\             程序本体、依赖库与前端资源（请勿删除或单独移动）
  ffmpeg\\                可选，随附的 ffmpeg.exe
  ov_models\\             模型权重目录（首次启动时自动填充）
  subtitles\\             字幕输出目录
  longform_state\\        长音频断点与逐段记录（运行时生成）
  settings.json          设置（运行时生成）
  build_manifest.json    本包的构建清单与依赖版本
  使用说明.txt           本文件

【为什么是文件夹而不是单个 EXE】
单文件版每次启动都要把约 200MB 解压到临时目录，更新时也要整包替换。
分层目录版启动时不再解压，依赖稳定；你的设置、模型、字幕、断点全部留在
上面这些顶层目录里，以后换新版只需要替换 QwenASR-WebView.exe 与
_internal\\ 两项，其余照旧即可沿用。整个文件夹请一起移动或改名，
不要只拷走 QwenASR-WebView.exe。
"""


def onedir_output(distpath: Path) -> Path:
    """PyInstaller --onedir 的产物目录：<distpath>/<APP_NAME>/。"""
    return distpath / APP_NAME


def make_package(version: str, sha: str, ffmpeg_exe: Path | None) -> Path:
    distpath = ROOT / "dist" / "webview"
    produced = onedir_output(distpath)
    exe = produced / f"{APP_NAME}.exe"
    if not exe.exists():
        raise FileNotFoundError(f"未找到构建产物：{exe}")
    internal = produced / "_internal"
    if not internal.is_dir():
        raise FileNotFoundError(f"分层目录缺失：{internal}")

    stage = ROOT / "release_pkg" / APP_NAME
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Launcher on top, everything it needs under _internal/. Copies the whole
    # onedir tree so a missing DLL shows up here instead of at first launch.
    shutil.copytree(produced, stage, dirs_exist_ok=True)

    has_ffmpeg = False
    if ffmpeg_exe and Path(ffmpeg_exe).exists():
        (stage / "ffmpeg").mkdir(exist_ok=True)
        shutil.copy2(ffmpeg_exe, stage / "ffmpeg" / "ffmpeg.exe")
        has_ffmpeg = True

    for d in ("ov_models", "subtitles", "longform_state"):
        (stage / d).mkdir(exist_ok=True)
        (stage / d / ".keep").write_text("", encoding="utf-8")

    (stage / "使用说明.txt").write_text(
        readme_text(version, sha, has_ffmpeg), encoding="utf-8")

    manifest = {
        "版本": version,
        "提交": sha,
        "构建时间": datetime.datetime.now().isoformat(timespec="seconds"),
        "平台": "windows-x64",
        "打包方式": "PyInstaller onedir / windowed（分层：EXE 在根，依赖在 _internal/）",
        "EXE 体积": exe.stat().st_size,
        "_internal 体积": dir_size(internal),
        "随附 ffmpeg": has_ffmpeg,
        "随附 Python 源码": py_sources(),
        "静态资源": [src for src, _ in STATIC_DATA],
        "依赖版本": dep_versions(),
        "说明": "模型权重（OpenVINO INT8 IR）不内嵌，首次启动按需下载；详见使用说明.txt",
    }
    (stage / "build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    zip_path = ROOT / "release_pkg" / f"{APP_NAME}_{version}_windows-x64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path(APP_NAME) / p.relative_to(stage))
    return zip_path


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# Files that must exist inside the staged package for the build to be usable.
# Paths are relative to the package root. The first entry proves the launcher
# was produced; everything below proves the layered payload actually landed
# under _internal/ (a onedir regression would otherwise surface as an
# ImportError on first use, not at build time).
REQUIRED_FILES = [
    f"{APP_NAME}.exe",
    "_internal/longform_runner.py",
    "_internal/downloader.py",
    "_internal/models.json",
    "_internal/prompt_template.json",
    "_internal/webview_assets/index.html",
    "_internal/ov_models/silero_vad_v4.onnx",
]


def verify_onedir(stage: Path) -> dict:
    """Assert the onedir package has a launcher plus its layered payload.

    Replaces the old CArchive inspection: in onedir the payload is loose files,
    so checking the filesystem is both simpler and stronger than parsing an
    archive table.
    """
    result: dict = {"方式": "onedir 目录文件检查", "必需条目": REQUIRED_FILES,
                    "命中": [], "缺失": [], "状态": "未验证"}
    for rel in REQUIRED_FILES:
        p = stage / rel
        ok = p.is_file() and (rel.endswith(".exe") or p.stat().st_size > 0)
        (result["命中"] if ok else result["缺失"]).append(rel)
    internal = stage / "_internal"
    result["_internal 文件数"] = (sum(1 for p in internal.rglob("*") if p.is_file())
                                  if internal.is_dir() else 0)
    result["状态"] = "通过" if not result["缺失"] else "存在缺失"
    return result


def _force_utf8():
    """Windows 控制台默认 cp1252 会让中文 print 抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def main() -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="构建 Windows 发行包（简体中文）")
    ap.add_argument("--version", default="dev", help="版本标签，写入文件名与说明")
    ap.add_argument("--sha", default="local", help="构建对应的 commit 短哈希")
    ap.add_argument("--ffmpeg", default="", help="随包附带的 ffmpeg.exe 路径")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印 PyInstaller 命令与源码清单，不实际构建")
    ap.add_argument("--skip-build", action="store_true",
                    help="跳过 PyInstaller，仅用已有 EXE 组装 ZIP（调试用）")
    args = ap.parse_args()

    print(f"源码目录：{ROOT}")
    print(f"随包 .py 共 {len(py_sources())} 个：")
    for n in py_sources():
        print(f"   {n}")

    cmd = build_command(ROOT / "dist" / "webview",
                        resolve_deps=not (args.dry_run or args.skip_build))
    print("\nPyInstaller 参数条目数：", len(cmd))
    if args.dry_run:
        print("\n=== dry-run 命令 ===")
        print(" ".join(f'"{x}"' if " " in x else x for x in cmd))
        print("\n=== 静态资源检查 ===")
        for src, _ in STATIC_DATA:
            p = ROOT / src
            print(f'  {"存在" if p.exists() else "缺失"}  {src}')
        return 0

    if not args.skip_build:
        (ROOT / "dist" / "webview").mkdir(parents=True, exist_ok=True)
        print("\n开始 PyInstaller 构建……")
        subprocess.run(cmd, check=True, cwd=str(ROOT))

    produced = onedir_output(ROOT / "dist" / "webview")
    exe = produced / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"构建失败：未找到 {exe}")
        return 1
    print(f"启动器 EXE 大小：{exe.stat().st_size / 1024 / 1024:.1f} MB")
    internal = produced / "_internal"
    if internal.is_dir():
        print(f"_internal 体积：{dir_size(internal) / 1024 / 1024:.1f} MB")

    ff = Path(args.ffmpeg) if args.ffmpeg else None
    zip_path = make_package(args.version, args.sha, ff)
    print(f"\n发行包：{zip_path}")
    print(f"大小：{zip_path.stat().st_size / 1024 / 1024:.1f} MB")

    # 校验的是**最终要发出去的目录**，不是构建中间产物：分层漏文件只有在这里
    # 才暴露得出来（onedir 没有 CArchive 目录表可读）。
    verify = verify_onedir(ROOT / "release_pkg" / APP_NAME)
    print("\n=== 包内容验证 ===")
    print(f"  状态：{verify['状态']}  _internal 文件数：{verify['_internal 文件数']}")
    if verify["缺失"]:
        print("  缺失：", verify["缺失"])
    if verify["命中"]:
        print("  命中：", ", ".join(verify["命中"]))

    # 把验证结果也写进 manifest，便于 CI 断言
    stage_manifest = ROOT / "release_pkg" / APP_NAME / "build_manifest.json"
    data = json.loads(stage_manifest.read_text(encoding="utf-8"))
    data["包内容验证"] = verify
    stage_manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    (ROOT / "release_pkg" / "archive_verify.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=2), encoding="utf-8")

    if verify["缺失"]:
        print("包内容存在缺失，构建判为失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
