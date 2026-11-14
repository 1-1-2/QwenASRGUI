"""fa_batch.py — 自适应窗口的批量 ForcedAligner 队列。

背景
----
逐段对齐时每段都要重新起一次 ``chatllm/main.exe``、重新载入约 2.9 GB 权重。
实测 FA 耗时近似 ``T(L) = a + b·L + c·L²``：a ≈ 0.8~0.9 s 是每次子进程的固定
开销，L 太大又会因解码变长而超线性。两端夹击使单位成本 T/L 呈 U 型，本机实测
甜点在 20~30 秒（10 s 0.168、20 s 0.127、30 s 0.116、60 s 0.138 秒/秒音频）。

做法
----
ASR 主线程只把 ``(chunk, text, t0, t1)`` 推入队列（``submit`` 立即返回），
累计音频达到窗口 W 才合并成一次 FA 请求，并交给后台单线程执行；结果按提交
顺序由 ``settle`` 取回。W 由前几次实测的 (L, T) 在线拟合，取使 T/L 最小的 L，
并夹在 [MIN_WINDOW, MAX_WINDOW]。

与 ``fa_pipeline`` 的差别
------------------------
``fa_pipeline`` 是一段一次 FA，深度固定 1，组内提交完就收 —— 实际等价于同步。
这里把「攒批」与「不阻塞」合成一件事：窗口攒满即派发到后台，主线程继续跑
ASR，只在真正要写字幕时才 ``settle`` 那一窗。FA 比 ASR 快，所以正常情况下
settle 不会真的等待。

失败语义
--------
FA 返回空、时间覆盖明显不足、抛异常 —— 一律整窗降级为该窗每段返回 ``None``。
调用方拿到 ``None`` 会退回比例切行器，所以「对齐失败」永远不丢字幕，只是
没有词级时间戳。

依赖
----
仅 numpy + fa_segmenter / subtitle_lines 的纯函数。``align_fn`` 由调用方注入，
签名 ``(pcm_ndarray, ref_text, lang) -> [(word, local_start, local_end), ...]``，
便于单测不加载任何模型。
"""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

SR = 16000
DEFAULT_WINDOW = 25.0
MIN_WINDOW = 15.0
MAX_WINDOW = 40.0
FIT_SAMPLES = 3          # 至少几次观测才开始调整窗口
MIN_COVERAGE = 0.5       # FA 时间覆盖低于音频长度此比例 → 视为静默降级


@dataclass
class _Pending:
    chunk: np.ndarray
    text: str
    t0: float
    t1: float
    idx: int


@dataclass
class Handle:
    """``submit`` 的返回值，只记录全局序号。"""
    idx: int
    empty: bool = False


@dataclass
class _Window:
    future: Future
    idxs: list[int]
    left: int


