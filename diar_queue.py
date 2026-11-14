"""说话人向量队列（轻量、无门控）。

说话人模型只有 32.5 MB、每段毫秒级，不需要门控也不需要状态显示。
它只需要一件事：**不要跑在识别线程里**。所以这里同样用「入队 + 后台单线程」，
识别侧只付出一次 append 的代价。

音频由 worker 自己按语音段区间从源文件解出 —— 与对齐管线同一套做法，
不复制识别的输入、不建缓冲池。
"""
from __future__ import annotations

import collections
import json
import threading
import time
from pathlib import Path


class DiarQueue:
    def __init__(self, embed_fn, src_path, out_path, tick: float = 0.5):
        """
        embed_fn : ``(src_path, t0, t1) -> vector | None``
        out_path : 向量文件（jsonl，每段一行）
        """
        self.embed_fn = embed_fn
        self.src_path = str(src_path)
        self.out_path = Path(out_path)
        self.tick = float(tick)
        self._pending: collections.deque[tuple[float, float]] = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._written = 0
        self._failed = 0
        self._dropped = 0

    def submit(self, t0: float, t1: float) -> None:
        if t1 - t0 < 0.3:
            return
        with self._cv:
            self._pending.append((float(t0), float(t1)))
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop,
                                                name="diar-embed", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._pending:
                    if self._stop.is_set():
                        return
                    self._cv.wait(self.tick)
                    continue
                t0, t1 = self._pending.popleft()
            try:
                vec = self.embed_fn(self.src_path, t0, t1)
            except Exception:
                vec = None
            if vec is None:
                self._failed += 1
                continue
            try:
                self.out_path.parent.mkdir(parents=True, exist_ok=True)
                with self.out_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"t0": round(t0, 3), "t1": round(t1, 3),
                         "vec": [round(float(x), 5) for x in vec]},
                        ensure_ascii=False) + "\n")
                    f.flush()
                self._written += 1
            except OSError:
                self._failed += 1

    def stats(self) -> dict:
        return {"written": self._written, "failed": self._failed,
                "dropped": self._dropped, "pending": len(self._pending)}

    def close(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._cv:
                if not self._pending:
                    break
            if self._thread is None or not self._thread.is_alive():
                break
            time.sleep(0.05)
        with self._cv:
            self._dropped += len(self._pending)
            self._pending.clear()
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
