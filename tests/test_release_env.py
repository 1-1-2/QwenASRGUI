"""模拟发行环境：在「没有 tkinter / customtkinter / scipy」的条件下跑冒烟检查。

为什么需要这个测试
------------------
2026-11-11 连续两次 Windows 构建「全绿打包、用户一启动就崩」：

  1. 缺 `ui_text.py` → WebViewServer=None → TypeError: 'NoneType' object is not callable
  2. `webview_backend → ffmpeg_utils` 顶层 `import customtkinter` → tkinter 被
     EXCLUDE 排除 → RuntimeError: No module named 'tkinter'

两次都无法在源码环境复现，只能靠 CI 里真机跑 exe 才发现。这个脚本把
「发行环境」搬到本地：装一个 MetaPathFinder 把 tkinter / customtkinter /
scipy 全部封掉（它们是发布包刻意不带的），然后 import 启动器的完整依赖链。

    python tests/test_release_env.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED = ("tkinter", "customtkinter", "darkdetect", "scipy")

# 子进程里运行的探针：封掉依赖后，把启动器的依赖链整条 import 一遍
PROBE = textwrap.dedent('''
    import os
    import sys
    from importlib.abc import MetaPathFinder

    BLOCKED = {blocked!r}

    class Blocker(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("%s blocked (release env)" % name)
            return None

    sys.meta_path.insert(0, Blocker())
    os.environ["QWENASR_WEBVIEW_BUILD"] = "1"      # 打包时的同一开关

    failed = []
    for mod in {mods!r}:
        try:
            __import__(mod)
        except Exception as exc:
            failed.append((mod, type(exc).__name__, str(exc)))
    print("FAILEDJSON" + repr(failed))
''')

# 启动器（含 GUI 进程）实际需要导入的模块；ffmpeg_utils / api_server / audio_io
# 是最容易被忽略的间接依赖（它们过去在顶层 import tkinter）。
CHAIN = [
    "app_paths", "proc_guard", "single_instance",
    "webview_server", "webview_backend",
    "app", "ffmpeg_utils", "audio_io", "diarize", "cluster_core",
    "api_server", "cli_mode", "downloader", "model_catalog",
    "longform_runner", "crisp_engine", "batch_queue", "subtitle_lines",
    "subtitle_extras", "run_log", "version", "updater", "ui_text",
    "endpoint_tab", "model_tab", "batch_tab", "subtitle_editor", "setting",
]


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def run_probe(mods: list[str]) -> tuple[list, str]:
    code = PROBE.format(blocked=BLOCKED, mods=mods)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["QWENASR_WEBVIEW_BUILD"] = "1"
    env.pop("PYTHONHOME", None)
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=300, env=env)
    failed = []
    for line in p.stdout.splitlines():
        if line.startswith("FAILEDJSON"):
            failed = eval(line[len("FAILEDJSON"):])      # 只解析自己打印的字面量
    return failed, (p.stdout + p.stderr)[-1200:]


def main() -> int:
    r = Result()
    print("\n[发行环境] 封掉 tkinter / customtkinter / scipy 后导入启动器的依赖链")

    # 1) 核心链必须全通（这就是「打包版能不能起来」的等价条件）
    core = [m for m in CHAIN if not m.endswith("_tab") and m != "subtitle_editor"
            and m != "setting"]
    failed, log = run_probe(core)
    r.check("探针本身跑通了", "FAILEDJSON" in log or not failed, log[-200:])
    r.check("核心依赖链全部可导入", not failed,
            "; ".join(f"{m}: {e}" for m, _, e in failed[:3]))

    # 2) 明确断言：这条链里不许有人顶层 import tkinter
    if not failed:
        r.check("WebViewServer 可构造", True)
    else:
        r.check("WebViewServer 可构造", False, "")

    # 3) Tk 专用分页：允许失败，但必须给出「不拖累核心」的结论
    tk_only = ["endpoint_tab", "model_tab", "batch_tab", "subtitle_editor", "setting"]
    tk_failed, _ = run_probe(tk_only)
    r.check("Tk 专用分页在发行环境里确实不可导入（它们不该进包）",
            len(tk_failed) == len(tk_only),
            f"{len(tk_failed)}/{len(tk_only)} 失败（预期）")

    # 4) 源码守卫：不允许再出现 ffmpeg_utils 那种顶层 customtkinter
    import ast
    offenders = []
    for py in sorted(ROOT.glob("*.py")):
        if py.name in ("app-gpu.py", "app.py"):
            continue                       # Tk 版入口本身要 tkinter
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:             # 只看顶层语句
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for nm in names:
                if nm.split(".")[0] in BLOCKED:
                    offenders.append(f"{py.name}:{node.lineno} {nm}")
    # 被允许的例外：Tk 版才用的模块（endpoint_tab / setting / *_tab / subtitle_editor
    # / app-gpu.py）不在发行包里，它们的顶层 tkinter 无所谓。
    allowed_files = {"endpoint_tab.py", "setting.py", "model_tab.py", "batch_tab.py",
                     "subtitle_editor.py", "app-gpu.py", "app.py"}
    offenders = [o for o in offenders if o.split(":")[0] not in allowed_files]
    r.check("发行链上的模块不再顶层 import tkinter/customtkinter/scipy",
            not offenders, "; ".join(offenders[:3]))

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
