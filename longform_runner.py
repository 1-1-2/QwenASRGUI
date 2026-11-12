"""Streaming runner for long audio, replacing "load everything, write at the end".

Target workload: 1-5 hour lectures / meeting recordings.

What it changes
---------------
1. Never loads the whole file: ffmpeg streams 16 kHz / mono / s16le PCM and a
   rolling buffer keeps only the speech groups not yet emitted, so resident
   memory is independent of file length.
2. Never buffers all subtitles until the end: each completed group is appended
   to SRT and JSONL immediately. A crash, power loss or cancel keeps everything
   produced so far.
3. A SQLite checkpoint stores the global frame index, SRT index, a task
   fingerprint and the VAD recurrent state, enabling exact resume.
4. progress_cb(pct:int, status:str) matches the existing WebView SSE contract;
   no frontend change is required. Optional detail_cb / segment_cb add
   structured progress (processed seconds, group count, ETA) and throttled
   live-subtitle batches for the UI without changing that legacy contract.

Deliberate non-goals
--------------------
* No concurrency. ASREngine.transcribe() holds self._lock, so extra threads only
  queue; extra processes each load their own IR and multiply memory peaks.
* No changes to OpenVINO thread policy (PERFORMANCE_HINT / INFERENCE_NUM_THREADS
  are controlled from the Model page and are a separate tuning axis).
* The memory sawtooth is flattened by releasing at group boundaries and keeping
  the peak independent of file length, not by retuning OpenVINO.

Dependencies: stdlib + numpy only. The VAD session and ASR engine are injected
by the caller so the module can be tested without model weights.
"""

from __future__ import annotations

import base64
import collections
import gc
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
VAD_CHUNK = 512                  # Silero VAD 固定输入：32ms / 帧
READ_BYTES = 64 * 1024          # 每次读 2.048s PCM，避免几十万次小 read
DEFAULT_MAX_CHUNK_SECS = 30     # 0.6B 上限；1.7B 请传 10

# A five-minute sample is enough to judge both segmentation quality and
# recognition accuracy, while being roughly twice as fast as a ten-minute one.
TRIAL_MINUTES = 5
PREVIEW_MINUTES = 5
WAVE_BINS = 200                  # waveform bars rendered by the frontend

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

# h and c are each (2, 1, 64) float32 -> 256 floats in total.
_VAD_STATE_FLOATS = 2 * 2 * 1 * 64


def encode_vad_state(h: np.ndarray, c: np.ndarray) -> str:
    """Pack the VAD recurrent state into a base64 string for the checkpoint."""
    flat = np.concatenate((np.asarray(h, dtype=np.float32).ravel(),
                           np.asarray(c, dtype=np.float32).ravel()))
    return base64.b64encode(flat.tobytes()).decode("ascii")


def decode_vad_state(blob: str):
    """Restore (h, c), or return None when the payload is missing/malformed."""
    if not blob:
        return None
    try:
        flat = np.frombuffer(base64.b64decode(blob), dtype=np.float32)
        if flat.size != _VAD_STATE_FLOATS:
            return None
        half = _VAD_STATE_FLOATS // 2
        return (flat[:half].reshape(2, 1, 64).copy(),
                flat[half:].reshape(2, 1, 64).copy())
    except Exception:
        return None


