"""停止/收尾时管线该怎么关：paused 不 drain、done 才 drain、子进程不弹窗。

为什么要有这个测试：用户实测发现「停止队列后 FA 仍在不停分段、main.exe
反复启停、黑窗闪烁」。根因是 paused 路径也调了 _close_queues()，而
AlignQueue.close() 的语义是「把积压跑完」—— 停止却继续在后台连起十几个
ffmpeg + main.exe，看起来像程序没停下来。

跑法：python tests/test_pipeline_shutdown.py
"""
from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Report:
    def __init__(self):
        self.pass_ = 0
        self.fail = 0
        self.fails: list[str] = []

    def check(self, name: str, cond, detail: str = ""):
        if cond:
            self.pass_ += 1
            print(f"  ✓ {name}")
        else:
            self.fail += 1
            self.fails.append(name)
            print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


class FakeQueue:
    def __init__(self):
        self.closed_with = None
        self.drain_called = False

    def close(self, timeout: float = 120.0):
        self.closed_with = timeout
        self.drain_called = timeout > 0


def main() -> int:
    r = Report()

    print("\n[1] _close_queues 的 drain 参数")
    import longform_runner as lr
    sig = inspect.signature(lr.LongformRunner._close_queues)
    r.check("有 drain 參數", "drain" in sig.parameters)
    r.check("預設 True（正常收尾要補齊）",
            sig.parameters["drain"].default is True)

    print("\n[2] drain=False 時 timeout=0（不跑積壓）")
    runner = object.__new__(lr.LongformRunner)
    q = FakeQueue()
    runner.align_queue = q
    runner.diar_queue = None
    runner.drain_timeout = 180.0
    runner._close_queues(drain=False)
    r.check("align_queue.close(timeout=0)", q.closed_with == 0.0, str(q.closed_with))
    r.check("不會 drain 積壓", q.drain_called is False)

    print("\n[3] drain=True 時用使用者設定的收尾上限")
    q2 = FakeQueue()
    runner.align_queue = q2
    runner.drain_timeout = 240.0
    runner._close_queues(drain=True)
    r.check("timeout=240（跟隨設定）", q2.closed_with == 240.0, str(q2.closed_with))
    r.check("會 drain 積壓", q2.drain_called is True)

    print("\n[4] paused 路徑必須 drain=False，done 路徑必須 drain=True")
    src = (ROOT / "longform_runner.py").read_text(encoding="utf-8")
    # paused 分支：_close_queues(drain=False) 出現在 return {"status": "paused" 之前
    i_paused = src.find('return {"status": "paused", "srt"')
    seg = src[max(0, i_paused - 400):i_paused]
    r.check("paused 前呼叫 _close_queues(drain=False)",
            "_close_queues(drain=False)" in seg)
    # 用 rfind：文件里有两个 return {"status": "done"}，前一个是「上次已跑完」
    # 的短路分支（不需要收尾），末尾那个才是正常完成路径。
    i_done = src.rfind('return {"status": "done", "srt"')
    seg2 = src[max(0, i_done - 1200):i_done]
    r.check("done 前呼叫 _close_queues()（預設 drain）",
            "_close_queues()" in seg2 and "_close_queues(drain=False)" not in seg2,
            "找不到 _close_queues() 或誤用 drain=False")

    print("\n[5] 子進程不彈窗：_decode_window 與 align_backfill 都走 run_hidden")
    wb = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
    r.check("webview_backend 用 run_hidden 解碼窗口",
            "from proc_utils import run_hidden" in wb
            and "run_hidden(cmd, capture_output=True, timeout=180)" in wb)
    r.check("_decode_window 不再裸 subprocess.run",
            "subprocess.run(cmd, capture_output=True, timeout=180)" not in wb)
    ab = (ROOT / "align_backfill.py").read_text(encoding="utf-8")
    r.check("align_backfill 用 run_hidden",
            "from proc_utils import run_hidden" in ab)
    r.check("align_backfill 不再裸 subprocess.run",
            "subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL"
            not in ab)

    print("\n[6] proc_utils 真的會加上不彈窗參數")
    import proc_utils
    r.check("CREATE_NO_WINDOW = 0x08000000",
            proc_utils.CREATE_NO_WINDOW == 0x08000000)
    kw = proc_utils.hidden_kwargs()
    r.check("hidden_kwargs 含 creationflags",
            kw.get("creationflags") == 0x08000000)
    r.check("hidden_kwargs 含 startupinfo",
            kw.get("startupinfo") is not None)
    # 真的跑一次 cmd：確認不會拋錯且能拿到輸出
    out = proc_utils.run_hidden(
        [sys.executable, "-c", "print('hidden-ok')"],
        capture_output=True, text=True, timeout=30)
    r.check("run_hidden 可執行子進程",
            out.returncode == 0 and "hidden-ok" in (out.stdout or ""),
            (out.stdout or out.stderr or "")[:80])

    print("\n[7] 失敗列有行內重試入口")
    app = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    r.check("失敗狀態渲染成 chip-retry 按鈕", "chip-retry" in app)
    r.check("點擊呼叫 batchRetry",
            'row.querySelector("[data-retry]")' in app
            and "API.batchRetry(it.id)" in app)
    css = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
    r.check("chip-retry 有可點樣式", ".chip-retry {" in css
            and "cursor: pointer" in css)

    print("\n" + "=" * 56)
    print(f"通過 {r.pass_} / 失敗 {r.fail}")
    if r.fails:
        print("失敗項：")
        for n in r.fails:
            print("  -", n)
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
