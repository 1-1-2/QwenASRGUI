"""Wave 1 regression: duration probe, task fingerprint, VAD state resume.

Runs without model weights. Every check uses a synthetic WAV plus a mock VAD /
ASR engine, so it is deterministic and fast enough for every CI run.
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from longform_runner import (Checkpoint, LongformRunner, VAD_CHUNK, SR,
                             decode_vad_state, encode_vad_state, probe_duration)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" - " + str(extra) if extra else ""))


def make_wav(path: Path, seconds: float = 8.0) -> None:
    """A deterministic tone + silence pattern, 16 kHz / mono / s16le."""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    # 3 s speech / 1 s silence, like a lecture with natural pauses.
    env = ((t % 4.0) < 3.0).astype(np.float32)
    pcm = (0.25 * np.sin(2 * np.pi * 220 * t) * env * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


class MockVAD:
    def __init__(self):
        self.calls = 0

    def run(self, _names, feed):
        self.calls += 1
        x = feed["input"][0]
        active = bool(np.abs(x).mean() > 0.002)
        # Real onnxruntime sessions return a list of arrays; the runner reads
        # out[0, 0], so expose the probability tensor directly here.
        return (np.array([[0.9 if active else 0.05]], dtype=np.float32),
                feed["h"], feed["c"])


class MockASR:
    max_chunk_secs = 30

    def __init__(self):
        self.n = 0

    def transcribe(self, chunk, language=None, context=None):
        self.n += 1
        return "测试字幕"


def build_runner(td: Path, wav: Path, fp: str, cancel=None) -> LongformRunner:
    return LongformRunner(
        engine=MockASR(), vad_sess=MockVAD(),
        out_srt=td / "out.srt", work_dir=td / "state",
        ffmpeg="ffmpeg", task_id="same", fingerprint=fp,
        cancel=cancel, max_chunk_secs=30,
    )


def reset_outputs(td: Path, fp: str) -> None:
    (td / "out.srt").write_text("", encoding="utf-8")
    (td / "state" / "same.segments.jsonl").write_text("", encoding="utf-8")
    Checkpoint(td / "state" / "checkpoints.db", "same").save(
        0, 1, "new", fingerprint=fp, vad_state="")


def main() -> int:
    print("[1] Duration probe (A0)")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        wav = td / "src.wav"
        make_wav(wav, 8.0)
        d = probe_duration(wav, "ffmpeg")
        check("WAV header path returns exact duration",
              d is not None and abs(d - 8.0) < 0.02, str(d))
        # A non-WAV container forces the ffmpeg fallback branch.
        if shutil.which("ffmpeg"):
            flac = td / "src.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav),
                            str(flac)], check=True)
            d2 = probe_duration(flac, "ffmpeg")
            check("ffmpeg header-only probe returns duration",
                  d2 is not None and abs(d2 - 8.0) < 0.2, str(d2))
        else:
            check("ffmpeg header-only probe skipped (ffmpeg absent)", True)

    print("\n[2] VAD state codec (A2)")
    h = np.arange(128, dtype=np.float32).reshape(2, 1, 64)
    c = (np.arange(128, dtype=np.float32) + 0.5).reshape(2, 1, 64)
    blob = encode_vad_state(h, c)
    h2, c2 = decode_vad_state(blob)
    check("roundtrip is bit-exact", np.array_equal(h, h2) and np.array_equal(c, c2))
    check("malformed payload returns None", decode_vad_state("not-base64!!") is None)
    check("empty payload returns None", decode_vad_state("") is None)

    print("\n[3] Checkpoint schema and metadata (A1/A2)")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        ck = Checkpoint(td / "ck.db", "task1")
        ck.save(500, 12, "paused", fingerprint="fp-A", vad_state=blob)
        check("load() keeps the legacy 3-tuple", ck.load() == (500, 12, "paused"),
              str(ck.load()))
        check("load_meta() reads fingerprint + VAD state",
              ck.load_meta() == ("fp-A", blob))
        ck.save(600, 14, "running")  # None means "keep previous value"
        check("save(None) preserves fingerprint/state", ck.load_meta() == ("fp-A", blob))

        legacy = td / "legacy.db"
        con = sqlite3.connect(legacy)
        con.execute("CREATE TABLE progress (task_id TEXT PRIMARY KEY, "
                    "next_frame INTEGER, next_index INTEGER, status TEXT, "
                    "updated_at REAL)")
        con.execute("INSERT INTO progress VALUES ('t',10,3,'paused',0)")
        con.commit()
        con.close()
        ck2 = Checkpoint(legacy, "t")
        check("legacy database migrates without data loss",
              ck2.load() == (10, 3, "paused") and ck2.load_meta() == ("", ""),
              str(ck2.load_meta()))

    print("\n[4] Fingerprint mismatch + zero-loss resume (A1/A2)")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        wav = td / "src.wav"
        make_wav(wav, 40.0)

        full = build_runner(td, wav, "fp-1").run(wav, total_sec=40.0)
        full_text = (td / "out.srt").read_text(encoding="utf-8")
        check("full run completes", full["status"] == "done", str(full["status"]))

        reset_outputs(td, "fp-1")
        ev = threading.Event()
        r2 = build_runner(td, wav, "fp-1", ev)
        orig = r2.engine.transcribe

        def stop_after(chunk, language=None, context=None):
            if r2.engine.n >= 2:
                ev.set()
            return orig(chunk, language=language, context=context)

        r2.engine.transcribe = stop_after
        paused = r2.run(wav, total_sec=40.0)
        check("cancel reports paused", paused["status"] == "paused",
              paused["status"])
        check("cancel keeps a non-zero frame", paused["next_frame"] > 0,
              str(paused["next_frame"]))
        paused_text = (td / "out.srt").read_text(encoding="utf-8")
        check("subtitles survive cancel", len(paused_text) > 0)

        resumed = build_runner(td, wav, "fp-1").run(wav, total_sec=40.0)
        resumed_text = (td / "out.srt").read_text(encoding="utf-8")
        check("same-fingerprint resume is byte-identical to a full run",
              resumed["status"] == "done" and resumed_text == full_text,
              f"full={len(full_text)}B resumed={len(resumed_text)}B")

        reset_outputs(td, "fp-1")
        ck = Checkpoint(td / "state" / "checkpoints.db", "same")
        ck.save(400, 8, "paused", fingerprint="fp-OTHER", vad_state="")
        r4 = build_runner(td, wav, "fp-1").run(wav, total_sec=40.0)
        check("changed fingerprint restarts from zero",
              r4["status"] == "done" and r4["segments"] > 0,
              f"status={r4['status']} segments={r4['segments']}")
        check("changed fingerprint does not keep stale progress",
              (td / "out.srt").read_text(encoding="utf-8") == full_text)

    bad = [n for n, ok_, _ in RESULTS if not ok_]
    print(f"\nResult: {len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    if bad:
        print("Failed: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