class Checkpoint:
    """Progress record: frame index, SRT index, task fingerprint, VAD state.

    SQLite is preferred; on permission / sync-folder / lock failures it falls
    back to a JSON mirror (common on Windows with antivirus or OneDrive). Both
    paths are always written so a downgrade never loses progress.

    The fingerprint prevents resuming a half-old/half-new transcript after the
    model, chunk length, VAD threshold, language or script setting changed.
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
                    updated_at REAL NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    vad_state TEXT NOT NULL DEFAULT '')""")
                # Migrate databases created by earlier builds (safe to re-run).
                cols = {row[1] for row in c.execute("PRAGMA table_info(progress)")}
                if "fingerprint" not in cols:
                    c.execute("ALTER TABLE progress ADD COLUMN "
                              "fingerprint TEXT NOT NULL DEFAULT ''")
                if "vad_state" not in cols:
                    c.execute("ALTER TABLE progress ADD COLUMN "
                              "vad_state TEXT NOT NULL DEFAULT ''")
                c.commit()
            finally:
                c.close()
        except sqlite3.Error:
            self._use_sqlite = False

    def _conn(self):
        # Use a fresh connection per operation and close it explicitly:
        # `with conn` only manages the transaction, not the handle.
        return sqlite3.connect(str(self.db_path), timeout=30)

    def _json_all(self) -> dict:
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _json_load(self):
        d = self._json_all().get(self.task_id)
        return (int(d["next_frame"]), int(d["next_index"]), d["status"]) if d else (0, 1, "new")

    def _json_save(self, frame, index, status, fingerprint, vad_state):
        d = self._json_all()
        d[self.task_id] = {"next_frame": int(frame), "next_index": int(index),
                           "status": status, "fingerprint": fingerprint or "",
                           "vad_state": vad_state or ""}
        tmp = self.json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.json_path)          # atomic replace: never half-written

    def load(self) -> tuple[int, int, str]:
        """Kept for compatibility: (next_frame, next_index, status)."""
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

    def load_meta(self) -> tuple[str, str]:
        """Return (fingerprint, vad_state_blob) for this task."""
        with self._lock:
            if self._use_sqlite:
                try:
                    c = self._conn()
                    try:
                        row = c.execute(
                            "SELECT fingerprint, vad_state FROM progress WHERE task_id=?",
                            (self.task_id,)).fetchone()
                        if row:
                            return (row[0] or "", row[1] or "")
                    finally:
                        c.close()
                except sqlite3.Error:
                    self._use_sqlite = False
            d = self._json_all().get(self.task_id) or {}
            return (d.get("fingerprint", "") or "", d.get("vad_state", "") or "")

    def save(self, next_frame: int, next_index: int, status: str = "running",
             fingerprint: str | None = None, vad_state: str | None = None):
        """Persist progress. None fingerprint/vad_state means "keep the old one"."""
        with self._lock:
            prev = self._json_all().get(self.task_id) or {}
            fp = prev.get("fingerprint", "") if fingerprint is None else (fingerprint or "")
            vs = prev.get("vad_state", "") if vad_state is None else (vad_state or "")
            if self._use_sqlite:
                try:
                    c = self._conn()
                    try:
                        c.execute("""INSERT INTO progress
                                     (task_id,next_frame,next_index,status,updated_at,
                                      fingerprint,vad_state)
                                     VALUES(?,?,?,?,?,?,?)
                                     ON CONFLICT(task_id) DO UPDATE SET
                                       next_frame=excluded.next_frame,
                                       next_index=excluded.next_index,
                                       status=excluded.status,
                                       updated_at=excluded.updated_at,
                                       fingerprint=excluded.fingerprint,
                                       vad_state=excluded.vad_state""",
                                  (self.task_id, int(next_frame), int(next_index),
                                   status, time.time(), fp, vs))
                        c.commit()
                    finally:
                        c.close()
                except sqlite3.Error:
                    self._use_sqlite = False
            self._json_save(next_frame, next_index, status, fp, vs)


# ───────────────────────── PCM 源 ─────────────────────────

def ffmpeg_pcm_stream(path: Path, ffmpeg: str = "ffmpeg", start_sec: float = 0.0):
    """yield float32 PCM 块；ffmpeg 常驻子进程，绝不把整段读进内存。

    ``-ss`` 放在 ``-i`` 之后 → 音频精确 seek，断点续跑时时间轴不漂移。

    关于 stderr：**必须收集，不能丢进 DEVNULL**。曾经这里写的是
    ``stderr=subprocess.DEVNULL``，而 ffmpeg 的失败原因（文件被占用、路径无
    权限、扩展名与实际内容不符、解复用器不支持…）**只出现在 stderr 里**。
    丢掉之后，Windows 上的失败只剩下自己 OSError 的一句「[WinError 5] 拒绝
    访问」——没有主词、没有文件、没有原因，使用者拿到的就是这么一句。
    现在改成管道收集尾部若干行，失败时拼进异常消息。
    """
    cmd = [ffmpeg, "-v", "error", "-i", str(path)]
    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.6f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]

    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, **kw)
    except OSError as e:
        # Popen 失败（PermissionError / FileNotFoundError / 其他 OS 错误）。
        # 关键：**这些错都发生在「启动子进程」这一步，不是「读文件」**。
        # 仅凭错误文本无法区分成因，这里把诊断所需的全部上下文打出来。
        ff_path = cmd[0]
        try:
            ff_abs = str(Path(ff_path).resolve())
        except Exception:
            ff_abs = str(ff_path)
        try:
            ff_exists = Path(ff_path).is_file()
        except Exception:
            ff_exists = "unknown"
        try:
            ff_mode = oct(Path(ff_path).stat().st_mode) if ff_exists is True else "n/a"
        except Exception:
            ff_mode = "n/a"
        raise RuntimeError(
            f"无法启动 ffmpeg 子进程：{type(e).__name__}: {e}\n"
            f"ffmpeg 路径：{ff_abs}\n"
            f"ffmpeg 存在：{ff_exists}\n"
            f"ffmpeg 权限：{ff_mode}\n"
            f"当前工作目录：{os.getcwd()}\n"
            f"命令行：{' '.join(str(c) for c in cmd)}\n"
            "Popen 失败通常来自：\n"
            "  - ffmpeg 不存在（路径错 / 没下载 / PATH 没配）\n"
            "  - ffmpeg 所在目录没有「执行」权限\n"
            "  - Windows 受控文件夹访问（Controlled Folder Access）拦截\n"
            "  - AppLocker / WDAC / Smart App Control 策略阻止\n"
            "  - 杀毒软件 / EDR 阻止 CreateProcess\n"
            "请把以上信息一并反馈给开发者，便于定位。"
        ) from e
    # stderr 必须持续抽干：管道写满（Windows 约 4 KB）后 ffmpeg 会阻塞，
    # 表现为「转换卡住不动」，比报错更难查。读线程只留尾部若干行。
    tail: collections.deque[str] = collections.deque(maxlen=8)

    def _drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
        except Exception:
            pass

    _stderr_thread = threading.Thread(target=_drain, name="ffmpeg-stderr", daemon=True)
    _stderr_thread.start()

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
                _stderr_thread.join(timeout=2)
                raise RuntimeError(
                    f"ffmpeg 退出码 {rc}：{_ffmpeg_reason(tail, path)}")
        else:
            # 消费者提前退出（取消 / 异常）：静默回收，不覆盖真实异常
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            proc.stderr.close()
        except Exception:
            pass


