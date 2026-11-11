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


# ── 诊断 ────────────────────────────────────────────────────────────────
# 只在失败时跑：用包内自带的依赖（_internal）把启动器的依赖链逐个 import 一遍，
# 一次看出「到底缺哪个模块」，不必反复等 20 分钟的 Windows 构建。
#
# 两个坑，都踩过：
#   1. 不能用 `python -I`：隔离模式会忽略 sys.path 注入，所有模块一律
#      ModuleNotFoundError，诊断等于没跑（2026-11-11 就白跑了一轮）。
#   2. 光看谁失败没用 —— 一次运行只会暴露「最先断的那一环」，修掉它才轮到下一个。
#      所以每个模块都在**独立子进程**里导入。
DIAG = r"""
import os, sys, traceback
host = r"{host}"
internal = r"{internal}"
# Windows 的扩展模块（.pyd）要能找到同目录的 DLL；_internal 里有 python311.dll
if hasattr(os, "add_dll_directory") and os.path.isdir(internal):
    try:
        os.add_dll_directory(internal)
    except OSError:
        pass
# 包内依赖优先，其次是宿主解释器的 stdlib / site-packages
sys.path.insert(0, host)
sys.path.insert(1, internal)
# 关键：要复刻 exe 的**运行时**条件，而不是构建时的条件。
# 这里刻意【不设】QWENASR_WEBVIEW_BUILD —— 那个变量只在 PyInstaller 分析
# 子进程里存在，exe 在用户机器上跑的时候是没有的。设置它会让诊断假绿
# （2026-11-11 的诊断就是这样连续两轮被糊弄过去）。运行时唯一的判据是
# sys.frozen。
sys.frozen = True
os.environ.pop("QWENASR_WEBVIEW_BUILD", None)
# 发行包刻意不带的，按「已缺失」处理 —— 与 exe 里的真实情况一致
for name in ("tkinter", "customtkinter", "darkdetect", "scipy"):
    sys.modules[name] = None

chain = ("app_paths", "webview_server", "webview_backend", "app",
         "ffmpeg_utils", "audio_io", "downloader", "ui_text")
for mod in chain:
    try:
        __import__(mod)
        print("OK   " + mod)
    except BaseException:
        tb = traceback.format_exc().strip().splitlines()
        # 只留真正有信息量的行：异常类型 + 报错位置
        keep = [ln.strip() for ln in tb if ln.startswith("  File")][-3:]
        keep.append(tb[-1] if tb else "")
        print("FAIL " + mod + " -> " + " | ".join(keep))
"""


def diagnose_imports(exe_dir: Path) -> None:
    """用包内的依赖逐个 import 启动器依赖链，定位真正缺失的模块。"""
    code = DIAG.format(host=str(exe_dir), internal=str(exe_dir / "_internal"))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    print("  宿主解释器：" + sys.executable)
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd=str(exe_dir),
                           capture_output=True, text=True, timeout=240, env=env)
    except subprocess.TimeoutExpired:
        print("  诊断超时")
        return
    out = (p.stdout or "").strip()
    print(out if out else "(无输出)")
    if p.returncode != 0 and p.stderr:
        print("  诊断进程 stderr：" + (p.stderr or "")[-600:])


