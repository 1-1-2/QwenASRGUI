"""长音频流式 Runner —— 替换「整段加载 + 末尾一次性写盘」的调度层。

本文件为长音频改造新增模块，注释与文档统一使用简体中文。

解决的问题（面向 1~5 小时网课 / 会议录音）
----------------------------------------
1. 不再把整段音频读进内存：ffmpeg 流式输出 16k / mono / s16le PCM，
   滚动缓冲只保留尚未吐出的语音组，常驻内存与文件长度无关。
2. 不再把全部字幕攒到末尾才写：每完成一个语音组立刻 append 写 SRT 与
   JSONL，中途崩溃、断电、取消，已产出的字幕全部保留。
3. SQLite checkpoint 记录全局帧号与 SRT 序号，支持断点续跑。
4. progress_cb(pct:int, status:str) 与 WebView 现有 SSE 契约完全一致，
   前端零改动。

刻意不做的事
------------
* 不加并发。ASREngine.transcribe() 自带 self._lock 互斥锁，多线程只会
  排队；多进程则每份进程各载一套 IR，内存峰值与锯齿叠加。固定单 worker。
* 不改 OpenVINO 线程策略。PERFORMANCE_HINT / INFERENCE_NUM_THREADS 由
  「模型」页的 CPU 执行绪设置控制，属于独立调优项。
* 削平内存锯齿靠「段边界主动释放 + 峰值不随文件长度增长」，不靠调 OV。

依赖：仅标准库 + numpy。VAD session 与 ASR engine 由调用方注入，便于测试。
"""

from __future__ import annotations

import gc
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

SR = 16000
VAD_CHUNK = 512                  # Silero VAD 固定输入：32ms / 帧
READ_BYTES = 64 * 1024          # 每次读 2.048s PCM，避免几十万次小 read
DEFAULT_MAX_CHUNK_SECS = 30     # 0.6B 上限；1.7B 请传 10

_MIN_CH = 16                    # 最短语音段（帧）≈ 0.5s
_PAD_CH = 5                     # 段前后各补 5 帧 ≈ 0.16s
_MERGE_CH = 16                  # 相邻间隔 ≤16 帧则合并
_TAIL_SEC = 0.35                # 多含尾段，补捉句尾轻声字（与 app.py 一致）


class Cancelled(Exception):
    """用户取消。已产出的字幕与进度会保留。"""


# ───────────────────────── SRT / 断行 ─────────────────────────

def srt_ts(sec: float) -> str:
    """与 subtitle_lines._srt_ts 完全一致的 SRT 时间格式。"""
    ms = int(round(max(0.0, sec) * 1000))
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1_000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def simple_lines(text: str, t0: float, t1: float) -> list[tuple[float, float, str]]:
    """兜底断行：按标点切句，按字符数线性分配时间轴。

    真实环境建议注入 app._split_to_lines + _assign_ts（见集成说明），
    字幕质量与原本的 process_file() 一致。
    """
    if "<asr_text>" in text:
        text = text.split("<asr_text>", 1)[1]
    parts = [p.strip() for p in re.split(r"[，。？！；：,.!?;:\n…—]+", text)
             if p.strip()]
    if not parts:
        return []
    weights = [max(1, len(p)) for p in parts]
    total = sum(weights)
    span = max(0.1, t1 - t0)
    out, cur = [], t0
    for p, w in zip(parts, weights):
        nxt = cur + span * w / total
        out.append((cur, nxt, p))
        cur = nxt
    return out


# ───────────────────────── Checkpoint ─────────────────────────

