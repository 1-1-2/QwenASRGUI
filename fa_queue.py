"""按内存门控拉起的对齐队列（独立管线）。

设计要点
--------
* ``submit()`` 只入队，**永不阻塞识别线程**；识别只管往下跑。
* 单 worker 线程，同一时刻最多一个窗口在飞。
* 门控只有一个判据：系统可用物理内存 ≥ 阈值（默认 4.0 GB）。
  不通过时不排队等待、不降级 —— 只是这一轮不拉起，下一拍再看。
* 对齐进程**不常驻**：每窗起一次、跑完退出，内存随之归还。
* 音频由 worker **自己从源文件解出这一窗**，不复制识别的输入、不建缓冲池。
* 结果按**行号**追加到词级文件，落盘即游标；崩溃最多丢最后一个窗口。
"""
from __future__ import annotations

import collections
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 门控阈值 = 对齐运行峰值 3.5 GB + 安全余量 0.5 GB（说话人的 0.1 折进余量）
DEFAULT_THRESHOLD_GB = 4.0
# 实测单位成本 T/L 的甜点在 20~30 秒；超过 40 秒超线性惩罚开始明显
DEFAULT_WINDOW_SEC = 26.0
MAX_WINDOW_SEC = 40.0


@dataclass
class AlignSeg:
    """一个待对齐的识别段：音频区间 + 已写出的行。"""

    seg: int                    # 段序号（1 起，单调）
    index: int                  # 该段第一条字幕行的行号
    t0: float
    t1: float
    text: str
    lines: list = field(default_factory=list)   # [(start, end, text), ...]


def _default_avail() -> float:
    """可用物理内存（GB）。查不到返回 0 = 门关着（保守，不冒险拉起对齐）。"""
    try:
        import sysmem
        v = sysmem.available_gb()
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


