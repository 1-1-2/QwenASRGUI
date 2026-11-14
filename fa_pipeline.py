"""fa_pipeline.py — 让词级对齐与 ASR 重叠执行（深度 1 的流水线）。

问题
----
``LongformRunner`` 的 chunk 循环原本是「ASR(i) → FA(i) → ASR(i+1) → FA(i+1)」。
ForcedAligner 是独立子进程（``chatllm/main.exe``，CPU），ASR 等它属于纯粹空转：
36.7 小时音档实测 FA 需要 157 分钟，这段时间本可以盖在下一段识别上。

做法
----
单线程流水线，深度 1：提交 FA(i) 后立刻回去做 ASR(i+1)，在提交 FA(i+1)
之前收取 FA(i) 并落盘。任意时刻只有 1 个 FA 子进程（峰值 2.9 GB），结果
**严格按提交顺序**收取 —— SRT 序号与 ``_resume_last_end`` 的单调性都依赖
这个顺序，乱序会直接写坏时间轴。

失败语义与同步路径逐字节一致：任何异常或空结果都返回 ``None``，调用方
回退到按字数比例的切行器，字幕永不丢失。

内存
----
开不开由 ``sysmem.can_run_parallel_with_asr()``（FA 2.9 GB + ASR 余裕 2.6 GB）
决定；不够就退回同步路径，宁可慢，不要让机器换页。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

# 同时在飞的 FA 任务数。1 = 只与「下一段 ASR」重叠，内存峰值可预测；
# 加到 2 会同时起两个 2.9 GB 子进程，收益却受 ASR 速度限制，不划算。
DEFAULT_DEPTH = 1


class FaPipeline:
    """把 ``build(chunk, text, t0, t1) -> lines | None`` 放到后台线程执行。

    ``build`` 就是原先同步调用的那个行生成函数（FA 时间戳 → 字幕行），
    内部状态（跨 chunk 的 ``_prev_end``）由单线程顺序执行保证一致。
    """

    def __init__(self, build, depth: int = DEFAULT_DEPTH):
        if not callable(build):
            raise TypeError("FaPipeline 需要一个可调用的 build(chunk, text, t0, t1)")
        self._build = build
        self.depth = max(1, int(depth))
        self._pool = ThreadPoolExecutor(max_workers=self.depth,
                                        thread_name_prefix="fa-pipe")
        self._closed = False
        self.submitted = 0
        self.failed = 0

    # ── 生命周期 ──────────────────────────────────────────────────────
    def submit(self, *args):
        """提交一段。返回 future；已关闭时返回 None（调用方按失败处理）。"""
        if self._closed:
            return None
        self.submitted += 1
        try:
            return self._pool.submit(self._build, *args)
        except RuntimeError:                 # 线程池已关闭（并发 close）
            return None

    def collect(self, future, timeout: float | None = None):
        """收取结果。超时/异常/空结果统一返回 None，调用方自行回退。"""
        if future is None:
            self.failed += 1
            return None
        try:
            out = future.result(timeout=timeout)
        except Exception:
            self.failed += 1
            return None
        if not out:
            self.failed += 1
            return None
        return out

    def close(self, wait: bool = False) -> None:
        """收工。默认不等待 —— 取消时 FA 子进程最长要跑 45 秒（subprocess
        timeout），等它会把「停止」按钮卡住。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._pool.shutdown(wait=wait, cancel_futures=True)
        except TypeError:                    # Python < 3.9
            self._pool.shutdown(wait=wait)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def open_pipeline(build) -> tuple[FaPipeline | None, str]:
    """按当下可用内存决定要不要开流水线。

    返回 ``(pipeline | None, reason)``，reason ∈ ``ok`` / ``lowmem`` / ``error``。
    调用方拿到 ``lowmem`` 时应把「回退串行」写进状态栏——静默变慢比慢更糟。
    """
    try:
        import sysmem
        ok, avail = sysmem.can_run_parallel_with_asr()
    except Exception:
        return None, "error"
    if not ok:
        return None, "lowmem"
    try:
        return FaPipeline(build), "ok"
    except Exception:
        return None, "error"
