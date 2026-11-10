"""长音频流式 Runner 的端到端测试（简体中文）。

可直接运行，不依赖 pytest：
    python tests/test_longform_runner.py
    python tests/test_longform_runner.py --real-vad /path/silero_vad_v4.onnx
    python tests/test_longform_runner.py --minutes 4.9

测试覆盖：
  1. 流式分段 + 增量 SRT / JSONL 落盘
  2. 时间轴单调、段长不超过 max_chunk_secs
  3. 断点续跑（done 状态不重复写）
  4. 取消 → paused → 再次启动续跑
  5. 无 ffmpeg 的 ArrayPcmStream 备用路径
  6. 内存峰值不随文件长度增长（长音频压力测试）
  7. 可选：真实 Silero VAD ONNX 分段

说明：ASR 引擎使用 mock。本测试验证的是调度层（分段、落盘、断点、取消、
时间轴、内存），不验证识别准确率；真实模型推理需在用户本地环境执行。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import tracemalloc
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from longform_runner import (  # noqa: E402
    ArrayPcmStream, LongformRunner, SR, VAD_CHUNK, srt_ts,
)

TMP = Path(tempfile.mkdtemp(prefix="qwenasr_longform_"))
FIXTURE = HERE / "fixtures" / "longform_4min.wav"


def force_rmtree(path: Path):
    """沙箱 / Windows 上偶发「目录非空」，带 chmod 重试地强制清理。"""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerror)


# ───────────────────────── 测试替身 ─────────────────────────

class MockVAD:
    """按帧能量判定语音，模拟 Silero VAD 的 (1,1) 概率输出。"""

    def __init__(self):
        self.calls = 0

    def run(self, _out_names, feed):
        x = feed["input"][0]
        self.calls += 1
        energy = float(np.sqrt(np.mean(x ** 2)))
        p = 0.95 if energy > 0.005 else 0.01
        return np.array([[p]], dtype=np.float32), feed["h"], feed["c"]


class RealVAD:
    """包装真实 Silero VAD ONNX session（输入名与 app.py 一致）。"""

    def __init__(self, onnx_path):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(str(onnx_path),
                                        providers=["CPUExecutionProvider"])
        self.calls = 0

    def run(self, _out_names, feed):
        self.calls += 1
        out, h, c = self.sess.run(None, {
            "input": feed["input"].astype(np.float32),
            "h": feed["h"], "c": feed["c"], "sr": feed["sr"]})
        return out, h, c


class MockEngine:
    """只回传固定文本，记录每次收到的音频长度。"""

    max_chunk_secs = 30

    def __init__(self):
        self.calls: list[tuple[int, str | None]] = []
        self.lock = threading.Lock()

    def transcribe(self, audio, language=None, context=None, **kw):
        self.calls.append((len(audio), language))
        return "这是网课语音识别的测试字幕，包含标点，用于验证断行。"


class CancelEngine(MockEngine):
    """第 N 次推理后触发取消，用于验证断点保存。"""

    def __init__(self, cancel: threading.Event, after: int = 3):
        super().__init__()
        self.cancel = cancel
        self.after = after

    def transcribe(self, audio, language=None, context=None, **kw):
        out = super().transcribe(audio, language, context, **kw)
        if len(self.calls) >= self.after:
            self.cancel.set()
        return out


# ───────────────────────── 工具 ─────────────────────────

def parse_srt(path: Path):
    txt = path.read_text(encoding="utf-8")
    pairs = re.findall(r"(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})", txt)

    def to_s(x):
        hh, mm, rest = x.split(":")
        ss, ms = rest.split(",")
        return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000

    return [(to_s(a), to_s(b)) for a, b in pairs], txt


def read_pcm(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return (np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                .astype(np.float32) / 32768.0)


def make_runner(work: Path, task: str, **kw):
    return LongformRunner(
        out_srt=TMP / f"{task}.srt", work_dir=work,
        task_id=task, max_chunk_secs=30, **kw)


class Result:
    def __init__(self):
        self.items: list[tuple[str, bool, str]] = []

    def check(self, name, ok, detail=""):
        self.items.append((name, bool(ok), detail))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(x[1] for x in self.items)


# ───────────────────────── 测试用例 ─────────────────────────

def test_basic(r: Result, vad_factory, minutes: float, use_ffmpeg: bool):
    """主流程：生成几分钟音频 → 流式分段 → SRT / JSONL / 时间轴。"""
    force_rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(HERE))
    from make_fixture import build
    pcm, rounds = build(minutes)
    fixture = FIXTURE
    fixture.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(fixture), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    total = len(pcm) / SR
    print(f"\n[1] 主流程：{total/60:.2f} 分钟 / {rounds} 轮 / "
          f"{fixture.stat().st_size/1024/1024:.1f} MB")

    vad, eng = vad_factory(), MockEngine()
    events: list[tuple[int, str]] = []
    runner = make_runner(
        TMP / "work1", "t1", engine=eng, vad_sess=vad,
        progress_cb=lambda p, s: events.append((p, s)),
        ffmpeg="ffmpeg", line_splitter=None)
    pcm_source = None if use_ffmpeg else ArrayPcmStream(read_pcm(fixture))
    res = runner.run(fixture, pcm_source=pcm_source, total_sec=total)

    spans, txt = parse_srt(TMP / "t1.srt")
    jsonl = (TMP / "work1" / "t1.segments.jsonl").read_text(
        encoding="utf-8").strip().splitlines()

    r.check("任务状态为 done", res["status"] == "done", str(res))
    r.check("产生了字幕", len(spans) > 0, f"{len(spans)} 条")
    if not spans:
        r.check("后续断言可继续", False, "未产生字幕，跳过时间轴检查")
        return
    r.check("ASR 被调用", len(eng.calls) > 0, f"{len(eng.calls)} 次")
    r.check("VAD 逐帧执行", vad.calls > 0, f"{vad.calls} 帧")
    r.check("段长不超过 30s", all(L <= 30 * SR + 1 for L, _ in eng.calls))
    r.check("时间轴 start<=end", all(a <= b for a, b in spans))
    r.check("时间轴不倒退", all(spans[i][0] >= spans[i - 1][0]
                            for i in range(1, len(spans))))
    r.check("时间轴不越界", spans[-1][1] <= total + 1.0,
            f"末条 {spans[-1][1]:.2f}s / 总长 {total:.2f}s")
    r.check("JSONL 与 SRT 条数一致", len(jsonl) == len(spans),
            f"{len(jsonl)} vs {len(spans)}")
    r.check("进度事件已推送", len(events) > 0, f"{len(events)} 条")
    r.check("SRT 时间格式正确", srt_ts(3661.234) == "01:01:01,234")

    # 断点：done 状态不应重复写
    runner2 = make_runner(TMP / "work1", "t1",
                          engine=MockEngine(), vad_sess=vad_factory())
    res2 = runner2.run(fixture, pcm_source=ArrayPcmStream(read_pcm(fixture)))
    _, txt2 = parse_srt(TMP / "t1.srt")
    r.check("断点续跑不重复写", res2.get("resumed") is True and txt2 == txt)

    # 取消 → paused → 续跑
    cancel = threading.Event()
    eng3 = CancelEngine(cancel, after=3)
    runner3 = make_runner(TMP / "work3", "t3", engine=eng3,
                          vad_sess=vad_factory(), cancel=cancel)
    res3 = runner3.run(fixture, pcm_source=ArrayPcmStream(read_pcm(fixture)),
                       total_sec=total)
    spans3, _ = parse_srt(TMP / "t3.srt")
    r.check("取消后状态为 paused", res3["status"] == "paused", str(res3))
    r.check("取消前字幕已保存", len(spans3) > 0, f"{len(spans3)} 条")

    runner4 = make_runner(TMP / "work3", "t3", engine=MockEngine(),
                          vad_sess=vad_factory(), cancel=threading.Event())
    res4 = runner4.run(fixture, pcm_source=ArrayPcmStream(read_pcm(fixture)),
                       total_sec=total)
    spans4, _ = parse_srt(TMP / "t3.srt")
    r.check("续跑可继续产出", res4["status"] == "done" and len(spans4) > len(spans3),
            f"{len(spans3)} → {len(spans4)} 条")

    # 无 ffmpeg 的 ArrayPcmStream 备用路径
    runner5 = make_runner(TMP / "work5", "t5", engine=MockEngine(),
                          vad_sess=vad_factory())
    res5 = runner5.run(None, pcm_source=ArrayPcmStream(read_pcm(fixture)),
                       total_sec=total)
    spans5, _ = parse_srt(TMP / "t5.srt")
    r.check("ArrayPcmStream 备用路径", res5["status"] == "done" and len(spans5) > 0,
            f"{len(spans5)} 条")


def current_rss_mb() -> float | None:
    """读取当前进程常驻内存（MB）。Linux 用 /proc，Windows 用 psutil（若可用）。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", encoding="utf-8") as f:
                return int(f.read().split()[1]) * 4096 / 1024 / 1024
        except Exception:
            pass
    return None


