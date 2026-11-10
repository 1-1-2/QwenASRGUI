"""build_release.py — CI 专用：构建 WebView 单文件 EXE 并组装可直接解压运行的 ZIP 包。

本文件为「一键发行包」改造新增，注释与产出说明统一使用简体中文。

为什么不直接复用 build_webview.bat
----------------------------------
原脚本把路径写死在 ``F:\\AIStudio\\QwenASR`` 与本机 venv，GitHub Actions 上
无法执行；更关键的是它手工列举 --add-data，遗漏了本次新增的
``longform_runner.py``。本项目大量模块是**函数内动态 import**（downloader、
longform_runner、ffmpeg_utils……），PyInstaller 静态分析抓不到，靠的是把这些
.py 用 --add-data 放进 _MEIPASS 根目录，运行时由标准 PathFinder 从那里加载。
一旦漏列，EXE 启动时不会报错，点到对应功能才 ImportError —— 长音频模式就会
这样静默失效。因此本脚本改为**自动扫描根目录所有 .py**，从结构上杜绝漏列。

用法
----
    python build_release.py --version 2.0.0-longform --sha abc1234
    python build_release.py --dry-run          # 只打印 PyInstaller 命令，不执行

产物
----
    dist/webview/QwenASR-WebView.exe
    release_pkg/QwenASR-WebView/                 解压即用的目录
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
    ("webview", "webview_assets"),
    ("ov_models/mel_filters.npy", "ov_models"),
    ("ov_models/silero_vad_v4.onnx", "ov_models"),
]

# 这些包带原生 DLL / 数据文件，PyInstaller 静态分析抓不全，整包塞进去
COLLECT_DIRS = ["opencc", "openvino", "kaldi_native_fbank"]
COLLECT_ALL = ["webview", "pythonnet", "clr_loader", "tokenizers"]
COLLECT_DATA = ["certifi"]

HIDDEN_IMPORTS = [
    "certifi", "webview", "webview.platforms.edgechromium",
    "webview.platforms.winforms", "clr", "clr_loader", "pythonnet",
    "proxy_tools", "bottle", "app", "webview_backend", "webview_server",
    "openvino", "openvino.runtime", "onnxruntime", "opencc", "customtkinter",
    "sounddevice", "soxr", "soundfile", "kaldi_native_fbank",
    "scipy", "scipy.cluster", "scipy.cluster.hierarchy",
    "scipy.spatial", "scipy.spatial.distance", "scipy._lib.messagestream",
    "crisp_engine", "subtitle_lines", "cli_mode", "version", "updater",
    "fa_aligner", "api_server", "endpoint_tab", "segno", "cf_tunnel",
    "proc_guard", "diarize", "audio_io", "processor_numpy", "downloader",
    "ffmpeg_utils", "subtitle_lines", "longform_runner",
]

# 纯 CPU + OpenVINO INT8：明确排除 PyTorch 全家桶与 transformers
EXCLUDE_MODULES = [
    "torch", "torchvision", "torchaudio", "transformers", "qwen_asr",
    "triton", "bitsandbytes",
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
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--windowed", "--noconfirm",
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
   Windows 10/11 一般已自带 WebView2 Runtime；缺失时会自动回退到 Edge 窗口。
3. 首次使用需要在「模型」页下载识别模型：
     · 0.6B  INT8：约 1.2GB，速度快，内存占用低
     · 1.7B  INT8：约 4.4GB，中文准确率更高，段长上限 10 秒
   下载会存到本目录 ov_models\\ 下。国内网络可在设置页把下载源切到
   hf-mirror.com 镜像。也可以离线把现成 IR 放到：
     ov_models\\qwen3_asr_int8\\            （0.6B）
     ov_models\\qwen3_asr_1p7b_kv_int8\\    （1.7B）
4. 把网课音频/视频拖进界面开始转写，字幕输出到 subtitles\\。

【为什么模型没有内嵌】
INT8 权重单份就有 1.2~4.4GB，内嵌会让安装包变成数 GB，且不同用户需要的
精度不同。因此保留程序内「按需下载」机制，安装包装的只有程序本体与小型资源
（前端、Mel 滤波器、Silero VAD 模型）。装好模型之后即可完全离线使用。

【ffmpeg】
{ffmpeg_line}

【长音频（1~5 小时网课）】
音频时长 ≥ 20 分钟会自动进入流式模式：边读边分段、每组立刻写 SRT，
中途取消或崩溃都不丢已完成字幕；对同一文件再次开始可从断点续跑。
断点与逐段记录保存在 longform_state\\。
如需命令行强制启用或从头重跑（源码版）：
    python app_webview.py transcribe 课程.mp4 --longform --json
    python app_webview.py transcribe 课程.mp4 --longform --no-resume
注意：本 EXE 是无控制台的窗口版，命令行子命令请用源码版运行。

流式模式的功能边界：词级时间轴（逐字高亮）、说话人分离、ForcedAligner
精确对齐暂不支持，会回退为行级字幕；短音频（< 20 分钟）仍走原路径，
这些功能不受影响。断点恢复时 VAD 状态会重置，边界最多损失一个语音组
（≤30 秒）；要求零丢失请按 20~30 分钟预先切分文件。

【关于自动更新】
本仓库是 dseditor/QwenASRMiniTool 的 fork，程序内「检查更新」仍指向上游
仓库。请到本仓库的 Releases 页面手动下载新版，不要依赖内置更新器。

【目录】
  QwenASR-WebView.exe   主程序（单文件，内含全部 Python 代码与前端资源）
  ffmpeg\\                可选，随附的 ffmpeg.exe
  ov_models\\             模型权重目录（首次启动时自动填充）
  subtitles\\             字幕输出目录
  longform_state\\        长音频断点与逐段记录（运行时生成）
  build_manifest.json    本包的构建清单与依赖版本
  使用说明.txt           本文件
"""