class Checkpoint:
    """进度记录：已处理到的全局音频帧号 + 下一条 SRT 序号。

    优先 SQLite；若 SQLite 因权限、同步盘、热日志或锁冲突失败，自动降级为
    JSON 文件（Windows + 杀毒软件 / OneDrive 目录下较常见）。两条路径始终
    双写镜像，保证任何一次降级都不会丢进度。
    """

    def __init__(self, db_path: Path, task_id: str):
        self.db_path = Path(db_path)
        self.task_id = task_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path = self.db_path.with_suffix(".json")
        self._lock = threading.Lock()
        self._use_sqlite = True
        try:
            c = self._conn()
            try:
                c.execute("""CREATE TABLE IF NOT EXISTS progress (
                    task_id TEXT PRIMARY KEY,
                    next_frame INTEGER NOT NULL,
                    next_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL)""")
                c.commit()
            finally:
                c.close()
        except sqlite3.Error:
            self._use_sqlite = False

    def _conn(self):
        # 每次操作独立连接并显式 close：`with conn` 只管事务不关连接。
        return sqlite3.connect(str(self.db_path), timeout=30)

    def _json_all(self) -> dict:
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _json_load(self):
        d = self._json_all().get(self.task_id)
        return (int(d["next_frame"]), int(d["next_index"]), d["status"]) if d else (0, 1, "new")

    def _json_save(self, frame, index, status):
        d = self._json_all()
        d[self.task_id] = {"next_frame": int(frame), "next_index": int(index),
                           "status": status}
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.json_path)          # 原子替换，防止写一半崩

    def load(self) -> tuple[int, int, str]:
        with self._lock:
            row = None
            if self._use_sqlite:
                try:
                    c = self._conn()
                    try:
                        row = c.execute(
                            "SELECT next_frame, next_index, status FROM progress WHERE task_id=?",
                            (self.task_id,)).fetchone()
                    finally:
                        c.close()
                except sqlite3.Error:
                    self._use_sqlite = False
            if row:
                return (row[0], row[1], row[2])
            return self._json_load()

    def save(self, next_frame: int, next_index: int, status: str = "running"):
        with self._lock:
            if self._use_sqlite:
                try:
                    c = self._conn()
                    try:
                        c.execute("""INSERT INTO progress
                                     (task_id,next_frame,next_index,status,updated_at)
                                     VALUES(?,?,?,?,?)
                                     ON CONFLICT(task_id) DO UPDATE SET
                                       next_frame=excluded.next_frame,
                                       next_index=excluded.next_index,
                                       status=excluded.status,
                                       updated_at=excluded.updated_at""",
                                  (self.task_id, int(next_frame), int(next_index),
                                   status, time.time()))
                        c.commit()
                    finally:
                        c.close()
                except sqlite3.Error:
                    self._use_sqlite = False
            self._json_save(next_frame, next_index, status)


# ───────────────────────── PCM 源 ─────────────────────────

def ffmpeg_pcm_stream(path: Path, ffmpeg: str = "ffmpeg", start_sec: float = 0.0):
    """yield float32 PCM 块；ffmpeg 常驻子进程，绝不把整段读进内存。

    ``-ss`` 放在 ``-i`` 之后 → 音频精确 seek，断点续跑时时间轴不漂移。
    """
    cmd = [ffmpeg, "-v", "error", "-i", str(path)]
    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.6f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]

    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, **kw)
    completed = False
    try:
        while True:
            raw = proc.stdout.read(READ_BYTES)
            if not raw:
                completed = True
                break
            usable = len(raw) - (len(raw) % 2)   # s16le 每样本 2 字节
            if usable:
                yield (np.frombuffer(raw[:usable], dtype="<i2")
                       .astype(np.float32) / 32768.0)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if completed:
            try:
                rc = proc.wait(timeout=5)
            except Exception:
                proc.kill()
                rc = proc.wait()
            if rc not in (0, None):
                raise RuntimeError(f"ffmpeg 退出码 {rc}")
        else:
            # 消费者提前退出（取消 / 异常）：静默回收，不覆盖真实异常
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


class ArrayPcmStream:
    """离线 / 测试用：把 numpy 数组按块喂进来，完全绕开 ffmpeg。"""
    def __init__(self, audio: np.ndarray, start_sec: float = 0.0):
        self.audio = np.asarray(audio[int(start_sec * SR):], dtype=np.float32)
        self.pos = 0

    def __iter__(self):
        step = READ_BYTES // 2
        while self.pos < len(self.audio):
            yield self.audio[self.pos:self.pos + step]
            self.pos += step