def _ffmpeg_reason(tail, path: Path) -> str:
    """把 ffmpeg stderr 尾部整理成一句能照着动手的话。

    Windows 的错误文本形如
        [in#0 @ 0000021c] Error opening input: Permission denied
        Error opening input file 002.m4a.
    原样贴出去已经比「拒绝访问」强得多；这里再把最常见的几种翻译成中文，
    让使用者不必自己猜「Permission denied」在哪一层。
    """
    lines = [x for x in tail if x.strip()]
    raw = " | ".join(lines[-4:]) if lines else "(ffmpeg 未输出任何错误文本)"
    low = raw.lower()
    if "permission denied" in low:
        hint = "（檔案被其他程式占用，或目前帳號對它／它所在的資料夾沒有讀取權限）"
    elif "no such file" in low or "cannot find" in low:
        hint = "（路徑不存在，或檔名在傳遞過程中被截斷）"
    elif "invalid data" in low or "moov atom not found" in low:
        hint = "（檔案內容不是它副檔名宣稱的格式，或檔案未下載完整）"
    elif "is a directory" in low:
        hint = "（給的是資料夾，不是音檔）"
    else:
        hint = ""
    return f"{raw}{hint}"


def wav_pcm_stream(path: Path, start_sec: float = 0.0):
    """WAV 专用流式读取（标准库 wave，逐块 yield）。

    仅用于 16kHz / mono / s16le 的 WAV。ffmpeg 不可用时作为兜底，
    保证纯 Python 环境也能跑长音频测试；其他格式仍需要 ffmpeg。
    """
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2):
            raise ValueError(
                "缺少 ffmpeg，且 WAV 不是 16kHz/mono/s16le；"
                "请安装 ffmpeg 或先转换音频格式")
        w.setpos(int(start_sec * SR))
        frames_per_read = READ_BYTES // 2
        while True:
            raw = w.readframes(frames_per_read)
            if not raw:
                break
            yield (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)


def ffmpeg_available(ffmpeg: str) -> bool:
    """判断 ffmpeg 可执行：支持 PATH 名称与绝对路径两种写法。"""
    return bool(shutil.which(ffmpeg)) or os.path.isfile(ffmpeg)


def pcm_stream(path: Path, ffmpeg: str = "ffmpeg", start_sec: float = 0.0):
    """PCM 源分发：优先 ffmpeg；缺失且输入为 WAV 时回退到标准库流式读取。"""
    if not ffmpeg_available(ffmpeg) and str(path).lower().endswith(".wav"):
        return wav_pcm_stream(path, start_sec)
    return ffmpeg_pcm_stream(path, ffmpeg, start_sec)


class ArrayPcmStream:
    """Offline / test source: feed a numpy array in chunks, bypassing ffmpeg."""
    def __init__(self, audio: np.ndarray, start_sec: float = 0.0):
        self.audio = np.asarray(audio[int(start_sec * SR):], dtype=np.float32)
        self.pos = 0

    def __iter__(self):
        step = READ_BYTES // 2
        while self.pos < len(self.audio):
            yield self.audio[self.pos:self.pos + step]
            self.pos += step


def limited_pcm_stream(source, max_samples: int | None):
    """Stop a PCM generator after `max_samples` samples.

    Used by the five-minute trial run and the five-minute VAD preview. Cutting
    the generator (instead of seeking inside ffmpeg) keeps the WAV fallback and
    ArrayPcmStream on exactly the same code path.
    """
    if max_samples is None or max_samples <= 0:
        yield from source
        return
    remaining = int(max_samples)
    for pcm in source:
        arr = np.asarray(pcm, dtype=np.float32)
        if remaining <= 0:
            break
        if len(arr) > remaining:
            yield arr[:remaining]
            return
        yield arr
        remaining -= len(arr)


