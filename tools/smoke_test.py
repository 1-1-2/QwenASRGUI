"""smoke_test.py — 把打好的 ZIP 解开、就地跑一遍，确认「打不开 / 一启动就报错」。

为什么必须在 CI 里做
--------------------
2026-11-11 的发布包构建全绿、校验全过，用户一启动却是：

    TypeError: 'NoneType' object is not callable

原因是包内少了 `ui_text.py`，`webview_server` 的导入链断掉，被启动器的
except 吞成 `WebViewServer = None`。**静态校验（文件清单/manifest）发现不了
这种事** —— 文件数对得上，缺的那一个恰好不在清单里。

所以这里不看清单，只看行为：
  1. 解压 ZIP，关键模块与 OpenVINO CPU 三件套必须在位
  2. `exe --selftest` 必须返回 0，且日志里的 JSON 是 ok=true / server=measured
     （server=skipped 视为失败 —— 那正是本次崩溃的形态）
  3. 真正启动 GUI，等它把 `http://127.0.0.1:PORT` 写进日志
  4. 全程不允许出现 crash.log；进程不许提前退出
  5. 收尾 taskkill /T，别把 runner 弄脏

用法：
    python tools/smoke_test.py dist.zip
    python tools/smoke_test.py dist.zip --workdir D:\\tmp\\smoke --wait 40
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

EXE_NAME = "QwenASR-WebView.exe"
APP_DIR_NAME = "QwenASR-WebView"      # 期望的包根目录名（找不到就在整棵树里搜）
LOG_DIR_NAME = "聲音辨識小工具"        # 与 app_webview.APP_NAME 一致

# 缺一个就说明这个包不可用（不是「可能会坏」，是「一定坏」）
# 必需条目 —— 按「文件名 + 相对 _internal 的路径」断言，不写死包根目录名
# （实测包根是 QwenASR-WebView_2.0.0-longform.xxx_windows-x64/，不是 APP_NAME）
REQUIRED_INTERNAL = [
    "ui_text.py",
    "webview_backend.py",
    "webview_server.py",
    "cluster_core.py",
    "openvino/libs/openvino.dll",
    "openvino/libs/openvino_intel_cpu_plugin.dll",
]
REQUIRED_TOP = ["manifest.json"]

# 出现即说明瘦身没生效（白送体积，不是功能问题，但也别发出去）
FORBIDDEN_INTERNAL_PREFIX = [
    "scipy/",
    "scipy.libs/",
    "openvino/libs/openvino_intel_gpu",
    "openvino/libs/openvino_intel_npu",
]


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home()
    return root / LOG_DIR_NAME


def read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def tail(text: str, n: int = 25) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join("      " + ln[:200] for ln in lines[-n:])


def kill_tree(proc: subprocess.Popen) -> None:
    """连子进程一起收掉：启动器会派生 Edge、crispasr.exe 等。"""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


# 诊断用：在包内 _internal 里「去掉 tkinter 相关模块」再导入启动器的依赖链。
# 打包版缺哪个模块，这里一次就能看出来，不必反复等 20 分钟的 Windows 构建。
DIAG = r"""
import os, sys, traceback
here = sys.argv[1]
sys.path.insert(0, here)
for name in ("tkinter", "customtkinter"):
    sys.modules[name] = None
os.environ["QWENASR_WEBVIEW_BUILD"] = "1"
out = []
for mod in ("webview_server", "webview_backend", "app", "ffmpeg_utils"):
    try:
        __import__(mod)
        out.append("OK   " + mod)
    except Exception:
        tb = traceback.format_exc().strip().splitlines()
        out.append("FAIL " + mod + " -> " + " | ".join(tb[-6:]))
