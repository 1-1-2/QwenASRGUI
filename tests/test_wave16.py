#!/usr/bin/env python3
"""Wave 16 回归：VAD 单线程 / 关窗拦截与优雅取消 / 批量页改版。

本轮的共同点是「删掉或推翻既有设计」，所以这些用例大多在守「旧的确实没了、
新的确实在，而且理由写明」——单看某一条很容易回归时静默放行。
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Result:
    def __init__(self):
        self.items: list[tuple[bool, str, str]] = []

    def check(self, name: str, cond: bool, detail: str = ""):
        self.items.append((bool(cond), name, detail))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ── 1. VAD session 必须单线程（实测 5.2 倍差距的根因）─────────────────
def test_vad_single_thread(r: Result):
    print("\n[1] VAD session 用单线程，别按核数开池")
    app = _read("app.py")
    m = re.search(r"_vad_so\s*=\s*ort\.SessionOptions\(\)(.{0,400}?)InferenceSession\(\s*\n?\s*str\(vad_path\),\s*_vad_so",
                  app, re.S)
    r.check("VAD 用独立的 SessionOptions 构造", m is not None)
    blk = m.group(1) if m else ""
    r.check("intra_op_num_threads = 1", "intra_op_num_threads = 1" in blk)
    r.check("inter_op_num_threads = 1", "inter_op_num_threads = 1" in blk)
    r.check("注入到 InferenceSession 而非丢掉", "_vad_so" in app.split("InferenceSession(")[-2][:300]
            or "_vad_so\n" in app or "_vad_so," in app)
    # 只改 VAD：encoder / decoder 仍必须走 cpu_cfg 的默认多线程（大 GEMM 需要）
    r.check("encoder 仍用 cpu_cfg（未误改大模型线程）",
            re.search(r"self\.audio_enc\s*=\s*core\.compile_model\([^)]*cpu_cfg", app, re.S) is not None
            or "cpu_cfg" in app.split("_compile_decoder")[0])
    r.check("代码里写明了为何单线程（含实测数字）",
            "5.2 倍" in app or "5.2" in blk or "109 秒" in app)


# ── 2. 关窗 beforeunload 拦截 ────────────────────────────────────────
def test_beforeunload(r: Result):
    print("\n[2] 关窗前拦截：仅在真有任务在跑时生效")
    app = _read("webview/js/app.js")
    r.check("注册了 beforeunload", 'addEventListener("beforeunload"' in app)
    r.check("调用了 preventDefault", "e.preventDefault()" in app)
    r.check("设置了 returnValue（Chromium 需要）", "e.returnValue" in app)
    blk = app.split('addEventListener("beforeunload"')[1].split("});")[0]
    r.check("只在指定 scope 忙时拦截", "_busyScopes()" in blk and "_GUARD_SCOPES" in app)
    r.check("拦截范围是 transcribe / batch", '"transcribe", "batch"' in app)
    r.check("空闲时不拦截（直接 return）",
            re.search(r"if\s*\(!_busyScopes\(\)\.length\)\s*return;", blk) is not None)
    # "model"（下载/加载）刻意不拦，避免长下载变成噪音
    r.check("model scope 不在拦截范围", '"model"' not in app.split("_GUARD_SCOPES")[1][:60])


# ── 3. 关窗优雅取消（行为）───────────────────────────────────────────
def test_graceful_cancel(r: Result):
    print("\n[3] 关窗优雅取消：空闲不打扰 / 忙时等落盘 / 超时不死等")
    import app_webview as aw

    # 隔離真實桌面：本機若正好開著程式視窗，_find_app_hwnd() 會一直回報
    # 「視窗還活著」，這個純行為用例就會無限等待（不是產品 bug）。
    aw._find_app_hwnd = lambda: 0

    class FakeBE:
        def __init__(self, busy=True, stop_after=1.0):
            self.busy = busy
            self.stop_after = stop_after
            self.canceled = False
            self.t0 = None

        def work_in_flight(self):
            if self.t0 and time.monotonic() - self.t0 >= self.stop_after:
                self.busy = False
            return self.busy

        def cancel_all(self):
            self.canceled = True
            self.t0 = time.monotonic()
            return True

    class FakeSrv:
        def __init__(self, be, alive):
            self.backend = be
            self._alive = alive

        def window_alive(self):
            return self._alive

    # 空闲：不取消，立即返回
    be = FakeBE(busy=False)
    t = time.monotonic()
    got = aw._wait_for_window_close(FakeSrv(be, False), idle_grace=0.05, cancel_grace=2)
    r.check("空闲关窗不触发取消", got is False and be.canceled is False)
    r.check("空闲关窗立即返回（<0.5s）", time.monotonic() - t < 0.5,
            f"{time.monotonic() - t:.2f}s")

    # 忙：触发取消并等到停止
    be = FakeBE(busy=True, stop_after=0.5)
    got = aw._wait_for_window_close(FakeSrv(be, False), idle_grace=0.05, cancel_grace=3)
    r.check("忙时关窗触发取消", got is True and be.canceled is True)

    # 取消后迟迟不停：超时放行，不能卡住进程
    be = FakeBE(busy=True, stop_after=999)
    t = time.monotonic()
    got = aw._wait_for_window_close(FakeSrv(be, False), idle_grace=0.05, cancel_grace=0.6)
    el = time.monotonic() - t
    r.check("超时放行（不死等）", got is True and el < 2.0, f"{el:.2f}s")

    # 窗口仍开着：绝不取消
    be = FakeBE(busy=True, stop_after=999)
    fired = threading.Event()

    def run():
        aw._wait_for_window_close(FakeSrv(be, True), idle_grace=0.05, cancel_grace=0.5)
        fired.set()

    threading.Thread(target=run, daemon=True).start()
    r.check("窗口仍在时不会退出、也不会取消",
            not fired.wait(0.5) and be.canceled is False)

    # 只在超时上限内等待，常量存在且有限
    r.check("有明确的等待上限常量",
            isinstance(getattr(aw, "_CLOSE_CANCEL_WAIT_SEC", None), float)
            and aw._CLOSE_CANCEL_WAIT_SEC <= 60)


def test_backend_hooks(r: Result):
    print("\n[4] 后端提供 work_in_flight / cancel_all")
    wb = _read("webview_backend.py")
    r.check("有 work_in_flight()", "def work_in_flight(" in wb)
    r.check("有 cancel_all()", "def cancel_all(" in wb)
    r.check("work_in_flight 同时看 _running 与队列",
            "self._running > 0" in wb.split("def work_in_flight(")[1][:600] and
            "batch_queue" in wb.split("def work_in_flight(")[1][:600])
    r.check("cancel_all 复用 cancel()",
            "self.cancel()" in wb.split("def cancel_all(")[1][:600])


# ── 5. 批量页：拖放取消 + 加入文件按钮 ──────────────────────────────
def test_batch_page(r: Result):
    print("\n[5] 批量页：取消拖放并说明，恢复加入文件按钮")
    html = _read("webview/index.html")
    app = _read("webview/js/app.js")
    css = _read("webview/css/app.css")
    i18n = _read("webview/js/i18n.js")

    r.check("拖放区已移除", 'id="batch-drop"' not in html)
    r.check("改为取消原因说明块", 'id="batch-nodrop"' in html)
    r.check("说明块有标题与原因", 'data-i18n="batch.noDropTitle"' in html and
            'data-i18n="batch.noDropWhy"' in html)
    r.check("说明块有样式", ".batch-nodrop" in css and ".nd-title" in css)
    r.check("旧的 .batch-drop 样式已清理", ".batch-drop {" not in css)
    r.check("「加入檔案」按钮已恢复", 'id="btn-batch-add"' in html)
    r.check("按钮点 pickBatchFiles", "pickBatchFiles();" in app)
    r.check("拖放 JS 全部移除",
            "batchDrop" not in app and "extractLocalPaths" not in app)
    r.check("三语齐备 noDropTitle/noDropWhy/addFiles",
            all(k in i18n for k in ("batch.noDropTitle", "batch.noDropWhy", "batch.addFiles")))


# ── 6. 批量页：路径框常显 + 操作指引 ───────────────────────────────
def test_path_panel(r: Result):
    print("\n[6] 路径框常显、框头提示、含 Win10/Win11 取路径方法")
    html = _read("webview/index.html")
    app = _read("webview/js/app.js")
    css = _read("webview/css/app.css")
    i18n = _read("webview/js/i18n.js")

    r.check("路径框不再 hidden", re.search(r'id="batch-paths"(?![^>]*hidden)', html) is not None)
    r.check("去掉「貼上本機路徑」按钮", 'id="btn-batch-path"' not in html)
    r.check("去掉关闭按钮", 'id="btn-batch-path-close"' not in html)
    r.check("框头有标题", 'data-i18n="batch.pathTitle"' in html)
    r.check("框头有操作方法提示", 'data-i18n="batch.pathHowTo"' in html)
    r.check("框头有样式", ".bp-head" in css and ".bp-title" in css and ".bp-tip" in css)
    r.check("JS 不再有展开/关闭逻辑",
            "btn-batch-path-close" not in app and "pathPanel.hidden = !pathPanel.hidden" not in app)
    r.check("加入路徑按钮仍在", 'id="btn-batch-path-add"' in html)

    m = re.search(r'"batch\.pathHowTo":\s*\[(.*?)\],\n', i18n, re.S)
    body = m.group(1) if m else ""
    r.check("三语齐备", body.count('"') >= 6)
    r.check("写明 Win10 的 Shift+右键「複製檔案地址」",
            "Shift" in body and ("複製檔案地址" in body or "复制文件地址" in body
                                 or "Copy as path" in body))
    r.check("写明 Win11 的 Ctrl+Shift+C", "Ctrl+Shift+C" in body)


# ── 7. VAD 单线程不影响输出（数值一致性）─────────────────────────────
def test_vad_output_identical(r: Result):
    print("\n[7] 单线程与默认线程 VAD 输出逐值相同")
    try:
        import numpy as np
        import onnxruntime as ort
    except Exception as e:                                   # pragma: no cover
        r.check("依赖可用", False, str(e))
        return
    p = ROOT / "ov_models" / "silero_vad_v4.onnx"
    if not p.exists():
        r.check("VAD 模型存在", False, str(p))
        return
    CH, SR = 512, 16000
    sr = np.array(SR, dtype=np.int64)
    x = np.random.default_rng(0).standard_normal((80, CH)).astype(np.float32)

    def infer(so):
        s = ort.InferenceSession(str(p), so, providers=["CPUExecutionProvider"])
        h = np.zeros((2, 1, 64), np.float32)
        c = np.zeros((2, 1, 64), np.float32)
        out = []
        for i in range(len(x)):
            o, h, c = s.run(None, {"input": x[i:i + 1], "h": h, "c": c, "sr": sr})
            out.append(float(o[0, 0]))
        return np.array(out)

    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    a, b = infer(None), infer(so)
    r.check("逐值完全一致（max|diff| == 0）", bool(np.array_equal(a, b)),
            f"maxdiff={float(np.abs(a - b).max()):.2e}")


def main() -> int:
    r = Result()
    test_vad_single_thread(r)
    test_beforeunload(r)
    test_graceful_cancel(r)
    test_backend_hooks(r)
    test_batch_page(r)
    test_path_panel(r)
    test_vad_output_identical(r)
    ok = sum(1 for c, _, _ in r.items if c)
    print(f"\nResult: {ok}/{len(r.items)} passed")
    return 0 if ok == len(r.items) else 1


if __name__ == "__main__":
    sys.exit(main())
