"""独立管线的行为回归：对齐队列 + 运行器接线 + 批次调度。

覆盖三件事（都不需要模型权重 / 网络）：

  1. ``AlignQueue``：submit 不阻塞、窗口攒够才派发、内存门控、词→行归属、
     整窗失败仍保证游标连续、收尾会 drain；
  2. 运行器接线：**对齐开不开、快不快、成不成，SRT 都逐字节相同**；
  3. 断点续跑：中途取消再续，结果与一次跑完逐字节相同。

    python tests/test_parallel_policy.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from batch_queue import BatchQueue          # noqa: E402
from fa_queue import AlignQueue             # noqa: E402
from longform_runner import ArrayPcmStream, LongformRunner, SR  # noqa: E402
import sysmem                               # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


# ───────────────────────── 1. AlignQueue 单元 ─────────────────────────

def _words_for(t0, t1, text):
    """一字一词、按时间均匀铺满；模拟 FA 的返回契约（绝对时间）。"""
    if not text:
        return []
    step = max(1e-3, (t1 - t0) / len(text))
    return [(c, t0 + i * step, t0 + (i + 1) * step)
            for i, c in enumerate(text)]


def _read_fa(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_align_queue(r: Result):
    print("\n[1] AlignQueue：不阻塞 / 窗口 / 门控 / 归属")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        calls: list[tuple[float, float]] = []
        release = threading.Event()

        def slow_align(src, t0, t1, text, lang):
            calls.append((t0, t1))
            release.wait(2.0)                 # 故意拖住，验证 submit 不等它
            return _words_for(t0, t1, text)

        q = AlignQueue(slow_align, src_path="x.wav", out_path=tmp / "a.fa.jsonl",
                       window_sec=2.0, tick=0.02, avail_fn=lambda: 99.0)

        t = time.perf_counter()
        for i in range(3):
            q.submit(i * 2.0, i * 2.0 + 2.0, "一二三四", [(i * 2.0, i * 2.0 + 2.0, "一二三四")],
                     index=i * 1 + 1)
        cost = time.perf_counter() - t
        r.check("submit 不阻塞（3 段 < 50ms）", cost < 0.05, f"{cost * 1000:.1f}ms")
        r.check("窗口未满时不派发", not calls, str(calls))

        release.set()
        deadline = time.time() + 3
        while time.time() < deadline and len(_read_fa(tmp / "a.fa.jsonl")) < 3:
            time.sleep(0.02)
        rows = _read_fa(tmp / "a.fa.jsonl")
        r.check("窗口满后派发并落盘", len(rows) >= 3, str(len(rows)))
        r.check("每个行号都有记录（游标连续）",
                [x["index"] for x in rows[:3]] == [1, 2, 3],
                str([x["index"] for x in rows[:3]]))
        r.check("词按时间归属到行",
                all(x["words"] for x in rows[:3]),
                str([len(x["words"]) for x in rows[:3]]))
        q.close(timeout=3)
        r.check("close 幂等", (q.close(timeout=1), True)[1])

    # 内存门控：不够就一次都不派发
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        hit = []
        q = AlignQueue(lambda *a: hit.append(1) or [],
                       src_path="x.wav", out_path=tmp / "b.fa.jsonl",
                       window_sec=1.0, tick=0.02, avail_fn=lambda: 1.0)
        q.submit(0.0, 2.0, "甲乙", [(0.0, 2.0, "甲乙")], index=1)
        time.sleep(0.4)
        r.check("内存不足时不派发", not hit, str(len(hit)))
        r.check("归因为 mem", q.block_reason() == "mem", q.block_reason())
        r.check("标记为从未运行", q.ever_ran() is False)
        q.close(timeout=1)

    # 整窗失败：仍写空词，保证游标不断
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q = AlignQueue(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                       src_path="x.wav", out_path=tmp / "c.fa.jsonl",
                       window_sec=1.0, tick=0.02, avail_fn=lambda: 99.0)
        q.submit(0.0, 2.0, "丙丁", [(0.0, 2.0, "丙丁")], index=7)
        q.close(timeout=3)
        rows = _read_fa(tmp / "c.fa.jsonl")
        r.check("失败仍写空词（游标不断）",
                rows and rows[0]["index"] == 7 and rows[0]["words"] == [],
                str(rows[:1]))
        r.check("失败计数 +1", q.stats()["failures"] == 1, str(q.stats()))


# ───────────────────────── 2. 运行器接线 ─────────────────────────

class _VAD:
    def run(self, _names, feed):
        x = feed["input"][0]
        e = float(np.sqrt(np.mean(x ** 2)))
        p = 0.95 if e > 0.005 else 0.01
        return np.array([[p]], dtype=np.float32), feed["h"], feed["c"]


class _Engine:
    max_chunk_secs = 30

    def __init__(self, cancel_after: int = 0, cancel=None):
        self.n = 0
        self.cancel_after = cancel_after
        self.cancel = cancel

    def transcribe(self, audio, language=None, context=None, **_kw):
        self.n += 1
        if self.cancel is not None and self.n >= self.cancel_after:
            self.cancel.set()
        return "这是用于验证独立管线接线的测试字幕，包含标点。"


def _pcm(rounds: int = 6) -> np.ndarray:
    """N 段 1 秒语音 + 1 秒静音。

    静音必须长于 ``_MERGE_CH``（16 帧 ≈ 0.5 秒），否则相邻语音会被合并成
    一个 group —— 那样整段只会切出一块，测不出「多段」的行为。
    """
    sr = SR
    quiet = np.zeros(int(1.0 * sr), dtype=np.float32)
    loud = (np.random.default_rng(3).standard_normal(int(1.0 * sr))
            * 0.2).astype(np.float32)
    parts = []
    for _ in range(rounds):
        parts.extend([loud, quiet])
    return np.concatenate(parts)


def _run(tmp: Path, task: str, pcm: np.ndarray, *, align: bool = False,
         cancel_after: int = 0, resume: bool = False,
         cancel: threading.Event | None = None) -> dict:
    fa_path = tmp / f"{task}.fa.jsonl"
    q = None
    if align:
        q = AlignQueue(lambda src, t0, t1, text, lang: _words_for(t0, t1, text),
                       src_path="x.wav", out_path=fa_path,
                       window_sec=2.0, tick=0.02, avail_fn=lambda: 99.0)
    ev = cancel or threading.Event()
    runner = LongformRunner(
        out_srt=tmp / f"{task}.srt", work_dir=tmp / f"w_{task}",
        task_id=task, max_chunk_secs=30,
        engine=_Engine(cancel_after, ev if cancel_after else None),
        vad_sess=_VAD(), cancel=ev, align_queue=q, fa_path=fa_path)
    res = runner.run(tmp / "a.wav", pcm_source=ArrayPcmStream(pcm),
                     total_sec=len(pcm) / SR, resume=resume)
    return res


def test_runner_wiring(r: Result):
    print("\n[2] 运行器接线：对齐不影响字幕")
    pcm = _pcm()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _run(tmp, "off", pcm, align=False)
        _run(tmp, "on", pcm, align=True)
        off = (tmp / "off.srt").read_text(encoding="utf-8")
        on = (tmp / "on.srt").read_text(encoding="utf-8")
        r.check("对齐开关不改变 SRT（逐字节）", off == on,
                "" if off == on else "首个差异见文件")
        rows = _read_fa(tmp / "on.fa.jsonl")
        r.check("对齐结果独立落盘", bool(rows), f"{len(rows)} 行")

    # 对齐整窗失败：SRT 仍然一样
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bad = AlignQueue(lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                         src_path="x.wav", out_path=tmp / "bad.fa.jsonl",
                         window_sec=2.0, tick=0.02, avail_fn=lambda: 99.0)
        runner = LongformRunner(
            out_srt=tmp / "bad.srt", work_dir=tmp / "w_bad", task_id="bad",
            max_chunk_secs=30, engine=_Engine(), vad_sess=_VAD(),
            align_queue=bad, fa_path=tmp / "bad.fa.jsonl")
        runner.run(tmp / "a.wav", pcm_source=ArrayPcmStream(pcm),
                   total_sec=len(pcm) / SR)
        bad_srt = (tmp / "bad.srt").read_text(encoding="utf-8")
        r.check("对齐全失败时 SRT 不受影响", bad_srt.strip() != "")


def test_resume(r: Result):
    print("\n[3] 断点续跑：与一次跑完逐字节相同")
    pcm = _pcm(6)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _run(tmp, "full", pcm)
        full = (tmp / "full.srt").read_text(encoding="utf-8")

        # 跑到第 2 段时取消
        res = _run(tmp, "part", pcm, cancel_after=2)
        r.check("中途取消返回 paused", res["status"] == "paused", res["status"])
        partial = (tmp / "part.srt").read_text(encoding="utf-8")
        r.check("取消时已写出的行保留", partial.strip() != "")

        # 续跑（同一个 task_id / work_dir）
        _run(tmp, "part", pcm, resume=True)
        resumed = (tmp / "part.srt").read_text(encoding="utf-8")
        r.check("续跑结果与一次跑完逐字节相同", resumed == full,
                f"{len(resumed)} vs {len(full)} 字节")

        # 序号连续、无重复
        nums = [int(b.strip().split("\n")[0]) for b in resumed.split("\n\n")
                if b.strip() and b.strip().split("\n")[0].isdigit()]
        r.check("序号连续无重复", nums == list(range(1, len(nums) + 1)),
                f"{len(nums)} 条")


# ───────────────────────── 4. 批次调度 ─────────────────────────

class GateBackend:
    """假的後端：把「識別」與「補做對齊」都擋在同一個閘門上，好觀察順序。"""

    def __init__(self):
        self.gate = threading.Semaphore(0)
        self.events = []
        self.align_started = threading.Event()
        self.asr_started = threading.Event()

    def transcribe(self, opts, progress_cb=None):
        self.events.append((time.time(), "asr"))
        self.asr_started.set()
        self.gate.acquire(timeout=5)
        return {"srtPath": "x.srt", "totalSegments": 1}

    def align_item(self, opts, progress_cb=None, should_stop=None):
        self.events.append((time.time(), "align"))
        self.align_started.set()
        self.gate.acquire(timeout=5)
        return {"srtPath": "x.srt", "totalSegments": 1}


def _queue(tmp: Path, name: str, mem_ok: bool, parallel: bool):
    be = GateBackend()
    q = BatchQueue(be, state_dir=tmp / f"st_{name}")
    ids = []
    for i in range(3):
        p = tmp / f"{name}{i}.wav"
        if not p.exists():
            p.write_bytes(b"RIFF-fake")
        ids.append(q.add(p, p.name)["id"])
    q.mark_align(ids[:2])
    saved = sysmem.can_run_parallel_with_asr
    sysmem.can_run_parallel_with_asr = lambda: (mem_ok, 20.0 if mem_ok else 2.0)
    q.start({"alignParallel": parallel})
    return q, be, saved


def _finish(q, be, saved, releases: int = 3):
    for _ in range(releases):
        be.gate.release()
    end = time.time() + 6
    while time.time() < end and q.running:
        time.sleep(0.02)
    sysmem.can_run_parallel_with_asr = saved


def test_scheduler(r: Result):
    print("\n[4] 批次调度：补齐优先 + 尾段放行")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, saved = _queue(tmp, "off", True, False)
        be.align_started.wait(2)
        r.check("关并行：先跑补齐", be.events and be.events[0][1] == "align",
                str(be.events[:2]))
        be.gate.release()
        time.sleep(0.5)
        r.check("关并行：仍有补齐在跑时识别不启动",
                not be.asr_started.is_set(), str(be.events))
        be.gate.release()
        r.check("关并行：补齐跑完后识别才启动", be.asr_started.wait(4),
                str(be.events))
        _finish(q, be, saved)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, saved = _queue(tmp, "on", True, True)
        # 旧规则「补齐积压 > 1 时识别等待」已删除：识别不再被补做对齐压住。
        r.check("开并行：识别立刻进场（不再等补齐积压清零）",
                be.asr_started.wait(4), str(be.events))
        r.check("开并行：补齐同时在后台跑", be.align_started.wait(4),
                str(be.events))
        _finish(q, be, saved)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, saved = _queue(tmp, "low", False, True)
        # 内存不再是「识别能不能进场」的判据 —— 它只决定对齐拉不拉起。
        # 识别必须无条件往前跑，否则一次误判就压住整条流水线。
        r.check("内存不足：识别照常进场", be.asr_started.wait(4),
                str(be.events))
        _finish(q, be, saved)
        kinds = [k for _t, k in be.events]
        r.check("内存不足：三项最终都跑完", len(kinds) >= 3, str(kinds))


def main() -> int:
    r = Result()
    test_align_queue(r)
    test_runner_wiring(r)
    test_resume(r)
    test_scheduler(r)
    print(f"\n{'ALL PASS' if r.ok else 'FAILED'} — {sum(r.items)}/{len(r.items)}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