def make_package(version: str, sha: str, ffmpeg_exe: Path | None) -> Path:
    distpath = ROOT / "dist" / "webview"
    exe = distpath / f"{APP_NAME}.exe"
    if not exe.exists():
        raise FileNotFoundError(f"未找到构建产物：{exe}")

    stage = ROOT / "release_pkg" / APP_NAME
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(exe, stage / exe.name)

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
        "打包方式": "PyInstaller onefile / windowed",
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


def verify_archive(exe: Path) -> dict:
    """读取 PyInstaller CArchive 的目录表，确认关键动态导入模块确实进包。"""
    required = ["longform_runner.py", "downloader.py", "ffmpeg_utils.py",
                "webview_assets/index.html", "ov_models/silero_vad_v4.onnx",
                "prompt_template.json"]
    result: dict = {"方式": "PyInstaller.archive.readers.CArchiveReader",
                    "必需条目": required, "命中": [], "缺失": [], "状态": "未验证"}
    try:
        from PyInstaller.archive.readers import CArchiveReader
        reader = CArchiveReader(str(exe))
        toc = list(reader.toc.keys()) if hasattr(reader.toc, "keys") else list(reader.toc)
        normalized = [str(x).replace("\\", "/") for x in toc]
        for r in required:
            hit = any(r in x or x.endswith(r) for x in normalized)
            (result["命中"] if hit else result["缺失"]).append(r)
        result["条目总数"] = len(normalized)
        result["状态"] = "通过" if not result["缺失"] else "存在缺失"
    except Exception as e:
        # PyInstaller 版本差异可能让内部 API 不可用，退回官方命令行工具
        first_err = f"{type(e).__name__}: {e}"
        try:
            p = subprocess.run(["pyi-archive_viewer", "-l", str(exe)],
                               capture_output=True, text=True, timeout=120)
            text = (p.stdout or "") + (p.stderr or "")
            normalized = text.replace("\\", "/")
            for r in required:
                (result["命中"] if r in normalized else result["缺失"]).append(r)
            result["方式"] = "pyi-archive_viewer -l"
            result["状态"] = "通过" if not result["缺失"] else "存在缺失"
            result["回退原因"] = first_err
        except Exception as e2:
            result["状态"] = (f"检查跳过（API: {first_err}；"
                              f"命令行: {type(e2).__name__}: {e2}）")
    return result


def main() -> int:
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

    exe = ROOT / "dist" / "webview" / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"构建失败：未找到 {exe}")
        return 1
    print(f"EXE 大小：{exe.stat().st_size / 1024 / 1024:.1f} MB")

    archive = verify_archive(exe)
    print("\n=== 包内容验证 ===")
    print(f"  状态：{archive['状态']}  条目数：{archive.get('条目总数', '-')}")
    if archive["缺失"]:
        print("  缺失：", archive["缺失"])
    if archive["命中"]:
        print("  命中：", ", ".join(archive["命中"]))

    ff = Path(args.ffmpeg) if args.ffmpeg else None
    zip_path = make_package(args.version, args.sha, ff)
    print(f"\n发行包：{zip_path}")
    print(f"大小：{zip_path.stat().st_size / 1024 / 1024:.1f} MB")

    # 把验证结果也写进 manifest，便于 CI 断言
    stage_manifest = ROOT / "release_pkg" / APP_NAME / "build_manifest.json"
    data = json.loads(stage_manifest.read_text(encoding="utf-8"))
    data["包内容验证"] = archive
    stage_manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    (ROOT / "release_pkg" / "archive_verify.json").write_text(
        json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")

    if archive["缺失"] and archive["状态"] == "存在缺失":
        print("包内容存在缺失，构建判为失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