print("\n=== DIAG-IMPORT ===")
print("\n".join(out))
"""


def diagnose_imports(exe_dir: Path, internal: Path) -> None:
    """用包内自带的 Python 环境跑一遍导入，定位真正的缺失模块。"""
    fi = subprocess.run([sys.executable, "-I", "-c", DIAG, str(internal)],
                        capture_output=True, text=True, timeout=180)
    print((fi.stdout or "") + (fi.stderr or "")[-1500:])


class Checker:
    def __init__(self):
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""),
              flush=True)
        if not ok:
            self.failed.append(name)
        return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--workdir", default="", help="解压目录（默认系统临时目录）")
    ap.add_argument("--wait", type=float, default=40.0, help="等 GUI 就绪的秒数")
    ap.add_argument("--keep", action="store_true", help="跑完不删解压目录")
    args = ap.parse_args()

    zp = Path(args.zip_path).resolve()
    if not zp.is_file():
        print(f"错误：找不到 ZIP：{zp}")
        return 1
    work = Path(args.workdir) if args.workdir else Path(
        os.environ.get("RUNNER_TEMP") or os.environ.get("TEMP") or "/tmp") / "qwenasr_smoke"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    c = Checker()
    print(f"\n=== 冒烟测试：{zp.name}（{zp.stat().st_size / 1048576:.1f} MB）===")

    # ── 1) 解压 + 内容检查 ────────────────────────────────────────────────
    t0 = time.time()
    try:
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            z.extractall(work)
    except Exception as exc:
        print(f"错误：解压失败 {type(exc).__name__}: {exc}")
        return 1
    print(f"  解压 {len(names)} 个条目，用时 {time.time() - t0:.1f}s")

    name_set = {n.replace("\\", "/") for n in names}
    # 包根目录：唯一含 .exe 的那一层
    roots = {n.split("/")[0] for n in name_set if n.lower().endswith("/" + EXE_NAME.lower())}
    root = sorted(roots)[0] if roots else APP_DIR_NAME
    c.check("包根目录唯一", len(roots) == 1, str(sorted(roots)))
    internal_prefix = f"{root}/_internal/"
    missing = [r for r in REQUIRED_INTERNAL if internal_prefix + r not in name_set]
    missing += [f"{root}/{r}" for r in REQUIRED_TOP if f"{root}/{r}" not in name_set]
    c.check("必需文件齐全", not missing, ", ".join(missing[:5]))
    bad = [n for n in name_set
           if n.startswith(internal_prefix)
           and any(n[len(internal_prefix):].startswith(p) for p in FORBIDDEN_INTERNAL_PREFIX)]
    c.check("无 SciPy / GPU / NPU 残留", not bad, ", ".join(sorted(bad)[:3]))

    # 包根目录名不写死：它随版本变（实测是 ..._windows-x64/），先按名字找，
    # 找不到就在整棵树里搜唯一的 exe。
    exe = work / root / EXE_NAME
    if not exe.is_file():
        found = sorted(work.rglob(EXE_NAME))
        if len(found) == 1:
            exe = found[0]
        else:
            print(f"错误：解压后找不到唯一的 {EXE_NAME}")
            return 1
    print(f"  启动器：{exe.relative_to(work)}")
    exe_dir = exe.parent

    # ── 2) --selftest（无窗口，能捕获导入期错误）──────────────────────────
    logs = log_dir()
    launch_log = logs / "launcher.log"
    crash_log = logs / "crash.log"
    for p in (launch_log, crash_log):
        try:
            p.unlink()
        except OSError:
            pass

    print("\n  [1/3] --selftest")
    try:
        (logs / "selftest.json").unlink()
    except OSError:
        pass
    # windowed exe 没有控制台：stdout/stderr 都被启动器换成日志替身，
    # 且启动器关窗走 os._exit，等待管道会挂死 —— 所以固定超时，结果看文件。
    try:
        code = subprocess.run([str(exe), "--selftest"], cwd=str(exe_dir),
                              timeout=120).returncode
    except subprocess.TimeoutExpired:
        code = -1
    result_file = logs / "selftest.json"
    m = None
    try:
        m = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        m = None
    text = read_log(launch_log)
    if not c.check("selftest 退出码为 0", code == 0, f"code={code}"):
        print(tail(text))
        return 1
    if not c.check("selftest 写出了结果文件", m is not None, str(result_file)):
        print(tail(text))
        return 1
    c.check("selftest ok=true", m.get("ok") is True, str(m.get("ok")))
    # server=skipped 是「GUI 依赖没进包」的形态，正是 2026-11-11 那次崩溃
    c.check("server 已实测（非 skipped）", m.get("server") == "measured",
            str(m.get("serverSkipReason") or m.get("server")))

    # ── 3) 真正启动一次 GUI ───────────────────────────────────────────────
    print("\n  [2/3] 启动 GUI")
    for p in (launch_log, crash_log):
        try:
            p.unlink()
        except OSError:
            pass

    proc = subprocess.Popen([str(exe)], cwd=str(exe_dir))
    url_ok = False
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        t = read_log(launch_log)
        if re.search(r"http://127\.0\.0\.1:\d+", t):
            url_ok = True
            break
        time.sleep(0.5)

    alive = proc.poll() is None
    text = read_log(launch_log)
    c.check("进程启动后没有立即退出", alive,
            "" if alive else f"exit={proc.returncode}")
    c.check("已绑定本地端口并打印地址", url_ok, "" if url_ok else f"等待 {args.wait:.0f}s 未出现")
    c.check("未产生 crash.log", not crash_log.exists(),
            read_log(crash_log)[:300] if crash_log.exists() else "")
    # 缺 tkinter 等可选 GUI 依赖时，代码里可能有 messagebox 弹窗 —— CI 的
    # runner 是无交互桌面，弹窗会直接卡死。日志里出现弹窗痕迹就判失败。
    c.check("启动过程没有报缺模块",
            "No module named" not in text,
            next((ln for ln in text.splitlines() if "No module named" in ln), "")[:160])

    # 再多等两秒：有些崩溃发生在窗口创建之后
    if alive:
        time.sleep(2.5)
        c.check("启动 2.5s 后仍存活", proc.poll() is None,
                f"exit={proc.returncode}" if proc.poll() is not None else "")
    c.check("全程未产生 crash.log", not crash_log.exists())

    # ── 4) 收尾 ───────────────────────────────────────────────────────────
    print("\n  [3/3] 收尾")
    kill_tree(proc)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)

    if c.failed:
        print("\n=== 包内导入诊断（定位真实缺失模块）===")
        try:
            diagnose_imports(exe_dir, exe_dir / "_internal")
        except Exception as exc:
            print(f"  诊断本身失败：{type(exc).__name__}: {exc}")
        print(f"\n启动器日志尾部（{launch_log}）：")
        print(tail(text))
        print(f"\n结果：{len(c.failed)} 项失败 → {', '.join(c.failed)}")
        print("❌ 冒烟测试未通过")
        return 1
    print("\n✅ 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