def test_memory(r: Result, vad_factory):
    """长音频压力：流式过程中的 RSS 应保持平稳，不随进度线性增长。

    采样点在 progress_cb 里（每 512 帧 ≈ 16s 一次）；长音频运行时若滚动缓冲
    失控，RSS 会随采样序号持续抬升，波动会明显放大。
    """
    print("\n[2] 常驻内存平稳性（5 分钟流式过程采样）")
    sys.path.insert(0, str(HERE))
    from make_fixture import build

    pcm, _ = build(5.0, seed=7)
    src = ArrayPcmStream(pcm.astype(np.float32) / 32768.0)
    force_rmtree(TMP / "mem")

    samples: list[float] = []

    def on_progress(_pct, _status):
        v = current_rss_mb()
        if v is not None:
            samples.append(v)

    tracemalloc.start()
    runner = make_runner(TMP / "mem", "mem", engine=MockEngine(),
                         vad_sess=vad_factory(), progress_cb=on_progress)
    runner.run(None, pcm_source=src, total_sec=len(pcm) / SR)
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    if not samples:
        r.check("当前平台可读取 RSS", False, "无法采样，跳过内存断言")
        return

    swing = max(samples) - min(samples)
    print(f"  RSS 采样 {len(samples)} 点：{min(samples):.1f} ~ "
          f"{max(samples):.1f} MiB（波动 {swing:.1f} MiB）")
    print(f"  Python 堆峰值：{py_peak/1024/1024:.1f} MiB（tracemalloc 不含 numpy 原生缓冲）")
    r.check("流式过程 RSS 平稳", swing < 40,
            f"波动 {swing:.1f} MiB（阈值 40）")
    r.check("Python 堆峰值可控", py_peak / 1024 / 1024 < 80,
            f"{py_peak/1024/1024:.1f} MiB（阈值 80）")


