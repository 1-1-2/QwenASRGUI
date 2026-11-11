"""Wave 7 regression: WebView packaging must not drag the Tk GUI into the bundle.

Why this exists
---------------
``app.py`` is the Tk desktop app. ``app_webview.py`` (the shipped launcher) only
reuses its inference core: ASREngine / ASREngine1p7B / RealtimeManager and the
pure helpers above them. But PyInstaller analyses imports statically - one
top-level ``import customtkinter`` is enough to pull tkinter, customtkinter and
the whole tcl/tk runtime (~15-25 MB) into a package whose UI is HTML.

The GUI section is therefore guarded by ``QWENASR_WEBVIEW_BUILD=1``. These
tests run ``app.py`` in a subprocess under both settings and assert:

  * WebView mode: inference classes exist, tkinter/customtkinter never load;
  * Tk mode: App exists and the GUI stack does load (no regression);
  * build_release.py actually sets the flag for PyInstaller.

    python tests/test_webview_bundle.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name, why):
        # 不算通过也不算失败：依赖不在的环境（如 CI runner）无法验证这一项，
        # 但也不能因此判红 —— 否则「本机装了 customtkinter 才绿」的假象会一直存在。
        print(f"  --   {name}（跳过：{why}）")

    @property
    def ok(self):
        return all(self.items)


PROBE = r"""
import sys, json
import app
print(json.dumps({
    "has_app": hasattr(app, "App"),
    "has_engine": hasattr(app, "ASREngine"),
    "has_engine17": hasattr(app, "ASREngine1p7B"),
    "has_realtime": hasattr(app, "RealtimeManager"),
    "has_split": callable(getattr(app, "_split_to_lines", None)),
    "has_assign": callable(getattr(app, "_assign_ts", None)),
    "tk": "tkinter" in sys.modules,
    "ctk": "customtkinter" in sys.modules,
}))
"""


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _probe(env_extra: dict) -> dict | None:
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"})
    env.update(env_extra)
    for k, v in env_extra.items():
        if v == "":
            env.pop(k, None)
    import json
    try:
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        print("    stderr:", out.stderr.strip().splitlines()[-1:] or "")
        return None
    line = out.stdout.strip().splitlines()[-1]
    return json.loads(line)


def main() -> int:
    r = Result()

    # ── WebView build mode ────────────────────────────────────────────────
    wb = _probe({"QWENASR_WEBVIEW_BUILD": "1"})
    r.check("app.py imports in WebView mode", wb is not None)
    if wb:
        r.check("inference core is present",
                wb["has_engine"] and wb["has_engine17"] and wb["has_realtime"])
        r.check("pure helpers are present", wb["has_split"] and wb["has_assign"])
        r.check("Tk App is NOT defined", not wb["has_app"])
        r.check("tkinter is never imported", not wb["tk"])
        r.check("customtkinter is never imported", not wb["ctk"])

    # ── default / Tk mode (no regression for `python app.py`) ─────────────
    # Tk 模式需要 customtkinter；CI runner 不装它，本地也不一定有。
    # 依赖缺失时跳过（见 Result.skip），不要把它当成「代码坏了」。
    has_ctk = _has_module("customtkinter")
    if not has_ctk:
        for name in ("app.py imports in Tk mode", "Tk App still defined",
                     "inference core still present", "Tk mode does load tkinter"):
            r.skip(name, "环境未安装 customtkinter")
    else:
        tk = _probe({"QWENASR_WEBVIEW_BUILD": ""})
        r.check("app.py imports in Tk mode", tk is not None)
        if tk:
            r.check("Tk App still defined", tk["has_app"])
            r.check("inference core still present", tk["has_engine"])
            r.check("Tk mode does load tkinter", tk["tk"])

    # ── static checks on the build & launcher ─────────────────────────────
    build_src = (ROOT / "build_release.py").read_text(encoding="utf-8")
    r.check("build_release.py sets QWENASR_WEBVIEW_BUILD",
            "QWENASR_WEBVIEW_BUILD" in build_src)

    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    # 以 GUI 守卫块为界：界外的顶层 import 才是问题。
    # （这里曾经按 `_WEBVIEW_BUILD = os.environ` 这个字面量切分 —— 一旦赋值语句
    #   写法变了就切不到，head 变成整个文件，于是把守卫块里的 import 当成顶层
    #   import 报假红。切分标记要挑「语义上稳定」的那一行，不是实现细节。）
    marker = "\nif not _WEBVIEW_BUILD:"
    head = app_src.split(marker)[0] if marker in app_src else app_src
    r.check("app.py 有 GUI 守卫块 if not _WEBVIEW_BUILD:", marker.strip() in app_src)
    r.check("no top-level tkinter import in app.py",
            not any(ln.strip() in ("import tkinter as tk",)
                    for ln in head.splitlines()))
    r.check("no top-level customtkinter import in app.py",
            "import customtkinter as ctk" not in head)

    # 守卫的**判据**必须有一条运行期来源。只用构建期的环境变量会让 exe 在用户
    # 机器上翻转成 False（exe 里没有那个变量）→ import tkinter → 启动即崩。
    r.check("_WEBVIEW_BUILD 的判据包含 sys.frozen（运行期）",
            '"frozen"' in app_src.split("def _is_webview_build")[-1][:400]
            or "getattr(sys, \"frozen\"" in app_src.split("def _is_webview_build")[-1][:400])

    # Release packaging must keep excluding the .NET window stack, and now the
    # Tk stack too: tkinter cannot be excluded via --exclude-module alone when a
    # top-level import exists, hence the guard above; this asserts the tooling
    # never re-adds a hidden import for the GUI.
    r.check("customtkinter is not a hidden import in build_release.py",
            '"customtkinter"' not in build_src.split("HIDDEN_IMPORTS")[1].split("]")[0])

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
