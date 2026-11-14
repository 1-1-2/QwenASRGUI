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

# ══════════════════════════════════════════════════════════════════════════
# 体积精简（Wave 7）：删掉确定用不到的 OpenVINO 组件
# ══════════════════════════════════════════════════════════════════════════
# 为什么能删：本项目只用 OpenVINO 的「IR 前端 + CPU 插件」。
#   core.compile_model("<模型>.xml", "CPU") —— 模型是 OpenVINO IR（.xml/.bin），
#   设备恒为 CPU（app.py 里 device 只取 "CPU"；GPU 只用于「探测并列出」展示）。
# 因此下面这些在 Windows 上占 134.8 MB 解包体积里的绝大部分，都是死重：
#   openvino_intel_gpu_plugin.dll      30.9 MB  不推理 iGPU
#   openvino_intel_npu_plugin.dll       4.0 MB  不推理 NPU
#   openvino_tensorflow_frontend.dll    4.1 MB  不加载 TF 模型
#   openvino_onnx_frontend.dll          4.0 MB  不加载 ONNX 模型（onnxruntime 负责）
#   openvino_pytorch_frontend.dll       2.6 MB  不加载 PyTorch 模型
#   openvino_paddle_frontend.dll        1.5 MB
#   openvino_tensorflow_lite_frontend    1.2 MB
#   openvino_jax_frontend.dll           0.5 MB
#   *.lib（导入库，只在编译期用）        7.7 MB
#   cache.json                          8.8 MB  设备能力缓存，首次启动会重建
# 保留：openvino.dll、openvino_intel_cpu_plugin.dll、openvino_ir_frontend.dll、
#       openvino_auto_plugin / hetero / batch 等被 openvino.dll 依赖的插件。
#
# 风险与兜底：删多了会让 import openvino 直接失败 —— 那不是「少个功能」而是
# 「整个程序起不来」。所以：
#   1) 只在**确认 CPU 插件与 IR 前端仍在**时才执行删除；
#   2) 保留清单在这里集中声明，便于审阅；
#   3) tools/verify_package.py 会断言 CPU 插件存在、GPU/NPU 插件不存在；
#   4) 提供 --no-slim 一键回退到完整体积。
# 注意 "*intel_npu_*" 不能只写 "*intel_npu_plugin*"：2026.x 的 wheel 里
# openvino_intel_npu_compiler.dll 单个就 96 MB，比整个 CPU 插件还大。
OPENVINO_DROP_PATTERNS = [
    "*intel_gpu_*", "*intel_npu_*",
    "*tensorflow_frontend*", "*onnx_frontend*", "*pytorch_frontend*",
    "*paddle_frontend*", "*tensorflow_lite_frontend*", "*jax_frontend*",
    "*gguf_frontend*",
    "*.lib", "*.pdb", "cache.json",
]
OPENVINO_MUST_KEEP = [
    "openvino_intel_cpu_plugin.dll", "openvino.dll", "openvino_ir_frontend.dll",
]

# Python 侧的死重：被 openvino 包带进来的 OpenVINO 自己的前端 Python 绑定。
OPENVINO_DROP_SUBDIRS = ["frontend"]
# 纯 C++ 头文件目录（OneAPI TBB 等），运行时零用途。
OPENVINO_DROP_HEADERS = ["include"]

# 运行时完全用不到的纯开发物：C++ 头文件（include/，约 5 MB）与类型存根
# （*.pyi，约 1 MB）。两者只在编译/写代码时被读取，import 不会碰。
OPENVINO_DROP_PYI = True