class AlignQueue:
    """Memory-gated, non-blocking alignment queue."""

    def __init__(self, align_fn, src_path, out_path, language=None,
                 threshold_gb: float = DEFAULT_THRESHOLD_GB,
                 window_sec: float = DEFAULT_WINDOW_SEC,
                 tick: float = 0.5, avail_fn=None, sample_rate: int = 16000,
                 on_gate=None):
        """
        align_fn : ``(src_path, t0, t1, text, lang) -> [(word, abs_start, abs_end)]``
            必须返回**绝对音频时间**的词级列表；抛异常表示该窗失败。
        src_path : 源音频路径。worker 用它自己解码。
        out_path : 词级文件（jsonl，按行号追加）。
        """
        self.align_fn = align_fn
        self.src_path = str(src_path)
        self.out_path = Path(out_path)
        self.language = language or "Chinese"
        self.threshold_gb = float(threshold_gb)
        self.window_sec = float(window_sec)
        self.tick = float(tick)
        self.avail_fn = avail_fn or _default_avail
        self.sample_rate = sample_rate
        # 门控状态翻转时回调一次（action, avail, threshold, reason）。
        # 只有翻转才回调 —— 500ms 一次全打会把日志刷满。
        self.on_gate = on_gate
        self._reported = None

        self._pending: collections.deque[AlignSeg] = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._draining = threading.Event()
        self._thread: threading.Thread | None = None

        # 统计 / 状态
        self._running = False
        self._ever_ran = False
        self._block = "empty"        # mem / window / empty
        self._windows = 0
        self._failures = 0
        self._dropped = 0
        self._fa_sec = 0.0
        self._audio_sec = 0.0
        self._seg_no = 0
        self._last_index = 0
        # 双进度用：已提交的行数 / 已写出的行数
        self._lines_total = 0
        self._lines_done = 0

    # ── 生产者侧 ────────────────────────────────────────────────
    def submit(self, t0: float, t1: float, text: str, lines: list,
               index: int) -> int:
        """入队一段；返回段序号。O(1)，不等待任何东西。"""
        with self._cv:
            self._seg_no += 1
            seg = AlignSeg(seg=self._seg_no, index=int(index),
                           t0=float(t0), t1=float(t1),
                           text=str(text or ""), lines=list(lines or []))
            self._pending.append(seg)
            self._last_index = max(self._last_index, int(index) + len(lines) - 1)
            self._lines_total += len(lines)
            self._ensure_thread()
            self._cv.notify_all()
            return seg.seg

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="fa-align",
                                        daemon=True)
        self._thread.start()

    # ── 状态 ────────────────────────────────────────────────────
    def running(self) -> bool:
        return self._running

    def ever_ran(self) -> bool:
        return self._ever_ran

    def block_reason(self) -> str:
        """最近一次没有拉起的原因：``mem`` / ``window`` / ``empty``。"""
        if self._running:
            return ""
        if not self._pending:
            return "empty"
        return self._block

    def pending_sec(self) -> float:
        with self._cv:
            return sum(max(0.0, s.t1 - s.t0) for s in self._pending)

    def pending_count(self) -> int:
        with self._cv:
            return len(self._pending)

    def stats(self) -> dict:
        return {
            "windows": self._windows,
            "failures": self._failures,
            "dropped": self._dropped,
            "windowSec": round(self.window_sec, 2),
            "faSec": round(self._fa_sec, 2),
            "audioSec": round(self._audio_sec, 1),
            "pending": self.pending_count(),
            "thresholdGb": self.threshold_gb,
            "linesTotal": self._lines_total,
            "linesDone": self._lines_done,
        }

    # ── worker ──────────────────────────────────────────────────
    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._pending:
                    if self._stop.is_set():
                        return
                    self._block = "empty"
                    self._cv.wait(self.tick)
                    continue
                draining = self._draining.is_set()
                if not draining:
                    span = self._pending_span()
                    if span < self.window_sec:
                        # 窗口未满：等下一段。收尾时才强制放行。
                        self._block = "window"
                        self._cv.wait(self.tick)
                        continue

            if not self._draining.is_set():
                try:
                    avail = float(self.avail_fn())
                except Exception:
                    avail = 0.0
                if avail < self.threshold_gb:
                    self._block = "mem"
                    self._report_gate("closed", avail, "mem")
                    time.sleep(self.tick)
                    continue

            win = self._take_window()
            if not win:
                continue
            self._block = ""
            try:
                self._report_gate("open", float(self.avail_fn()), "")
            except Exception:
                pass
            self._run_window(win)

    def _report_gate(self, action: str, avail: float, reason: str) -> None:
        if self.on_gate is None or action == self._reported:
            return
        self._reported = action
        try:
            self.on_gate(action, avail, self.threshold_gb, reason)
        except Exception:
            pass

    def _pending_span(self) -> float:
        if not self._pending:
            return 0.0
        first = self._pending[0]
        last = self._pending[-1]
        return max(0.0, last.t1 - first.t0)

    def _take_window(self) -> list[AlignSeg] | None:
        with self._cv:
            if not self._pending:
                return None
            first = self._pending[0]
            take = 1
            for i, seg in enumerate(self._pending):
                if i == 0:
                    continue
                if seg.t1 - first.t0 > min(self.window_sec, MAX_WINDOW_SEC):
                    break
                take = i + 1
            return [self._pending.popleft() for _ in range(take)]

    def _run_window(self, win: list[AlignSeg]) -> None:
        t0 = win[0].t0
        t1 = max(s.t1 for s in win)
        text = "".join(s.text for s in win)
        self._running = True
        self._ever_ran = True
        started = time.perf_counter()
        words: list = []
        try:
            words = self.align_fn(self.src_path, t0, t1, text, self.language) or []
        except Exception:
            words = []
        finally:
            self._fa_sec += time.perf_counter() - started
            self._audio_sec += max(0.0, t1 - t0)
            self._running = False
        if not words:
            self._failures += 1
        try:
            self._write(win, words)
        except Exception:
            self._failures += 1
        self._windows += 1

    # ── 词 → 行 ─────────────────────────────────────────────────
    def _write(self, win: list[AlignSeg], words: list) -> None:
        """把整窗的词按时间归属到段、再归属到行，逐行追加。"""
        buckets: dict[int, list] = {}
        used = [False] * len(words)
        for seg in win:
            picked = []
            for i, (w, a, b) in enumerate(words):
                if used[i]:
                    continue
                mid = (float(a) + float(b)) / 2.0
                if seg.t0 <= mid < seg.t1:
                    picked.append((w, a, b))
                    used[i] = True
            # 兜底：这一整段一个词都没分到，但窗口里有词 —— 取时间上最近的
            # 那个补进来，保证「有语音的段一定有词级」，不整段空着。
            if not picked and words and seg.lines:
                best_i, best_d = -1, 1e9
                for i, (_w, a, b) in enumerate(words):
                    if used[i]:
                        continue
                    mid = (float(a) + float(b)) / 2.0
                    d = min(abs(mid - seg.t0), abs(mid - seg.t1))
                    if d < best_d:
                        best_i, best_d = i, d
                if best_i >= 0:
                    picked.append(words[best_i])
                    used[best_i] = True
            for k, (w, a, b) in enumerate(picked):
                line_i = self._line_for(seg, (float(a) + float(b)) / 2.0, k,
                                        len(picked))
                buckets.setdefault(line_i, []).append(
                    [str(w), round(float(a), 3), round(float(b), 3)])

        lines = [(seg.index + i, ln) for seg in win
                 for i, ln in enumerate(seg.lines)]
        self._lines_done += len(lines)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with self.out_path.open("a", encoding="utf-8") as f:
            for idx, _ln in lines:
                f.write(json.dumps({"index": idx,
                                    "words": buckets.get(idx, [])},
                                   ensure_ascii=False) + "\n")
            f.flush()

    @staticmethod
    def _line_for(seg: AlignSeg, mid: float, k: int, n: int) -> int:
        """词落在哪一行：优先按时间；时间越界时按序号比例落。"""
        for i, ln in enumerate(seg.lines):
            try:
                if float(ln[0]) <= mid < float(ln[1]):
                    return seg.index + i
            except (TypeError, ValueError, IndexError):
                continue
        if not seg.lines:
            return seg.index
        i = 0 if n <= 1 else min(len(seg.lines) - 1,
                                 int(k * len(seg.lines) / max(1, n)))
        return seg.index + i

    # ── 收尾 ────────────────────────────────────────────────────
    def close(self, timeout: float = 120.0) -> None:
        """把队列里剩下的段跑完再退出；超时则放弃并记数。"""
        self._draining.set()
        with self._cv:
            self._cv.notify_all()
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
            self._thread.join(timeout=5.0)
            self._thread = None