class PeakBins:
    """Incrementally build a 200-bar waveform while PCM is being streamed.

    The frontend used to call decodeAudioData() on the entire file just to draw
    200 bars, decoding a 5-hour lecture into GBs of PCM in the WebView2
    renderer and causing an "Out of memory" crash after the run completed.
    Storing one peak per second (18 000 floats for 5 h, ~72 KB) and resampling
    at the end removes that whole-file allocation.
    """

    def __init__(self, bins: int = WAVE_BINS):
        self.bins = int(bins)
        self._per_second: list[float] = []
        self._sample_in_second = 0
        self._cur = 0.0
        self.total_samples = 0

    def feed(self, pcm: np.ndarray) -> None:
        arr = np.asarray(pcm, dtype=np.float32)
        self.total_samples += len(arr)
        if arr.size == 0:
            return
        # Cheap block max; splitting per second keeps the loop vectorised.
        mx = float(np.abs(arr).max())
        self._cur = max(self._cur, mx)
        self._sample_in_second += len(arr)
        while self._sample_in_second >= SR:
            self._per_second.append(self._cur)
            self._sample_in_second -= SR
            self._cur = 0.0

    def to_bars(self) -> list[float]:
        """Return `bins` normalised bars in [0, 1]."""
        if self._cur > 0 and self._sample_in_second > 0:
            self._per_second.append(self._cur)
            self._cur = 0.0
            self._sample_in_second = 0
        if not self._per_second:
            return []
        n = len(self._per_second)
        out = []
        for i in range(self.bins):
            a, b = i * n // self.bins, max(i * n // self.bins + 1,
                                           (i + 1) * n // self.bins)
            out.append(max(self._per_second[a:b]))
        norm = max(max(out), 1e-4)
        return [round(x / norm, 4) for x in out]

    @property
    def duration(self) -> float:
        return self.total_samples / SR