def dump_log_errors(text: str, limit: int = 12) -> None:
    """把启动器日志里凡是带「异常/缺模块/失败」的行按时间顺序列出来。

    这里不做判定（判定只看后果：端口有没有绑上、进程有没有活着），
    只是把线索摆出来，省去翻整篇日志。
    """
    keys = ("Traceback", "Error", "No module named", "RuntimeError",
            "TypeError", "Exception", "失敗", "失败", "無法", "无法")
    hits = [ln.strip() for ln in text.splitlines() if any(k in ln for k in keys)]
    for ln in hits[:limit]:
        print("      " + ln[:220])


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
    result_file = logs / "selftest.json"

    def clean_logs():
        for p in (launch_log, crash_log, result_file):
            try:
                p.unlink()
            except OSError:
                pass

    clean_logs()
    print("\n  [1/3] --selftest")
    # windowed exe 没有控制台：stdout/stderr 都被启动器换成日志替身，
    # 且启动器关窗走 os._exit，等待管道会挂死 —— 所以固定超时，结果看文件。
    try:
        code = subprocess.run([str(exe), "--selftest"], cwd=str(exe_dir),
                              timeout=120).returncode
    except subprocess.TimeoutExpired:
        code = -1
    m = None
    try:
        m = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        m = None
    selftest_log = read_log(launch_log)
    if not c.check("selftest 退出码为 0", code == 0, f"code={code}"):
        dump_log_errors(selftest_log)
        return 1
    if not c.check("selftest 写出了结果文件", m is not None, str(result_file)):
        dump_log_errors(selftest_log)
        return 1
    c.check("selftest ok=true", m.get("ok") is True, str(m.get("ok")))
    # server=skipped 是「GUI 依赖没进包」的形态，正是 2026-11-11 那次崩溃。
    # 注意：这里不靠日志文本判定 —— 修好后导入链一旦通，启动器仍可能把
    # 一个【已处理的】旧告警写进日志（例如另一进程/另一包留下的痕迹），
    # 按文本判会假红。真正的判据是下面这一步的后果：端口有没有绑上。
    c.check("server 已实测（非 skipped）", m.get("server") == "measured",
            str(m.get("serverSkipReason") or m.get("server")))
    if c.failed:
        dump_log_errors(selftest_log)

    # ── 3) 真正启动一次 GUI ───────────────────────────────────────────────
    print("\n  [2/3] 启动 GUI")
    clean_logs()

    proc = subprocess.Popen([str(exe)], cwd=str(exe_dir))
    url_ok = False
    url = ""
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        t = read_log(launch_log)
        mm = re.search(r"http://127\.0\.0\.1:\d+", t)
        if mm:
            url_ok, url = True, mm.group(0)
            break
        time.sleep(0.5)

    alive = proc.poll() is None
    text = read_log(launch_log)
    c.check("进程启动后没有立即退出", alive,
            "" if alive else f"exit={proc.returncode}")
    c.check("已绑定本地端口并打印地址", url_ok, url or f"等待 {args.wait:.0f}s 未出现")
    c.check("未产生 crash.log", not crash_log.exists(),
            read_log(crash_log)[:300] if crash_log.exists() else "")

    # 端口通了就等于「WebView 后端确实起来了」—— 这比在日志里找
    # 「No module named」可靠：后者会被历史告警、无关的可选依赖干扰。
    health_ok = False
    if url_ok:
        import urllib.request
        try:
            with urllib.request.urlopen(url + "/health", timeout=10) as resp:
                health_ok = resp.status == 200
        except Exception as exc:
            health_ok = False
            text += f"\n[smoke] /health 探测失败：{type(exc).__name__}: {exc}"
    c.check("/health 返回 200（后端已就绪）", health_ok)

    # 再多等两秒：有些崩溃发生在窗口创建之后
    if alive:
        time.sleep(2.5)
        c.check("启动 2.5s 后仍存活", proc.poll() is None,
                f"exit={proc.returncode}" if proc.poll() is not None else "")
    c.check("全程未产生 crash.log", not crash_log.exists())

    # 把启动计时打出来。这些行由 app_webview._StartupClock 写在 launcher.log 里，
    # 是回答「为什么开得慢」的唯一一手数据 —— 留在 CI 日志里，下次不必再猜。
    stamps = [ln.strip() for ln in read_log(launch_log).splitlines()
              if "启动计时" in ln]
    if stamps:
        print("\n  真机启动分段耗时：")
        for ln in stamps:
            print(f"    {ln}")
    else:
        print("\n  （launcher.log 无启动计时行 —— 若刚加过 _StartupClock，说明没生效）")

    # ── 4) 收尾 ───────────────────────────────────────────────────────────
    print("\n  [3/3] 收尾")
    kill_tree(proc)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)

    if c.failed:
        print("\n=== 包内导入诊断（定位真实缺失模块）===")
        try:
            diagnose_imports(exe_dir)
        except Exception as exc:
            print(f"  诊断本身失败：{type(exc).__name__}: {exc}")
        print(f"\n启动器日志里的异常行（{launch_log}）：")
        dump_log_errors(text, 20)
        print(f"\n结果：{len(c.failed)} 项失败 → {', '.join(c.failed)}")
        print("❌ 冒烟测试未通过")
        return 1
    print("\n✅ 冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