def test_real_vad(r: Result, onnx_path: Path):
    """真实 Silero VAD：验证分段参数与输入名契约。"""
    print(f"\n[3] 真实 Silero VAD ONNX：{onnx_path}")
    sys.path.insert(0, str(HERE))
    from make_fixture import build
    pcm, _ = build(0.5, seed=3)
    force_rmtree(TMP / "realvad")
    eng = MockEngine()
    vad = RealVAD(onnx_path)
    runner = make_runner(TMP / "realvad", "rv", engine=eng, vad_sess=vad)
    res = runner.run(None, pcm_source=ArrayPcmStream(pcm.astype(np.float32) / 32768.0),
                     total_sec=len(pcm) / SR)
    spans, _ = parse_srt(TMP / "rv.srt")
    r.check("真实 VAD 可完成推理", res["status"] == "done" and vad.calls > 0,
            f"{vad.calls} 帧 / {len(spans)} 条字幕")
    r.check("真实 VAD 切出了语音段", len(eng.calls) > 0, f"{len(eng.calls)} 组")


def main() -> int:
    ap = argparse.ArgumentParser(description="长音频流式 Runner 测试（简体中文）")
    ap.add_argument("--minutes", type=float, default=1.2,
                    help="主流程测试音频时长（分钟），默认 1.2")
    ap.add_argument("--real-vad", type=Path, default=None,
                    help="可选：真实 Silero VAD ONNX 路径")
    ap.add_argument("--skip-ffmpeg", action="store_true",
                    help="跳过 ffmpeg 流式路径，只用 ArrayPcmStream")
    args = ap.parse_args()

    use_ffmpeg = (not args.skip_ffmpeg) and (shutil.which("ffmpeg") is not None)
    print(f"ffmpeg 流式路径：{'启用' if use_ffmpeg else '跳过（用 ArrayPcmStream）'}"
          f"；numpy {np.__version__}")

    r = Result()
    test_basic(r, MockVAD, args.minutes, use_ffmpeg)
    test_memory(r, MockVAD)
    if args.real_vad:
        if args.real_vad.exists():
            test_real_vad(r, args.real_vad)
        else:
            r.check("真实 VAD 文件存在", False, str(args.real_vad))

    passed = sum(1 for _, ok, _ in r.items if ok)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    force_rmtree(TMP)
    if FIXTURE.exists():
        FIXTURE.unlink()
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