@dataclass
class FaBatch:
    """自适应窗口的 FA 批量队列。

    ``align_fn`` 接收 (拼接后的 pcm, 拼接后的文本, 语言)，返回相对窗口音频
    起点的 ``(word, start, end)`` 列表；返回空或抛异常时整窗降级。
    ``async_worker=False`` 时在调用线程内直接执行，供单测使用。
    """

    align_fn: Callable
    language: str = "Chinese"
    sample_rate: int = SR
    initial_window: float = DEFAULT_WINDOW
    min_window: float = MIN_WINDOW
    max_window: float = MAX_WINDOW
    async_worker: bool = True
    max_windows: int = 4        # 未收取的窗口数上限（背压，防止无限堆积）

    window_sec: float = field(init=False)
    observations: list[tuple[float, float]] = field(init=False)
    windows: int = field(init=False, default=0)
    failures: int = field(init=False, default=0)
    _queue: list[_Pending] = field(init=False, default_factory=list)
    _queued_sec: float = field(init=False, default=0.0)
    _slots: dict[int, tuple[_Window, int]] = field(init=False, default_factory=dict)
    _next_idx: int = field(init=False, default=0)
    _prev_end: float | None = field(init=False, default=None)
    # 收取时的等待量 = FA 没被 ASR 藏住的那部分。调用方据此加深队列。
    _wait_sum: float = field(init=False, default=0.0)
    _wait_n: int = field(init=False, default=0)
    _executor: ThreadPoolExecutor | None = field(init=False, default=None)
    _windows_out: list[_Window] = field(init=False, default_factory=list)

    def __post_init__(self):
        if not callable(self.align_fn):
            raise TypeError("align_fn 必须可调用")
        if self.min_window <= 0 or self.max_window < self.min_window:
            raise ValueError("窗口上下限不合法")
        self.window_sec = self._clamp(float(self.initial_window))
        self.observations = []
        if self.async_worker:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="fa-batch")

    # ── 队列 ──────────────────────────────────────────────────
    def _clamp(self, w: float) -> float:
        return max(self.min_window, min(self.max_window, float(w)))

    def submit(self, chunk, text: str, t0: float, t1: float) -> Handle:
        """推入一段，立即返回。窗口攒满时把整窗派发到后台线程。

        空文本没有可对齐的字，直接返回空句柄（该段的字幕行由调用方按比例
        切行写出）。
        """
        if not text:
            return Handle(-1, empty=True)
        pcm = np.ascontiguousarray(np.asarray(chunk, dtype=np.float32))
        if pcm.size == 0:
            return Handle(-1, empty=True)

        p = _Pending(pcm, str(text), float(t0), float(t1), self._next_idx)
        self._next_idx += 1
        self._queue.append(p)
        self._queued_sec += len(pcm) / self.sample_rate
        if self._queued_sec >= self.window_sec:
            self._dispatch()
        return Handle(p.idx)

    def pending_sec(self) -> float:
        return round(self._queued_sec, 3)

    def inflight(self) -> int:
        """已派发但还没被 settle 掉的窗口数。"""
        return len(self._windows_out)

    def _dispatch(self) -> None:
        """把当前队列整窗派发给后台；队列为空则什么也不做。"""
        if not self._queue:
            return
        batch, self._queue = self._queue, []
        self._queued_sec = 0.0
        if self._executor is None:
            fut: Future = Future()
            fut.set_result(self._run_window(batch))
        else:
            fut = self._executor.submit(self._run_window, batch)
        win = _Window(fut, [p.idx for p in batch], len(batch))
        for k, p in enumerate(batch):
            self._slots[p.idx] = (win, k)
        self._windows_out.append(win)
        # 背压：FA 落后太多时等最老的一窗跑完（正常不会触发，FA 比 ASR 快）。
        while len(self._windows_out) > self.max_windows:
            self._windows_out[0].future.result()
            self._windows_out.pop(0)

    def settle(self, handle: Handle | None):
        """取回某段的字幕行（带 words）；降级或空段返回 ``None``。

        正常路径下这一窗早已在后台跑完，这里只是取结果，不阻塞 ASR。
        """
        if handle is None or handle.empty:
            return None
        slot = self._slots.get(handle.idx)
        if slot is None:
            # 该段还压在未攒满的队列里 —— 主动派发，否则会永远等下去。
            self._dispatch()
            slot = self._slots.get(handle.idx)
            if slot is None:
                return None
        win, k = slot
        t0 = time.perf_counter()
        try:
            result = win.future.result()
        except Exception:
            result = None
        # 记下「真的等了多久」。0 表示这一窗完全被 ASR 藏住了。
        waited = time.perf_counter() - t0
        if waited > 0.02:
            self._wait_sum += waited
            self._wait_n += 1
        win.left -= 1
        if win.left <= 0:
            for idx in win.idxs:
                self._slots.pop(idx, None)
            try:
                self._windows_out.remove(win)
            except ValueError:
                pass
        if not result or k >= len(result):
            return None
        return result[k]

    def avg_wait(self) -> float:
        """平均每次 settle 的等待秒数；0 = FA 完全被藏住。"""
        if not self._wait_n:
            return 0.0
        return self._wait_sum / self._wait_n

    def reset_wait(self) -> None:
        self._wait_sum = 0.0
        self._wait_n = 0

    def drain(self) -> None:
        """把残余队列派发并等全部窗口跑完（断点 / 收尾用）。"""
        self._dispatch()
        for win in list(self._windows_out):
            try:
                win.future.result()
            except Exception:
                pass

    def close(self) -> None:
        """文件结束：收尾所有窗口。"""
        self.drain()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # ── 单窗执行 ──────────────────────────────────────────────
    def _run_window(self, batch: list[_Pending]) -> list:
        audio = np.concatenate([p.chunk for p in batch])
        ref = "".join(p.text for p in batch)
        length = len(audio) / self.sample_rate

        items = None
        t = time.perf_counter()
        try:
            items = self.align_fn(audio, ref, self.language)
        except Exception:
            items = None
        elapsed = time.perf_counter() - t

        self.windows += 1
        self.observations.append((round(length, 3), round(elapsed, 3)))
        self.window_sec = self._suggest_window()

        if not items:
            self.failures += 1
            return [None] * len(batch)

        # 静默降级检测：FA 缺 ffmpeg 时会回一份「固定 32 项、只覆盖 0~2s」
        # 的合法 JSON（见 f128731）。字数对不上不算异常 —— FA routinely
        # 吞掉标点，实测 62 字文本只回 54 词，属正常。
        try:
            cover = max(float(b) for _w, _a, b in items)
        except (TypeError, ValueError):
            self.failures += 1
            return [None] * len(batch)
        if length > 0 and cover / length < MIN_COVERAGE:
            self.failures += 1
            return [None] * len(batch)

        starts: list[float] = []
        acc = 0.0
        for p in batch:
            starts.append(acc)
            acc += len(p.chunk) / self.sample_rate

        per_chunk: list[list[tuple[str, float, float]]] = [[] for _ in batch]
        for w, a, b in items:
            try:
                ws, we = float(a), float(b)
            except (TypeError, ValueError):
                continue
            k = _locate(starts, ws)
            per_chunk[k].append((str(w), ws - starts[k], we - starts[k]))

        return [self._lines(p, per_chunk[k]) for k, p in enumerate(batch)]

    def _lines(self, p: _Pending, items: list[tuple[str, float, float]]):
        """单段：字级时间 → 字幕行（与 ``_fa_lines`` 的产出一致）。"""
        if not items:
            return None
        from fa_segmenter import align_to_lines, MIN_GAP
        from subtitle_lines import balance_lines, merge_tails

        try:
            lines = align_to_lines(items, p.text, offset=p.t0, merge=True)
        except Exception:
            return None
        if not lines:
            return None

        if len(items) == len(p.text) and "".join(ln[2] for ln in lines) == p.text:
            # 一字一 token：按字符偏移把词切给每一行，行文本就是原文的
            # 连续切片，归属是确定的。
            off = 0
            buf = []
            for _s, _e, txt in lines:
                buf.append([{"word": w,
                             "start": round(p.t0 + a, 3),
                             "end": round(p.t0 + b, 3)}
                            for w, a, b in items[off:off + len(txt)]])
                off += len(txt)
            # 先按字符偏移把词切给细行，再交给版面层按 ceil(字数/20) 均分 ——
            # 顺序不能反：细行是「声音在哪断得开」的结论，均分是「这屏放几个字」。
            tmp = [(ln[0], ln[1], ln[2], None, buf[i])
                   for i, ln in enumerate(lines)]
            out = [(s, e, t, w) for (s, e, t, _sp, w)
                   in merge_tails(balance_lines(tmp))]
        else:
            # 字数对不上（英文整词 token、数字被拆、标点被吞）：不能按字符
            # 偏移切片，退到「词中点落在哪一行」。
            flat = [(w, p.t0 + a, p.t0 + b) for w, a, b in items]
            out = [(s, e, txt,
                    [{"word": w, "start": round(a, 3), "end": round(b, 3)}
                     for w, a, b in flat if s <= (a + b) / 2 < e])
                   for (s, e, txt) in lines]

        # 跨段单调：本段首行不早于上一段末行 + MIN_GAP。窗口是按提交顺序
        # 串行处理的，所以这个状态可以跨窗口延续。
        if out and self._prev_end is not None:
            s, e, txt = out[0][0], out[0][1], out[0][2]
            floor = self._prev_end + MIN_GAP
            if s < floor - 1e-6 and e > floor + 0.12:
                out[0] = (floor, e, txt) + tuple(out[0][3:])
        if out:
            self._prev_end = out[-1][1]
        return out

    # ── 自适应 ────────────────────────────────────────────────
    def _suggest_window(self) -> float:
        """由 (L, T) 观测拟合 T = a + bL + cL²，取单位成本最低的 L。

        样本不足、c ≤ 0（没观察到超线性）或拟合失败时保持当前值；结果一律
        夹到 [min_window, max_window]。冷启动用 DEFAULT_WINDOW。
        """
        pts = [(L, T) for L, T in self.observations if L > 0 and T > 0]
        if len(pts) < FIT_SAMPLES:
            return self.window_sec
        try:
            a, b, c = _fit_quadratic(pts)
        except Exception:
            return self.window_sec
        if c <= 1e-9 or a <= 0:
            return self.window_sec
        return self._clamp((a / c) ** 0.5)

    def stats(self) -> dict:
        total = sum(t for _l, t in self.observations)
        audio = sum(l for l, _t in self.observations)
        return {
            "windows": self.windows,
            "failures": self.failures,
            "windowSec": round(self.window_sec, 2),
            "pendingSec": self.pending_sec(),
            "inflight": self.inflight(),
            "faSec": round(total, 2),
            "audioSec": round(audio, 1),
            "observations": list(self.observations),
        }