def probe_duration(path, ffmpeg: str = "ffmpeg") -> float | None:
    """探测音频总时长（秒）。失败返回 None，只影响进度百分比显示。"""
    try:
        import soundfile as sf
        return float(sf.info(str(path)).duration)
    except Exception:
        pass
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        m = re.findall(rb"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            h, mi, s = m[0]
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


# ───────────────────────── 主 Runner ─────────────────────────

class LongformRunner:
    """流式分段 + 增量落盘 + 断点续跑 + 取消。单 worker，无并发。"""

    def __init__(self, engine, vad_sess, out_srt, work_dir,
                 ffmpeg: str = "ffmpeg",
                 progress_cb=None, cancel: threading.Event | None = None,
                 max_chunk_secs: int | None = None,
                 language: str | None = None,
                 context: str | None = None,
                 line_splitter=None,
                 task_id: str = "default",
                 gc_every: int = 0,
                 vad_threshold: float = 0.5):
        if vad_sess is None:
            raise ValueError("缺少 VAD session：请传入 engine.vad_sess")
        self.engine = engine
        self.vad_sess = vad_sess
        self.out_srt = Path(out_srt)
        self.work_dir = Path(work_dir)
        self.ffmpeg = ffmpeg
        self.progress_cb = progress_cb or (lambda pct, status: None)
        self.cancel = cancel or threading.Event()
        ceiling = getattr(type(engine), "max_chunk_secs", DEFAULT_MAX_CHUNK_SECS)
        self.max_secs = int(max_chunk_secs or ceiling or DEFAULT_MAX_CHUNK_SECS)
        self.language = language
        self.context = context
        self.line_splitter = line_splitter
        self.task_id = task_id
        self.gc_every = gc_every
        self.threshold = vad_threshold
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.out_srt.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.work_dir / f"{task_id}.segments.jsonl"
        self.ckpt = Checkpoint(self.work_dir / "checkpoints.db", task_id)

    def _emit(self, pct: int, status: str):
        try:
            self.progress_cb(max(0, min(100, int(pct))), status)
        except Exception:
            pass

    def _init_outputs(self, next_index: int):
        if next_index <= 1:
            self.out_srt.write_text("", encoding="utf-8")
            self.jsonl.write_text("", encoding="utf-8")

    def _append(self, index: int, start: float, end: float, text: str) -> int:
        """每完成一段立刻落盘：SRT 序号连续，JSONL 便于重排与校对。"""
        with open(self.out_srt, "a", encoding="utf-8") as f:
            f.write(f"{index}\n{srt_ts(start)} --> {srt_ts(end)}\n{text}\n\n")
        with open(self.jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps({"index": index, "start": round(start, 3),
                                "end": round(end, 3), "text": text},
                               ensure_ascii=False) + "\n")
        return index + 1

    def run(self, audio_path, pcm_source=None, total_sec: float | None = None,
            resume: bool = True) -> dict:
        next_frame, next_index, status = self.ckpt.load() if resume else (0, 1, "new")
        if status == "done":
            return {"status": "done", "srt": str(self.out_srt),
                    "segments": next_index - 1, "resumed": True}
        self._init_outputs(next_index)

        start_sec = next_frame * VAD_CHUNK / SR
        self._emit(0, f"从第 {start_sec / 60:.1f} 分钟续跑" if start_sec else "开始流式分段")
        stream = (pcm_source if pcm_source is not None else
                  ffmpeg_pcm_stream(Path(audio_path), self.ffmpeg, start_sec))
        if total_sec is None and audio_path is not None:
            total_sec = probe_duration(audio_path, self.ffmpeg)

        # ── 滚动状态 ──────────────────────────────────────────────
        carry = np.empty(0, dtype=np.float32)   # 尚未吐出的样本
        base = next_frame * VAD_CHUNK           # carry[0] 对应的全局样本号
        read_ptr = 0                            # VAD 已消费到 carry 的位置
        frame = next_frame                      # 全局帧号（时间轴基准）
        h = np.zeros((2, 1, 64), dtype=np.float32)
        c = np.zeros((2, 1, 64), dtype=np.float32)
        sr_arr = np.array(SR, dtype=np.int64)
        in_sp, s0 = False, 0
        raw: list[tuple[int, int]] = []
        seg_index = next_index
        group_count = 0
        processed_frames = 0
        max_samples = self.max_secs * SR
        tail = int(_TAIL_SEC * SR)
        keep_limit = int((self.max_secs + _TAIL_SEC + 2.0) * SR)

        def emit_group(gs_f: int, ge_f: int) -> int:
            """把一组语音切成 ≤max_secs 子段，逐段 ASR + 即刻落盘。

            返回该组结束的全局样本号，供调用方裁剪 carry。
            """
            nonlocal seg_index, group_count
            g_a = gs_f * VAD_CHUNK
            g_b = ge_f * VAD_CHUNK + tail
            a = max(0, g_a - base)
            b = min(len(carry), g_b - base)
            if b - a < SR // 2:                 # < 0.5s 丢弃
                return g_b
            samples = carry[a:b]
            cur = 0
            while cur < len(samples):
                if self.cancel.is_set():
                    raise Cancelled()
                nxt = min(len(samples), cur + max_samples)
                chunk = np.ascontiguousarray(samples[cur:nxt], dtype=np.float32)
                text = (self.engine.transcribe(
                    chunk, language=self.language, context=self.context) or "").strip()
                t0 = (base + a + cur) / SR
                t1 = (base + a + nxt) / SR
                lines = (self.line_splitter(text, t0, t1) if self.line_splitter
                         else simple_lines(text, t0, t1))
                for s, e, line in lines:
                    seg_index = self._append(seg_index, s, e, line)
                group_count += 1
                # 主动断开引用：大块张量在段边界归还，避免锯齿叠加
                del chunk, text, lines
                if self.gc_every and group_count % self.gc_every == 0:
                    gc.collect()
                cur = nxt
            return g_b

        try:
            for pcm in stream:
                if self.cancel.is_set():
                    raise Cancelled()
                carry = np.concatenate((carry, np.asarray(pcm, dtype=np.float32)))
                n_frames = (len(carry) - read_ptr) // VAD_CHUNK

                for _ in range(n_frames):
                    fr = carry[read_ptr:read_ptr + VAD_CHUNK]
                    read_ptr += VAD_CHUNK
                    out, h, c = self.vad_sess.run(
                        None, {"input": fr[np.newaxis, :].astype(np.float32),
                               "h": h, "c": c, "sr": sr_arr})
                    p = float(out[0, 0])
                    if p >= self.threshold and not in_sp:
                        s0, in_sp = frame, True
                    elif p < self.threshold and in_sp:
                        if frame - s0 >= _MIN_CH:
                            raw.append((max(0, s0 - _PAD_CH), frame + _PAD_CH))
                        in_sp = False
                    frame += 1
                    processed_frames += 1

                # 合并相邻语音段；最后一组可能继续合并，留到下一轮
                merged: list[list[int]] = []
                for s, e in raw:
                    if merged and s - merged[-1][1] <= _MERGE_CH:
                        merged[-1][1] = e
                    else:
                        merged.append([s, e])
                ready = merged[:-1] if len(merged) > 1 else []
                raw = merged[-1:] if merged else []

                cut_to = None
                for gs, ge in ready:
                    cut_to = emit_group(gs, ge)
                if cut_to is not None:
                    cut = min(max(0, cut_to - base), len(carry))
                    carry = carry[cut:]
                    base += cut
                    read_ptr = max(0, read_ptr - cut)

                # 长静音时不让 carry 无限增长（只在无待吐组时裁）
                if not ready and not raw and len(carry) > keep_limit:
                    drop = len(carry) - keep_limit
                    carry = carry[drop:]
                    base += drop
                    read_ptr = max(0, read_ptr - drop)

                if processed_frames % 512 == 0:
                    done_sec = frame * VAD_CHUNK / SR
                    pct = int(done_sec / total_sec * 100) if total_sec else 0
                    self._emit(pct, f"已处理 {done_sec / 60:.1f} 分钟 · {group_count} 段")
                    self.ckpt.save(frame, seg_index, "running")

            # 文件结束：收尾当前语音段
            if in_sp and frame - s0 >= _MIN_CH:
                raw.append((max(0, s0 - _PAD_CH), frame))
            merged = []
            for s, e in raw:
                if merged and s - merged[-1][1] <= _MERGE_CH:
                    merged[-1][1] = e
                else:
                    merged.append([s, e])
            for gs, ge in merged:
                emit_group(gs, ge)

        except Cancelled:
            self.ckpt.save(frame, seg_index, "paused")
            self._emit(0, "已取消，进度已保存，可再次启动续跑")
            return {"status": "paused", "srt": str(self.out_srt),
                    "segments": seg_index - 1, "next_frame": frame,
                    "groups": group_count, "frames": frame}

        self.ckpt.save(frame, seg_index, "done")
        self._emit(100, f"完成 · 共 {seg_index - 1} 条字幕")
        return {"status": "done", "srt": str(self.out_srt),
                "segments": seg_index - 1, "next_frame": frame,
                "groups": group_count, "frames": frame}