def slim_openvino(internal: Path) -> dict:
    """删除 OpenVINO 里用不到的组件，返回删除清单与节省字节数。"""
    ov_dir = internal / "openvino"
    result = {"启用": True, "删除": [], "失败": [], "节省字节": 0, "跳过原因": ""}
    if not ov_dir.is_dir():
        result["跳过原因"] = "未找到 _internal/openvino"
        return result

    libs = ov_dir / "libs"
    names_under_libs = {p.name for p in libs.glob("*")} if libs.is_dir() else set()
    missing = [n for n in OPENVINO_MUST_KEEP if n not in names_under_libs]
    if missing:
        # 认不出布局就当不认识这个包 —— 宁可不动，也不要打出一个坏的包。
        result["跳过原因"] = f"必需组件缺失，放弃精简：{missing}"
        return result

    # 全树搜索，不只 openvino/libs/：PyInstaller 在解析 --hidden-import openvino 时
    # 会按二进制依赖把 libs/*.dll 另拷一份到 _internal/ 根（与 --add-data 的那份并存），
    # 只删带目录的那一份会留下一整套 GPU/NPU 插件 —— 这正是 2026-11-11 那次
    # Windows 构建报「未精简」的原因：精简确实删了 74.5 MB，但另一份没动。
    for pattern in OPENVINO_DROP_PATTERNS:
        for p in list(internal.rglob(pattern)):
            if p.name in OPENVINO_MUST_KEEP or not p.is_file():
                continue
            try:
                size = p.stat().st_size
                p.unlink()
                rel = p.relative_to(internal).as_posix()
                result["删除"].append(rel)
                result["节省字节"] += size
            except OSError as exc:
                result["失败"].append(f"{p.name}: {exc}")
    # include/ 与 .pyi 用嵌套 glob 处理：真实 wheel 里它们不止一级目录。
    for sub in OPENVINO_DROP_SUBDIRS + OPENVINO_DROP_HEADERS:
        for d in list(internal.glob(f"**/{sub}")):
            if d.is_dir():
                try:
                    result["节省字节"] += dir_size(d)
                    shutil.rmtree(d)
                    result["删除"].append(f"{d.relative_to(internal).as_posix()}/")
                except OSError as exc:
                    result["失败"].append(f"{d.name}/: {exc}")
    if OPENVINO_DROP_PYI:
        for p in list((internal / "openvino").rglob("*.pyi")) if ov_dir.is_dir() else []:
            try:
                size = p.stat().st_size
                p.unlink()
                result["删除"].append(p.name)
                result["节省字节"] += size
            except OSError as exc:
                result["失败"].append(f"{p.name}: {exc}")
    return result

HIDDEN_IMPORTS = [
    "certifi", "proxy_tools", "bottle", "app", "webview_backend", "webview_server",
    "openvino", "openvino.runtime", "onnxruntime", "opencc",
    "sounddevice", "soxr", "soundfile", "kaldi_native_fbank",
    # SciPy 已整体移除（2026-11）：说话人聚类改用 cluster_core.py（自带
    # average-linkage，与 scipy 输出逐位对齐），重采样改用频域低通。
    # 这两处一改，69 MB 的 scipy/ + scipy.libs/ 就彻底不必进包了。
    "cluster_core",
    "crisp_engine", "subtitle_lines", "cli_mode", "version", "updater",
    "fa_aligner", "api_server", "segno", "cf_tunnel",
    "proc_guard", "diarize", "audio_io", "processor_numpy", "downloader",
    "ffmpeg_utils", "model_catalog", "longform_runner", "app_paths",
    "batch_queue", "single_instance", "run_log", "subtitle_extras",
    "release_manifest",
    # 2026-11 啟動崩潰（TypeError: 'NoneType' object is not callable）之後補齊：
    # 這些是專案內模組，過去只靠「靜態可追蹤」被收進包裡，任何一個漏收都會讓
    # webview_backend 的匯入鏈斷掉，而斷點在 app_webview 的 except 裡被吞成
    # WebViewServer = None。逐一明列，讓打包結果不再取決於可追蹤性。
    "ui_text", "postprocess", "chatllm_engine",
]
# customtkinter was removed from HIDDEN_IMPORTS on purpose (Wave 7): the WebView
# UI is HTML, and `app.py` now guards its Tk GUI behind QWENASR_WEBVIEW_BUILD.
# Keeping a hidden import here would force tkinter + tcl/tk back into the ZIP.

