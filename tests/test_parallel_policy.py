"""FA 流水线 + 批次调度的并行策略回归。

覆盖三件事（都不需要模型权重 / 网络）：

  1. ``FaPipeline`` 单线程、严格按提交顺序、失败回退；
  2. 流水线产出的 SRT 与同步路径**逐字节相同**（并行不得改变结果）；
  3. 批次调度：补齐优先，识别只在「剩最后一个补齐文件 + 内存够」时进场。

    python tests/test_parallel_policy.py
"""
from __future__ import annotations

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

from batch_queue import BatchQueue          # noqa: E402
from fa_pipeline import FaPipeline          # noqa: E402
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


# ───────────────────────── 1. FaPipeline ─────────────────────────

def test_pipeline(r: Result):
    print("\n[1] FaPipeline：顺序 / 并发 / 失败回退")
    order, live, peak = [], [0], [0]
    lock = threading.Lock()

    def build(idx):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.03)                     # 模拟 FA 子进程
        with lock:
            live[0] -= 1
            order.append(idx)
        if idx == 2:
            raise RuntimeError("模拟子进程崩溃")
        return [(0.0, 1.0, f"line{idx}")]

    p = FaPipeline(build)
    futs = [p.submit(i) for i in range(2)]
    got = [p.collect(f) for f in futs]
    r.check("顺序返回", [g[0][2] for g in got] == ["line0", "line1"], str(got))
    r.check("单线程执行", peak[0] == 1, f"peak={peak[0]}")

    r.check("异常 → None（调用方回退）", p.collect(p.submit(2)) is None)
    r.check("失败计数", p.failed == 1, str(p.failed))
    p.close()
    r.check("关闭后提交返回 None", p.submit(9) is None)
    p.close()                                 # 幂等
    r.check("close 幂等", True)


# ───────────────────────── 2. 与同步路径等价 ─────────────────────────

class _VAD:
    def run(self, _names, feed):
        x = feed["input"][0]
        e = float(np.sqrt(np.mean(x ** 2)))
        p = 0.95 if e > 0.005 else 0.01
        return np.array([[p]], dtype=np.float32), feed["h"], feed["c"]


class _Engine:
    max_chunk_secs = 30

    def __init__(self):
        self.n = 0

    def transcribe(self, audio, language=None, context=None, **_kw):
        self.n += 1
        return "这是用于验证流水线与同步路径一致性的测试字幕，包含标点。"


def _run_once(tmp: Path, task: str, pcm: np.ndarray, sync: bool):
    """同一个固定行生成器，分别在同步 / 流水线下跑一遍。"""

    def build(chunk, text, t0, t1):
        return [(t0, t1, text)]               # 单行覆盖整段：结果只取决于 t0/t1

    kw = {"fa_lines": build} if sync else {"fa_pipeline": FaPipeline(build)}
    runner = LongformRunner(
        out_srt=tmp / f"{task}.srt", work_dir=tmp / f"w_{task}",
        task_id=task, max_chunk_secs=30, engine=_Engine(), vad_sess=_VAD(), **kw)
    runner.run(tmp / "a.wav", pcm_source=ArrayPcmStream(pcm), total_sec=len(pcm) / SR)
    return (tmp / f"{task}.srt").read_text(encoding="utf-8")