def _locate(starts: list[float], t: float) -> int:
    """二分找出时间 t 落在第几段（升序的段起点）。"""
    lo, hi = 0, len(starts)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= t:
            lo = mid
        else:
            hi = mid
    return lo


def _fit_quadratic(pts: list[tuple[float, float]]):
    """最小二乘解 T = a + b·L + c·L²（正规方程，3×3 高斯消元）。"""
    n = len(pts)
    sums = [0.0] * 5
    rhs = [0.0] * 3
    for L, T in pts:
        for k in range(5):
            sums[k] += L ** k
        rhs[0] += T
        rhs[1] += T * L
        rhs[2] += T * L * L
    A = [
        [float(n), sums[1], sums[2]],
        [sums[1], sums[2], sums[3]],
        [sums[2], sums[3], sums[4]],
    ]
    # 带部分主元的高斯消元；奇异时退化成线性模型（c=0）。
    for i in range(3):
        piv = max(range(i, 3), key=lambda r: abs(A[r][i]))
        if abs(A[piv][i]) < 1e-12:
            return 0.0, 0.0, 0.0
        A[i], A[piv] = A[piv], A[i]
        rhs[i], rhs[piv] = rhs[piv], rhs[i]
        for r in range(i + 1, 3):
            f = A[r][i] / A[i][i]
            for k in range(i, 3):
                A[r][k] -= f * A[i][k]
            rhs[r] -= f * rhs[i]
    x = [0.0] * 3
    for i in (2, 1, 0):
        x[i] = (rhs[i] - sum(A[i][k] * x[k] for k in range(i + 1, 3))) / A[i][i]
    return x[0], x[1], x[2]