# CPU + OpenVINO INT8 only. Exclude PyTorch / transformers, and the .NET
# window stack that is no longer used by the default Edge --app path.
EXCLUDE_MODULES = [
    "torch", "torchvision", "torchaudio", "transformers", "qwen_asr",
    "triton", "bitsandbytes",
    # 显式排除，别指望「没人 import 就不会被收」：numpy 的 hook 与若干
    # 三方包的 __init__ 都可能顺手把 scipy 拖进来（会悄悄多出 69 MB）。
    "scipy",
    "webview", "webview.platforms", "pythonnet", "clr", "clr_loader",
    # Wave 7: the shipped UI is HTML. app.py keeps its Tk GUI behind
    # QWENASR_WEBVIEW_BUILD, and these excludes are the belt to that braces:
    # if anything ever imports tkinter again, the build fails loudly instead of
    # silently adding ~20 MB of tcl/tk to every download.
    "tkinter", "_tkinter", "customtkinter", "darkdetect",
    # 这三个是 Tk 界面专用分页（endpoint_tab 在模块顶层 import tkinter /
    # customtkinter）。它们过去靠 HIDDEN_IMPORTS 被强行收进来，结果把 tkinter
    # 一起拖进包 —— 而 tkinter 又被上面排除掉，于是打包版一启动就死在
    #   RuntimeError: 无法载入核心模块：No module named 'tkinter'
    # （2026-11-11 由 tools/smoke_test.py 的整包冒烟测试抓到）。
    # WebView 版不需要它们：端点功能已抽出为 cf_tunnel / api_server。
    "endpoint_tab", "setting", "subtitle_editor", "batch_tab", "model_tab",
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
     · 1.7B  INT8：约 4.4GB，中文准确率更高，段长上限 20 秒
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
（另有一個不含 ffmpeg 的 -noffmpeg 變體，體積更小；功能相同，只是首次處理
  影片時要自行下載 ffmpeg。WAV 使用者選那個即可。）

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
切分长度只影响上下文完整度与总耗时；内存主要由模型决定
（1.7B 约 6GB 常驻，系统另计约 1~2GB 的浏览器与文件缓存）。
1.7B 的段长上限 20 秒（0.6B 为 30 秒），填更大值会被夹紧并在界面显示实际生效值。

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
  subtitles\\             正式字幕输出目录（每次识别完成自动写入）
  longform_state\\        长音频断点、逐段记录与批量上传中转（运行时生成）
  settings.json          设置（运行时生成）
  build_manifest.json    本包的构建清单与依赖版本
  使用说明.txt           本文件

  说明：「字幕存档」按钮是另外导出一份到浏览器的下载目录（通常是 下载\\），
  不影响 subtitles\\ 里的正式文件。

【出问题了看哪里】
启动器日志：%LOCALAPPDATA%\\聲音辨識小工具\\launcher.log
  把这段路径粘到资源管理器地址栏即可打开。启动失败、闪退、窗口打不开时
  会写在这里（单文件上限 1MB，超了自动改名成 launcher.log.1 保留上一份）。
  每行开头都有 [月-日 时:分:秒.毫秒]，每次启动另有一条
  「===== 启动 <日期> PID <n> =====」分隔线。
  每次启动还会在同目录单独留一份 launcher-<年月日-时分秒>-p<PID>.log，
  只含那一次的完整记录，保留最近 12 份。反馈问题时直接把最新那份附上即可，
  不用在混合日志里挑段落。
  ※ 这是本程序**唯一**会写到程序目录以外的地方；设置、模型、字幕、断点
     都还在程序目录里，方便整个文件夹搬走。
Edge 的窗口配置在同一個資料夾的 edge-profile\\（即
  %LOCALAPPDATA%\\聲音辨識小工具\\edge-profile\\），与日常 Edge 完全隔离；
  删掉它只会重置窗口状态，不影响任何数据。

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


def make_package(version: str, sha: str, ffmpeg_exe: Path | None,
                 slim: bool = True, variant: str = "") -> Path:
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

    # Trim OpenVINO before the ZIP is written: the deletion has to happen on
    # the staged tree, otherwise the savings never reach the download.
    #
    # 这里必须用 stage_internal（打包目录里的那份），不能用上面的 `internal`
    # ——那是 dist/ 里的构建中间产物。传错会「精简报告很好看、最终包一个字节
    # 都没少」，2026-11-11 的三次 Windows 构建都是这么失败的。
    stage_internal = stage / "_internal"
    slim_report = {"启用": False, "删除": [], "失败": [], "节省字节": 0, "跳过原因": "--no-slim"}
    if slim:
        slim_report = slim_openvino(stage_internal)
        print(f"\nOpenVINO 精简：删除 {len(slim_report['删除'])} 项，"
              f"省 {slim_report['节省字节'] / 1024 / 1024:.1f} MB"
              + (f"（跳过：{slim_report['跳过原因']}）" if slim_report["跳过原因"] else ""))
        if slim_report["失败"]:
            print("  删除失败：", slim_report["失败"][:5])

    # 精简作用对象自检：在最终目录里再数一遍，删不动就直接失败。
    # 这条曾经被漏掉，代价是三次构建才发现「报告说省了 176 MB，包里没变」。
    if slim and slim_report.get("启用"):
        leftover = [n for pat in OPENVINO_DROP_PATTERNS
                    for n in (p.name for p in stage_internal.rglob(pat))
                    if n not in OPENVINO_MUST_KEEP]
        if leftover:
            raise RuntimeError(
                "精简未作用到最终目录，仍残留：" + ", ".join(sorted(set(leftover))[:5]))

    # SciPy 不许出现在包里。它只服务两处（说话人聚类、重采样），现都已替换；
    # 万一被 numpy hook 或某个三方包的 __init__ 间接拉进来，就是白送 69 MB，
    # 而且不会有任何功能变化能让人察觉 —— 所以在打包端直接拦死。
    scipy_left = [p.name for p in stage_internal.iterdir()
                  if p.name.startswith("scipy")] if stage_internal.is_dir() else []
    if scipy_left:
        raise RuntimeError(
            "包内出现 SciPy（应由 EXCLUDE_MODULES 拦住）：" + ", ".join(scipy_left))

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
        # 必须统计已精简的发行目录；stat(dist) 会把删掉的 176 MB 又算回来
        "_internal 体积": dir_size(stage_internal),
        "随附 ffmpeg": has_ffmpeg,
        # 变体名必须能与「随附 ffmpeg」自洽：full 意味着「带了 ffmpeg」。
        # CI 用 bundle_ffmpeg=false 触发时，主包本身就是 noffmpeg 内容，却仍走
        # variant="" 这条路径；若直接回落成 "full"，manifest 会出现
        # {"随附 ffmpeg": false, "变体": "full"} 这种自相矛盾的记录，
        # 下游（下载中心、故障排查、用户比对大小）就无法从 manifest 判断
        # 「这个包为什么没有 ffmpeg」。所以没带 ffmpeg 一律记 noffmpeg。
        "变体": variant or ("full" if has_ffmpeg else "noffmpeg"),
        "OpenVINO 精简": {
            "启用": slim_report["启用"],
            "节省 MB": round(slim_report["节省字节"] / 1024 / 1024, 1),
            "删除项数": len(slim_report["删除"]),
            "失败": slim_report["失败"],
            "跳过原因": slim_report["跳过原因"],
        },
        "体积构成": size_breakdown(stage_internal),
        "随附 Python 源码": py_sources(),
        "静态资源": [src for src, _ in STATIC_DATA],
        "依赖版本": dep_versions(),
        "说明": "模型权重（OpenVINO INT8 IR）不内嵌，首次启动按需下载；详见使用说明.txt",
    }
    (stage / "build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # G3: content-addressed manifest over the managed payload only. It must be
    # written BEFORE the ZIP is assembled so it ships inside the package.
    from release_manifest import build_manifest
    build_manifest(stage, version, sha)

    zip_name = f"{APP_NAME}_{version}_windows-x64" + (f"-{variant}" if variant else "")
    zip_path = ROOT / "release_pkg" / f"{zip_name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    # ZIP 内的根目录名带上版本：用户同时留着 full / noffmpeg 两个包解压到
    # 同一处时不会互相覆盖（两个包的内层目录原本完全同名）。
    arc_root = f"{APP_NAME}_{version}_windows-x64" + (f"-{variant}" if variant else "")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                z.write(p, Path(arc_root) / p.relative_to(stage))
    return zip_path


def size_breakdown(internal: Path, top: int = 14) -> list[dict]:
    """把 _internal/ 拆成「谁占了体积」，供排查「能不能再瘦」时直接看。

    只做统计，不做判断：哪些能删是另一回事（`slim_openvino` 里有白名单保护）。
    Python 包按顶层目录/模块聚合，其余按文件。
    """
    rows: list[tuple[str, int]] = []
    if not internal.is_dir():
        return []
    for item in internal.iterdir():
        try:
            if item.is_dir():
                rows.append((item.name + "/", dir_size(item)))
            elif item.is_file():
                rows.append((item.name, item.stat().st_size))
        except OSError:
            continue
    rows.sort(key=lambda r: -r[1])
    total = sum(r[1] for r in rows) or 1
    out = [{"名称": n, "MB": round(sz / 1048576, 1),
            "占比": round(sz * 100 / total, 1)} for n, sz in rows[:top]]
    out.append({"名称": "（其余）", "MB": round((total - sum(r[1] for r in rows[:top])) / 1048576, 1),
                "占比": round((total - sum(r[1] for r in rows[:top])) * 100 / total, 1)})
    return out


def print_size_breakdown(internal: Path) -> None:
    print("\n=== _internal 体积构成 ===")
    for row in size_breakdown(internal):
        print(f"  {row['MB']:8.1f} MB  {row['占比']:5.1f}%  {row['名称']}")


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
    # 2026-11 启动崩溃的教训：ui_text 漏收会让 webview_backend 的导入链断掉，
    # 而 app_webview 的兜底会把它变成一句「'NoneType' object is not callable」。
    # 在打包端直接挡：这几个模块必须在，缺一个就别发版。
    "_internal/ui_text.py",
    "_internal/webview_backend.py",
    "_internal/webview_server.py",
    "_internal/cluster_core.py",
    "manifest.json",
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
    ap.add_argument("--no-slim", action="store_true",
                    help="保留完整 OpenVINO 组件（排查「设备消失」类问题时用）")
    ap.add_argument("--noffmpeg-variant", action="store_true",
                    help="主包带 ffmpeg 时，额外打一个 -noffmpeg 变体（CI 用）")
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
        # QWENASR_WEBVIEW_BUILD=1 必须对 PyInstaller 的分析子进程可见，否则
        # app.py 会连 Tk GUI 一起被静态收进包（见 app.py 的 _WEBVIEW_BUILD）。
        build_env = dict(os.environ)
        build_env["QWENASR_WEBVIEW_BUILD"] = "1"
        subprocess.run(cmd, check=True, cwd=str(ROOT), env=build_env)

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

    def _pack(ffmpeg_exe, variant=""):
        """打一个变体包，并把它自己的验证结果写进该包的 manifest。"""
        zp = make_package(args.version, args.sha, ffmpeg_exe,
                          slim=not args.no_slim, variant=variant)
        meta = json.loads((ROOT / "release_pkg" / APP_NAME / "build_manifest.json")
                          .read_text(encoding="utf-8"))
        return zp, meta

    zip_path, meta = _pack(ff, "")
    # 体积构成：只打印，不改任何东西。回答「哪个占大头」靠它，不靠猜。
    # 统计对象必须是最终发行目录（已精简），统计 dist 中间产物会把已删掉的
    # 244.9 MB openvino/ 重新算进来 —— 那份数字对用户毫无意义。
    stage_internal = ROOT / "release_pkg" / APP_NAME / "_internal"
    if stage_internal.is_dir():
        print(f"\n发行包 _internal 体积：{dir_size(stage_internal) / 1024 / 1024:.1f} MB")
        print_size_breakdown(stage_internal)

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

    # ── 第二变体：不含 ffmpeg ─────────────────────────────────────────────
    # 用途：只想转 WAV / 已有系统 ffmpeg 的用户可以少下 ~100 MB。
    # 只有「主包确实带了 ffmpeg」时才值得再打一个，否则两个包一模一样。
    if args.noffmpeg_variant:
        if meta.get("随附 ffmpeg"):
            print("\n=== 打不含 ffmpeg 的变体 ===")
            zp2, meta2 = _pack(None, "noffmpeg")
            v2 = verify_onedir(ROOT / "release_pkg" / APP_NAME)
            print(f"  发行包：{zp2.name}")
            print(f"  大小：{zp2.stat().st_size / 1024 / 1024:.1f} MB"
                  f"  状态：{v2['状态']}")
            if v2["缺失"]:
                print("  缺失：", v2["缺失"])
                return 1
            (ROOT / "release_pkg" / "archive_verify_noffmpeg.json").write_text(
                json.dumps(v2, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  变体 manifest：随附 ffmpeg = {meta2.get('随附 ffmpeg')}")
        else:
            print("\n主包未随附 ffmpeg → 跳过 noffmpeg 变体（两者会完全相同）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