def probe_duration(path, ffmpeg: str = "ffmpeg") -> float | None:
    """Return the total duration in seconds, or None on failure.

    A None result only degrades progress reporting; it never blocks a run.
    """
    if str(path).lower().endswith(".wav"):
        try:
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / w.getframerate()
        except Exception:
            pass
    try:
        import soundfile as sf
        return float(sf.info(str(path)).duration)
    except Exception:
        pass
    try:
        # Hide the console window on Windows. Without CREATE_NO_WINDOW /
        # STARTUPINFO this probe flashes a black cmd box on every task start;
        # ffmpeg_utils.py already uses the same guard.
        kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "stdin": subprocess.DEVNULL, "timeout": 30}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            si.wShowWindow = 0  # SW_HIDE
            kw["startupinfo"] = si
        # The old `-v error -f null -` both suppressed the info-level
        # "Duration:" line and decoded the whole file (hours for a lecture).
        # Giving no output file makes ffmpeg print the input header and exit
        # immediately, so probing stays fast regardless of file length.
        cmd = [ffmpeg, "-hide_banner", "-i", str(path)]
        r = subprocess.run(cmd, **kw)
        out = (r.stdout or b"") + (r.stderr or b"")
        m = re.findall(rb"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if m:
            h, mi, s = m[0]
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


# ───────────────────────── VAD preview ─────────────────────────

def vad_preview(audio_path=None, pcm_source=None, vad_sess=None,
                ffmpeg: str = "ffmpeg", threshold: float = 0.5,
                max_chunk_secs: int = DEFAULT_MAX_CHUNK_SECS,
                minutes: int = PREVIEW_MINUTES,
                progress_cb=None, cancel: threading.Event | None = None) -> dict:
    """Dry-run the segmentation on the first `minutes` of audio, without ASR.

    Reuses the exact segmentation constants and VAD feed logic of
    LongformRunner so the preview predicts real behaviour. Returns a compact
    statistics dict the UI can turn into red/yellow/green indicators.
    """
    if vad_sess is None:
        raise ValueError("vad_sess is required for VAD preview")
    emit = progress_cb or (lambda pct, status: None)
    cancel = cancel or threading.Event()
    limit_samples = int(minutes * 60 * SR)
    stream = (pcm_source if pcm_source is not None else
              pcm_stream(Path(audio_path), ffmpeg, 0.0))
    stream = limited_pcm_stream(stream, limit_samples)

    h = np.zeros((2, 1, 64), dtype=np.float32)
    c = np.zeros((2, 1, 64), dtype=np.float32)
    sr_arr = np.array(SR, dtype=np.int64)
    carry = np.empty(0, dtype=np.float32)
    frame = 0
    in_sp, s0 = False, 0
    speech_frames = 0
    raw: list[list[int]] = []          # frame intervals, same as the runner
    last_pct = -1

    for pcm in stream:
        if cancel.is_set():
            return {"cancelled": True}
        carry = np.concatenate((carry, np.asarray(pcm, dtype=np.float32)))
        n_frames = len(carry) // VAD_CHUNK
        for i in range(n_frames):
            fr = carry[i * VAD_CHUNK:(i + 1) * VAD_CHUNK]
            out, h, c = vad_sess.run(
                None, {"input": fr[np.newaxis, :].astype(np.float32),
                       "h": h, "c": c, "sr": sr_arr})
            active = float(out[0, 0]) >= threshold
            if active:
                speech_frames += 1
            if active and not in_sp:
                s0, in_sp = frame, True
            elif not active and in_sp:
                if frame - s0 >= _MIN_CH:
                    raw.append([max(0, s0 - _PAD_CH), frame + _PAD_CH])
                in_sp = False
            frame += 1
        # No audio has to be retained here: drop every consumed frame and keep
        # only the sub-frame remainder, so memory stays O(1).
        carry = carry[n_frames * VAD_CHUNK:]
        pct = int(frame * VAD_CHUNK / SR / (minutes * 60) * 100)
        if pct != last_pct and pct % 10 == 0:
            emit(min(100, pct), f"切分預檢 {pct}%")
            last_pct = pct
        if frame * VAD_CHUNK >= limit_samples:
            break

    if in_sp and frame - s0 >= _MIN_CH:
        raw.append([max(0, s0 - _PAD_CH), frame])

    merged: list[list[int]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] <= _MERGE_CH:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    lengths = sorted((e - s) * VAD_CHUNK / SR for s, e in merged)

    total_sec = frame * VAD_CHUNK / SR

    def pct_at(q: float) -> float:
        return round(lengths[min(len(lengths) - 1, int(len(lengths) * q))], 2) \
            if lengths else 0.0

    over = sum(1 for x in lengths if x > max_chunk_secs)
    short = sum(1 for x in lengths if x < 0.5)
    asr_calls = sum(max(1, -(-int(x) // max_chunk_secs)) for x in lengths)
    return {
        "cancelled": False,
        "previewSeconds": round(total_sec, 2),
        "previewMinutes": minutes,
        "totalFrames": frame,
        "groupCount": len(lengths),
        "lengths": {
            "p50": pct_at(0.50), "p90": pct_at(0.90),
            "p99": pct_at(0.99), "max": round(lengths[-1], 2) if lengths else 0.0,
        },
        "overLimitRatio": round(over / len(lengths), 4) if lengths else 0.0,
        "shortRatio": round(short / len(lengths), 4) if lengths else 0.0,
        "silenceRatio": round(1 - speech_frames / frame, 4) if frame else 1.0,
        "estimatedAsrCalls": asr_calls,
        "estimatedAsrCallsPerHour": (round(asr_calls * 3600 / total_sec, 1)
                                     if total_sec > 0 else 0),
        "maxChunkSecs": max_chunk_secs,
        "threshold": threshold,
        "verdict": ("yellow" if (lengths and (
            over / len(lengths) > 0.05 or
            pct_at(0.90) > 0.8 * max_chunk_secs or
            short / len(lengths) > 0.10)) else "green"),
    }


# ───────────────────────── Main runner ─────────────────────────

class LongformRunner:
    """Streaming segmentation + incremental writes + resume + cancel.

    Single worker by design; see the module docstring for the rationale.
    """

    def __init__(self, engine, vad_sess, out_srt, work_dir,
                 ffmpeg: str = "ffmpeg",
                 progress_cb=None, cancel: threading.Event | None = None,
                 max_chunk_secs: int | None = None,
                 language: str | None = None,
                 context: str | None = None,
                 line_splitter=None,
                 task_id: str = "default",
                 fingerprint: str = "",
                 gc_every: int = 0,
                 vad_threshold: float = 0.5,
                 detail_cb=None, segment_cb=None, enhancer=None,
                 refine_punct: bool = True):
        if vad_sess is None:
            raise ValueError("缺少 VAD session：请传入 engine.vad_sess")
        self.fingerprint = fingerprint or ""
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
        # Optional SSE enrichments. They are additive: legacy callers that only
        # pass progress_cb keep working unchanged.
        self.detail_cb = detail_cb
        self.segment_cb = segment_cb
        # Optional best-effort enrichment (word timing / speakers). The runner
        # only forwards the PCM chunk; failures are contained in the enhancer.
        self.enhancer = enhancer
        # C1: refine cross-segment punctuation in place once, on `done` only.
        # Never on `paused` - see _finalize_punctuation() for the A2 rationale.
        self.refine_punct = refine_punct
        self._seg_batch: list[dict] = []
        self._last_seg_flush = 0.0
        self._start_wall = time.monotonic()
        self._last_done_sec = 0.0
        self._last_group_count = 0

    def _emit(self, pct: int, status: str, detail: dict | None = None):
        pct = max(0, min(100, int(pct)))
        try:
            self.progress_cb(pct, status)
        except Exception:
            pass
        if self.detail_cb:
            payload = {"pct": pct, "status": status, "taskId": self.task_id}
            if detail:
                payload.update(detail)
                # Wave 13: expose measured wall-clock elapsed and realtime
                # factor so the batch page can show "本檔已耗時 / 速度 N×"
                # from real data instead of a client-side estimate.
                done = float(payload.get("doneSec") or 0)
                elapsed = max(0.0, time.monotonic() - self._start_wall)
                payload["elapsedSec"] = round(elapsed, 1)
                if elapsed > 0 and done > 0:
                    payload["speedX"] = round(done / elapsed, 2)
            try:
                self.detail_cb(payload)
            except Exception:
                pass

    def _queue_segment(self, seg: dict):
        """Batch live subtitles so SSE never floods the renderer.

        A 5-hour lecture can produce tens of thousands of lines; emitting one
        event per line would make the WebView DOM the new OOM source. Flush at
        most twice per second or every 8 lines, whichever comes first.
        """
        if not self.segment_cb:
            return
        self._seg_batch.append(seg)
        now = time.monotonic()
        if len(self._seg_batch) >= 8 or now - self._last_seg_flush >= 0.5:
            self._flush_segments()

    def _flush_segments(self, force: bool = False):
        if not self.segment_cb or not self._seg_batch:
            if force:
                self._last_seg_flush = time.monotonic()
            return
        batch, self._seg_batch = self._seg_batch, []
        self._last_seg_flush = time.monotonic()
        try:
            self.segment_cb({"taskId": self.task_id, "segments": batch,
                             "count": len(batch)})
        except Exception:
            pass

    def _eta_sec(self, done_sec: float, total_sec: float) -> float | None:
        """Extrapolate remaining wall time from processed audio seconds."""
        if not total_sec or done_sec <= 0:
            return None
        elapsed = max(1e-3, time.monotonic() - self._start_wall)
        rate = done_sec / elapsed
        if rate <= 0:
            return None
        return max(0.0, (total_sec - done_sec) / rate)

    def _init_outputs(self, next_index: int):
        if next_index <= 1:
            self.out_srt.write_text("", encoding="utf-8")
            self.jsonl.write_text("", encoding="utf-8")

    def _append(self, index: int, start: float, end: float, text: str) -> int:
        """Append one subtitle to SRT/JSONL immediately, then queue it for SSE."""
        with open(self.out_srt, "a", encoding="utf-8") as f:
            f.write(f"{index}\n{srt_ts(start)} --> {srt_ts(end)}\n{text}\n\n")
        with open(self.jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps({"index": index, "start": round(start, 3),
                                "end": round(end, 3), "text": text},
                               ensure_ascii=False) + "\n")
        self._queue_segment({"index": index, "start": round(start, 3),
                             "end": round(end, 3), "text": text})
        return index + 1

    def _finalize_punctuation(self) -> None:
        """C1: one-shot in-place punctuation refinement on the `done` path.

        Why this does not break A2 (resume == full run, byte for byte):

        * The raw SRT is a pure append-only stream of groups. Both a full run
          and a cancelled-then-resumed run end with the identical raw bytes.
        * This rewrite is a pure function of that complete raw SRT and runs
          exactly once, at `done`. The `paused` path deliberately skips it, so
          a resumed run appends to the *raw* file and only refines at the end.
        * `done` short-circuits on later starts, so it is never applied twice
          (the refinement is not idempotent).
        * Checkpoints store frame/SRT indices, not byte offsets; JSONL keeps the
          raw lines and stays the source of truth for enrichments.

        Any failure here is swallowed: punctuation is an enhancement, and a
        usable SRT must always survive even if this step fails.
        """
        if not self.refine_punct or not self.out_srt.is_file():
            return
        try:
            from subtitle_extras import refine_srt_text
            raw = self.out_srt.read_text(encoding="utf-8")
            refined = refine_srt_text(raw, cjk_only=True)
            if refined == raw:
                return
            tmp = self.out_srt.with_name(self.out_srt.name + ".tmp")
            tmp.write_text(refined, encoding="utf-8")
            tmp.replace(self.out_srt)
        except Exception:
            pass

    def run(self, audio_path, pcm_source=None, total_sec: float | None = None,
            resume: bool = True, limit_sec: float | None = None) -> dict:
        """Run the streaming pipeline.

        `limit_sec` truncates the PCM source after that many seconds (used for
        the five-minute trial run). It is an absolute offset from the start of
        the file, so a resumed run subtracts the already-consumed `start_sec`.
        """
        # A trial run must never share the checkpoint / output of a full run.
        if limit_sec:
            resume = False
        self._start_wall = time.monotonic()
        next_frame, next_index, status = self.ckpt.load() if resume else (0, 1, "new")
        saved_fp, saved_vad = self.ckpt.load_meta() if resume else ("", "")

        # A mismatched fingerprint means the model / chunk length / language /
        # script / VAD threshold changed. Resuming would silently produce a
        # half-old half-new transcript, so restart from the beginning instead.
        if resume and saved_fp and saved_fp != self.fingerprint:
            self._emit(0, "任务参数已变更（模型/段长/语言/简繁/阈值等），从头开始以免字幕混排")
            next_frame, next_index, status, saved_vad = 0, 1, "new", ""
            resume = False
            self.ckpt.save(0, 1, "running", fingerprint=self.fingerprint,
                           vad_state="")

        # Wave 14／需求 3：記住本次從第幾段起跑，回報給佇列以保留「已處理段數」。
        resume_from = int(next_index) if resume else 1
        if status == "done" and not limit_sec:
            return {"status": "done", "srt": str(self.out_srt),
                    "segments": next_index - 1, "resumed": True,
                    "resumeFrom": resume_from,
                    "peaks": [], "duration": 0.0, "truncated": False}
        self._init_outputs(next_index)

        start_sec = next_frame * VAD_CHUNK / SR
        # Wave 14／需求 3：续跑时把「已完成的段数」一并送给前端，否则界面会
        # 从 0 段开始显示、看起来像从头识别。
        self._emit(0, f"从第 {start_sec / 60:.1f} 分钟续跑" if start_sec else "开始流式分段",
                   {"doneSec": round(start_sec, 1),
                    "totalSec": round(total_sec or 0, 1),
                    "segments": max(0, next_index - 1),
                    "phase": "resume" if start_sec else "start"})
        stream = (pcm_source if pcm_source is not None else
                  pcm_stream(Path(audio_path), self.ffmpeg, start_sec))
        if total_sec is None and audio_path is not None:
            self._emit(0, "正在探测音频时长…")
            total_sec = probe_duration(audio_path, self.ffmpeg)
            if total_sec:
                self._emit(0, f"音频时长 {total_sec / 60:.1f} 分钟，采用流式分段")
        truncated = bool(limit_sec and (total_sec or 0) > limit_sec)
        if limit_sec:
            remaining = max(0, int(limit_sec * SR) - int(start_sec * SR))
            stream = limited_pcm_stream(stream, remaining)
            total_sec = min(total_sec or limit_sec, limit_sec)
            self._emit(0, f"试跑模式：只处理前 {limit_sec / 60:.0f} 分钟")
        self.ckpt.save(next_frame, next_index, "running",
                       fingerprint=self.fingerprint, vad_state=saved_vad)
        peaks = PeakBins()

        # ── 滚动状态 ──────────────────────────────────────────────
        carry = np.empty(0, dtype=np.float32)   # 尚未吐出的样本
        base = next_frame * VAD_CHUNK           # carry[0] 对应的全局样本号
        read_ptr = 0                            # VAD 已消费到 carry 的位置
        frame = next_frame                      # 全局帧号（时间轴基准）
        h = np.zeros((2, 1, 64), dtype=np.float32)
        c = np.zeros((2, 1, 64), dtype=np.float32)
        # Restore the VAD recurrent state so resume does not lose the speech
        # group straddling the breakpoint (previously up to one group < 30 s).
        restored = decode_vad_state(saved_vad) if resume else None
        if restored is not None:
            h, c = restored
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
            """Split one speech group into <=max_secs chunks, transcribe and
            append each result immediately.

            Cancellation is deliberately NOT checked inside this loop: a group
            is written atomically, so a resume can rewind to its start without
            duplicating half-written subtitles. The main loop checks the cancel
            flag at group boundaries instead.

            Returns the global sample index where the group ends, used to trim
            the rolling buffer.
            """
            nonlocal seg_index, group_count
            g_a = gs_f * VAD_CHUNK
            g_b = ge_f * VAD_CHUNK + tail
            a = max(0, g_a - base)
            b = min(len(carry), g_b - base)
            if b - a < SR // 2:                 # drop fragments < 0.5 s
                return g_b
            samples = carry[a:b]
            cur = 0
            while cur < len(samples):
                nxt = min(len(samples), cur + max_samples)
                chunk = np.ascontiguousarray(samples[cur:nxt], dtype=np.float32)
                text = (self.engine.transcribe(
                    chunk, language=self.language, context=self.context) or "").strip()
                t0 = (base + a + cur) / SR
                t1 = (base + a + nxt) / SR
                lines = (self.line_splitter(text, t0, t1) if self.line_splitter
                         else simple_lines(text, t0, t1))
                # Enrich before the chunk leaves scope: aligner / embedding
                # reuse this exact buffer, so there is no second decode pass.
                if self.enhancer is not None:
                    try:
                        self.enhancer.on_group(chunk, t0, t1, text, lines,
                                               start_index=seg_index)
                    except Exception:
                        pass
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
                peaks.feed(pcm)
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

                # Trim the rolling buffer only when no speech group is open.
                # An earlier version also trimmed while `in_sp` was True, which
                # silently dropped the beginning of a long uninterrupted speech
                # segment (e.g. a lecture with no pauses for several minutes).
                if not ready and not raw and not in_sp and len(carry) > keep_limit:
                    drop = len(carry) - keep_limit
                    carry = carry[drop:]
                    base += drop
                    read_ptr = max(0, read_ptr - drop)

                if processed_frames % 512 == 0:
                    done_sec = frame * VAD_CHUNK / SR
                    pct = int(done_sec / total_sec * 100) if total_sec else 0
                    self._emit(pct, f"已处理 {done_sec / 60:.1f} 分钟 · {group_count} 段",
                               {"doneSec": round(done_sec, 1),
                                "totalSec": round(total_sec or 0, 1),
                                "groups": group_count,
                                "etaSec": self._eta_sec(done_sec, total_sec or 0),
                                "phase": "running"})
                    self.ckpt.save(frame, seg_index, "running",
                                   fingerprint=self.fingerprint,
                                   vad_state=encode_vad_state(h, c))
                elif processed_frames % 128 == 0:
                    # Progress is emitted more often than the checkpoint is
                    # written. The old 512-frame (16 s) cadence produced no
                    # running update at all for clips shorter than ~16 s.
                    done_sec = frame * VAD_CHUNK / SR
                    pct = int(done_sec / total_sec * 100) if total_sec else 0
                    self._emit(pct, f"已处理 {done_sec / 60:.1f} 分钟 · {group_count} 段",
                               {"doneSec": round(done_sec, 1),
                                "totalSec": round(total_sec or 0, 1),
                                "groups": group_count,
                                "etaSec": self._eta_sec(done_sec, total_sec or 0),
                                "phase": "running"})

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
            # Rewind to the start of any speech group detected but not yet
            # written. Everything already appended belongs to earlier groups,
            # so resuming from here reproduces an uninterrupted run byte-for-byte
            # instead of losing or duplicating the group straddling the stop.
            pending: list[int] = []
            if in_sp:
                pending.append(s0)
            if raw:
                pending.append(min(s for s, _ in raw))
            resume_frame = min(pending) if pending else frame
            if resume_frame < frame:
                self._emit(0, "正在回退到当前语音段起点，保证续跑不丢字…")
            # A rewind re-reads the group, so VAD restarts from a clean state;
            # an exact-boundary pause keeps the recurrent state instead.
            vad_blob = "" if resume_frame < frame else encode_vad_state(h, c)
            self.ckpt.save(resume_frame, seg_index, "paused",
                           fingerprint=self.fingerprint, vad_state=vad_blob)
            done_sec = frame * VAD_CHUNK / SR
            pct = int(done_sec / total_sec * 100) if total_sec else 0
            # Keep the bar where it stopped; a paused task is not a 0 % task.
            self._emit(pct, f"已取消，进度已保存（{pct}%），可再次启动续跑",
                       {"doneSec": round(done_sec, 1),
                        "totalSec": round(total_sec or 0, 1),
                        "groups": group_count, "phase": "paused"})
            self._flush_segments(force=True)
            return {"status": "paused", "srt": str(self.out_srt),
                    "segments": seg_index - 1, "next_frame": resume_frame,
                    "groups": group_count, "frames": frame,
                    "peaks": peaks.to_bars(), "duration": peaks.duration,
                    "truncated": truncated,
                    "resumeFrom": resume_from, "resumed": resume_from > 1}

        # C1: refine the finished SRT in place (single pass, done path only).
        self._finalize_punctuation()
        self.ckpt.save(frame, seg_index, "done",
                       fingerprint=self.fingerprint, vad_state="")
        self._emit(100, f"完成 · 共 {seg_index - 1} 条字幕",
                   {"doneSec": round(total_sec or 0, 1),
                    "totalSec": round(total_sec or 0, 1),
                    "groups": group_count, "etaSec": 0, "phase": "done"})
        self._flush_segments(force=True)
        return {"status": "done", "srt": str(self.out_srt),
                "segments": seg_index - 1, "next_frame": frame,
                "groups": group_count, "frames": frame,
                "peaks": peaks.to_bars(), "duration": peaks.duration,
                "truncated": truncated,
                "resumeFrom": resume_from, "resumed": resume_from > 1}
