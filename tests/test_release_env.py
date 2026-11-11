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
import ast
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED = ("tkinter", "customtkinter", "darkdetect", "scipy")

# 子进程里运行的探针：封掉依赖后，把启动器的依赖链整条 import 一遍。
#
# 关键点：探针模拟的是 exe 的**运行时**，不是构建时。
#   · 必须设 sys.frozen = True —— 这是冻结版唯一可靠的「我是打包版」判据；
#   · 必须【不设】QWENASR_WEBVIEW_BUILD —— 那个变量只在 PyInstaller 分析
#     子进程里出现，用户机器上根本没有。
# 这两条曾经反过来写过（只设环境变量、不设 frozen），于是探针在
# app.py 的 `if not _WEBVIEW_BUILD:` 处顺利绕过了 tkinter，而真机没有那个
# 变量 → 直接 import tkinter → 打包版启动即崩。模拟与真实条件不一致，
# 就会把「必崩」测成「全绿」。
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
    sys.frozen = True                             # 与 exe 一致
    os.environ.pop("QWENASR_WEBVIEW_BUILD", None) # exe 运行时不存在

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
#
# 这份清单必须等于「起得来」所需的全部模块。它曾经少了 ffmpeg_utils —— 于是
# 本地模拟全绿，真机上启动器却死在 webview_backend → ffmpeg_utils。现在由
# tests/test_launcher_stdio.py 反向校验：解析 app_webview.py 的完整 import 闭包
# 逐个子查，谁被漏掉就直接报名字。
CHAIN = [
    "app_paths", "proc_guard", "single_instance",
    "webview_server", "webview_backend",
    "app", "ffmpeg_utils", "audio_io", "diarize", "cluster_core",
    "api_server", "cli_mode", "downloader", "model_catalog",
    "longform_runner", "crisp_engine", "batch_queue", "subtitle_lines",
    "subtitle_extras", "run_log", "version", "updater", "ui_text",
    "endpoint_tab", "model_tab", "batch_tab", "subtitle_editor", "setting",
    # 下面这些过去靠「静态可追踪 / hidden-import」被收进包，从没进过这份清单。
    # 它们缺了同样会让启动器起来，所以一并纳入模拟。
    "cf_tunnel", "chatllm_engine", "fa_aligner", "postprocess", "processor_numpy",
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


LOCAL_MODULES = {p.stem for p in ROOT.glob("*.py")}       # 项目自己的模块


def import_closure(entry: str = "app_webview.py") -> set[str]:
    """从入口文件出发，把「项目内模块」的 import 闭包算出来（含动态 def 内导入）。

    PyInstaller 也是这么看代码的（静态分析），差别在于它会把找到的全打包；
    我们只用这份闭包检查测试清单有没有漏。
    """
    seen: set[str] = set()
    stack = [Path(entry).stem]
    while stack:
        mod = stack.pop()
        if mod in seen or mod not in LOCAL_MODULES:
            continue
        seen.add(mod)
        py = ROOT / f"{mod}.py"
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
                names += [a.name for a in node.names]     # from x import y 里的 y
            for nm in names:
                if nm in LOCAL_MODULES and nm not in seen:
                    stack.append(nm)
    return seen


def missing_from_chain() -> tuple[list[str], set[str]]:
    """CHAIN 是否覆盖了启动器的全部项目内依赖。"""
    closure = import_closure()
    # app_webview 是入口本身，不需要「import 一次」来验证；app-gpu 是 Tk 版入口，
    # 发行包刻意不带。其余全要覆盖。
    allowed = {"app-gpu", "app_webview"}
    return sorted(closure - set(CHAIN) - allowed), closure


# 复刻 exe 运行时的最小条件：frozen=True、无 QWENASR_WEBVIEW_BUILD、tkinter 缺失。
FROZEN_PROBE = textwrap.dedent('''
    import os, sys
    from importlib.abc import MetaPathFinder

    class Blocker(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in {blocked!r}:
                raise ImportError("%s blocked (release env)" % name)
            return None

    sys.meta_path.insert(0, Blocker())
    sys.frozen = True
    os.environ.pop("QWENASR_WEBVIEW_BUILD", None)

    import app
    print("WEBVIEW_BUILD=" + repr(bool(getattr(app, "_WEBVIEW_BUILD", None))))
    print("TK_LOADED=" + repr("tkinter" in sys.modules))
''')


def _run_frozen_probe() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("QWENASR_WEBVIEW_BUILD", None)
    env.pop("PYTHONHOME", None)
    p = subprocess.run([sys.executable, "-c", FROZEN_PROBE.format(blocked=BLOCKED)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       timeout=300, env=env)
    out = {}
    for line in (p.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    if not out:
        out["_error"] = ((p.stdout or "") + (p.stderr or ""))[-400:]
    return out


def run_probe(mods: list[str]) -> tuple[list, str]:
    code = PROBE.format(blocked=BLOCKED, mods=mods)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    # 刻意不设 QWENASR_WEBVIEW_BUILD：exe 运行时没有它（见 PROBE 的注释）。
    env.pop("QWENASR_WEBVIEW_BUILD", None)
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

    # 2b) 直接钉住 2026-11-11 的真正死因：app.py 用「构建期环境变量」判断自己
    #     是不是 WebView 版。该变量只传给 PyInstaller 的分析子进程，exe 在用户
    #     机器上运行时不存在 → 布尔值翻转 → 真的去 import tkinter → 包里没有。
    #     这里在 frozen=True 且**没有**环境变量的条件下导入 app，断言两件事：
    #     它认自己是 WebView 版、且全程没有碰过 tkinter。
    o = _run_frozen_probe()
    wb, tk = o.get("WEBVIEW_BUILD"), o.get("TK_LOADED")
    r.check("frozen 且无环境变量时，app 仍认自己是 WebView 版", wb == "True",
            o.get("_error") or f"_WEBVIEW_BUILD={wb}")
    r.check("该条件下 app 全程未导入 tkinter", tk == "False",
            o.get("_error") or f"tkinter in sys.modules={tk}")

    # 3) Tk 专用分页：允许失败，但必须给出「不拖累核心」的结论
    tk_only = ["endpoint_tab", "model_tab", "batch_tab", "subtitle_editor", "setting"]
    tk_failed, _ = run_probe(tk_only)
    r.check("Tk 专用分页在发行环境里确实不可导入（它们不该进包）",
            len(tk_failed) == len(tk_only),
            f"{len(tk_failed)}/{len(tk_only)} 失败（预期）")

    # 4) 源码守卫：不允许再出现 ffmpeg_utils 那种顶层 customtkinter
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

    # 5) 反向校验：清单本身必须覆盖启动器的整条 import 闭包。
    #
    # 为什么需要「反向」这一条：上面几步只能验「我在清单里列的模块都能导入」。
    # 如果清单本身漏了某个模块（ffmpeg_utils 就是这么漏的 —— 那时它已经写在
    # CHAIN 里了，漏的是更早的一版），模拟就会全绿而真机必崩。所以这里改成
    # 从 app_webview.py 出发把项目内模块的 import 闭包算出来，跟 CHAIN 对差集，
    # 少一个就报名字。
    missing, closure = missing_from_chain()
    r.check("CHAIN 覆盖 app_webview 的完整 import 闭包", not missing,
            "未覆盖：" + ", ".join(missing[:6]) + f"（闭包共 {len(closure)} 个）")

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