def test_equivalence(r: Result):
    print("\n[2] 流水线 vs 同步：SRT 逐字节相同")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        sys.path.insert(0, str(HERE))
        from make_fixture import build as make_pcm
        pcm, _rounds = make_pcm(0.4)
        with wave.open(str(tmp / "a.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(pcm.tobytes())

        a = _run_once(tmp, "sync", pcm, True)
        b = _run_once(tmp, "pipe", pcm, False)
        r.check("两路都产出了字幕", bool(a.strip()) and bool(b.strip()),
                f"{len(a)} / {len(b)} 字节")
        r.check("逐字节一致", a == b,
                "" if a == b else "首个差异：" + repr(a[:80]) + " vs " + repr(b[:80]))


# ───────────────────────── 3. 批次调度策略 ─────────────────────────

class GateBackend:
    """补齐任务一个一个放行；记录调用顺序。"""

    def __init__(self, lanes: int):
        self.gate = threading.Semaphore(0)
        self.events = []
        self.lock = threading.Lock()
        self.align_started = threading.Event()
        self.asr_started = threading.Event()

    def _name(self, opts):
        return Path(str(opts.get("name"))).name

    def align_item(self, opts, progress_cb=None, should_stop=None):
        with self.lock:
            self.events.append(("start", "align", self._name(opts)))
        self.align_started.set()
        self.gate.acquire(timeout=5)
        with self.lock:
            self.events.append(("end", "align", self._name(opts)))
        return {"srtPath": "x.srt", "ratio": 1.0, "lanes": 1}

    def transcribe(self, opts, progress_cb=None):
        with self.lock:
            self.events.append(("start", "asr", self._name(opts)))
        self.asr_started.set()
        return {"srtPath": "x.srt", "totalSegments": 1}


def _queue(tmp: Path, name: str, mem_ok: bool, parallel: bool, lanes: int = 2):
    be = GateBackend(lanes)
    q = BatchQueue(be, state_dir=tmp / f"st_{name}")
    ids = []
    for i in range(3):
        p = tmp / f"{name}{i}.wav"
        if not p.exists():
            p.write_bytes(b"RIFF-fake")
        ids.append(q.add(p, p.name)["id"])
    q.mark_align(ids[:2])                     # 前两个变成「补做对齐」
    saved = sysmem.can_run_parallel_with_asr
    sysmem.can_run_parallel_with_asr = lambda: (mem_ok, 20.0 if mem_ok else 2.0)
    try:
        q.start({"alignParallel": parallel})
    except Exception:
        sysmem.can_run_parallel_with_asr = saved
        raise
    return q, be, ids, saved


def _finish(q, be, saved, releases: int = 3):
    for _ in range(releases):
        be.gate.release()
    end = time.time() + 6
    while time.time() < end and q.running:
        time.sleep(0.02)
    sysmem.can_run_parallel_with_asr = saved


def test_scheduler(r: Result):
    print("\n[3] 批次调度：补齐优先 + 尾段放行")

    # (a) 关并行：补齐全部跑完才允许识别
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, ids, saved = _queue(tmp, "off", True, False)
        be.align_started.wait(2)
        r.check("关并行：先跑补齐", be.events and be.events[0][1] == "align",
                str(be.events[:2]))
        be.gate.release()                     # 第一个补齐结束，第二个顶上
        time.sleep(0.5)
        r.check("关并行：仍有补齐在跑时识别不启动",
                not be.asr_started.is_set(), str(be.events))
        be.gate.release()
        r.check("关并行：补齐跑完后识别才启动", be.asr_started.wait(4),
                str(be.events))
        _finish(q, be, saved)

    # (b) 开并行 + 内存够：剩最后一个补齐时识别进场
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, ids, saved = _queue(tmp, "on", True, True)
        be.align_started.wait(2)
        time.sleep(0.6)
        r.check("开并行：补齐积压 >1 时识别仍等待",
                not be.asr_started.is_set(), str(be.events))
        be.gate.release()                     # 第一个补齐结束，第二个开始
        r.check("开并行：剩 1 个补齐时识别进场", be.asr_started.wait(4),
                str(be.events))
        _finish(q, be, saved)

    # (c) 开并行但内存不足：退回串行
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        q, be, ids, saved = _queue(tmp, "low", False, True)
        be.align_started.wait(2)
        be.gate.release()
        time.sleep(0.8)
        r.check("内存不足：剩 1 个补齐也不放识别",
                not be.asr_started.is_set(), str(be.events))
        be.gate.release()
        r.check("内存不足：补齐跑完后识别启动", be.asr_started.wait(4),
                str(be.events))
        _finish(q, be, saved)


def main() -> int:
    r = Result()
    test_pipeline(r)
    test_equivalence(r)
    test_scheduler(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
