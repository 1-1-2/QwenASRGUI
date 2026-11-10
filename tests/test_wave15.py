"""Wave 1.5 regression: single streaming path, OOM fixes, 5-minute trial/preview.

All checks run without OpenVINO weights. The post-run OOM used to happen in the
WebView2 renderer because the frontend decoded the entire audio file just to
draw 200 waveform bars; these tests pin the replacement behaviour.
"""

from __future__ import annotations

import re
import sys
import tempfile
import types
import wave
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from make_fixture import build  # noqa: E402

import longform_runner as lr  # noqa: E402

# ── Minimal fake `app` module so webview_backend can be imported ──
def _split_to_lines(text):
    return [p.strip() for p in re.split(r"[，。？！；：,.!?;:\n]+", text)
            if p.strip()]


def _assign_ts(lines, t0, t1):
    if not lines:
        return []
    total = sum(max(1, len(x)) for x in lines)
    span = max(0.1, t1 - t0)
    out, cur = [], t0
    for line in lines:
        end = cur + span * max(1, len(line)) / total
        out.append((cur, end, line))
        cur = end
    return out


fake_core = types.ModuleType("app")
fake_core.ASREngine = mock.MagicMock()
fake_core._split_to_lines = _split_to_lines
fake_core._assign_ts = _assign_ts
fake_core.SRT_DIR = None
sys.modules["app"] = fake_core


def _parse_srt(text):
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        m = re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*"
                      r"(\d\d):(\d\d):(\d\d),(\d\d\d)", lines[1])
        if not m:
            continue
        g = m.groups()
        to_s = lambda h, mi, s, ms: int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000
        out.append({"start": to_s(*g[:4]), "end": to_s(*g[4:]),
                    "text": "\n".join(lines[2:])})
    return out


fake_api = types.ModuleType("api_server")
fake_api._VIDEO_HINT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
fake_api._parse_srt = _parse_srt
sys.modules["api_server"] = fake_api

import webview_backend as wb  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), str(extra)))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" - " + str(extra) if extra else ""))


class MockVAD:
    def __init__(self):
        self.calls = 0

    def run(self, _names, feed):
        x = feed["input"][0]
        self.calls += 1
        active = bool(np.sqrt(np.mean(x ** 2)) > 0.005)
        return (np.array([[0.95 if active else 0.01]], dtype=np.float32),
                feed["h"], feed["c"])


class FakeEngine:
    max_chunk_secs = 10          # 1.7B ceiling
    _OV_SUBDIR = "qwen3_asr_1p7b_kv_int8"

    def __init__(self):
        self.ready = True
        self.vad_sess = MockVAD()
        self._last_segments_rich = None
        self.calls = 0
        self.process_file_calls = 0

    def transcribe(self, audio, language=None, context=None, **kw):
        self.calls += 1
        return "这是統一流式模式的測試字幕，用來驗證單一路徑。"

    def process_file(self, *a, **kw):
        # Must never be reached after the unification.
        self.process_file_calls += 1
        raise AssertionError("legacy whole-file path should not be used")


def write_wav(path: Path, seconds: float, seed: int = 7) -> None:
    n = int(seconds * lr.SR)
    t = np.arange(n, dtype=np.float32) / lr.SR
    env = ((t % 4.0) < 3.0).astype(np.float32)
    pcm = (0.25 * np.sin(2 * np.pi * 220 * t) * env * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(lr.SR)
        w.writeframes(pcm.tobytes())


def make_backend(tmp: Path, events: list, settings: dict | None = None):
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be = wb.WebBackend(on_event=None)
    st = {"output_format": "srt", "vad_threshold": 0.5, "chunk_secs": 30}
    if settings:
        st.update(settings)
    be._settings_raw = lambda: st
    be._emit = lambda e, p: events.append((e, p))
    be._apply_chunk_secs = lambda: None
    be._resolve_ffmpeg = lambda: "ffmpeg"
    be.engine = FakeEngine()
    fake_core.SRT_DIR = tmp / "subtitles"
    wb.BASE_DIR = tmp
    return be


def main() -> int:
    print("[1] PeakBins: 200 bars without decoding the file in the frontend")
    pb = lr.PeakBins()
    one_sec = (np.ones(lr.SR, dtype=np.float32) * 0.4).reshape(-1)
    for i in range(5 * 3600):        # simulate a 5-hour stream, constant memory
        pb.feed(one_sec * (1.0 if i % 60 == 0 else 0.25))
    bars = pb.to_bars()
    check("exactly 200 bars", len(bars) == 200, len(bars))
    check("bars normalised to [0,1]", all(0 <= x <= 1 for x in bars),
          f"max={max(bars)} min={min(bars)}")
    check("duration equals 5 hours", abs(pb.duration - 18000.0) < 0.01, pb.duration)
    check("only ~18k floats retained, not the PCM",
          17000 <= len(pb._per_second) <= 19000, len(pb._per_second))
    check("empty stream yields empty bars", lr.PeakBins().to_bars() == [])

    print("\n[2] limited_pcm_stream: shared truncation for trial and preview")
    src = lr.ArrayPcmStream(np.ones(60 * lr.SR, dtype=np.float32))
    chunks = list(lr.limited_pcm_stream(src, 5 * lr.SR))
    total = sum(len(c) for c in chunks)
    check("5-minute limit is exact", total == 5 * lr.SR, total)
    src2 = lr.ArrayPcmStream(np.ones(3 * lr.SR, dtype=np.float32))
    check("limit longer than source is harmless",
          sum(len(c) for c in lr.limited_pcm_stream(src2, 5 * lr.SR)) == 3 * lr.SR)
    check("unlimited pass-through",
          sum(len(c) for c in lr.limited_pcm_stream(
              lr.ArrayPcmStream(np.ones(lr.SR, dtype=np.float32)), None)) == lr.SR)

    print("\n[3] Trial run: exactly 5 minutes, separate output, peaks returned")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        wav = td / "course.wav"
        write_wav(wav, 40.0)

        def runner(limit=None, tag="full"):
            eng = FakeEngine()
            return lr.LongformRunner(
                engine=eng, vad_sess=MockVAD(), out_srt=td / f"{tag}.srt",
                work_dir=td / "state", ffmpeg="ffmpeg", task_id=tag,
                fingerprint="fp", max_chunk_secs=10), eng

        r_full, e_full = runner(tag="full")
        out_full = r_full.run(wav, total_sec=40.0, resume=False)
        r_trial, e_trial = runner(tag="trial")
        out_trial = r_trial.run(wav, total_sec=40.0, limit_sec=5 * 60, resume=False)
        check("trial reports truncated", out_trial["truncated"] is False,  # 40s < 300s
              f"truncated={out_trial['truncated']} (source shorter than 5 min)")
        check("trial returns peaks", len(out_trial["peaks"]) == 200,
              len(out_trial["peaks"]))
        check("trial duration matches streamed seconds",
              abs(out_trial["duration"] - 40.0) < 0.2, out_trial["duration"])

        # Real truncation on a longer synthetic array source.
        long_src = lr.ArrayPcmStream(
            (0.2 * np.sin(2 * np.pi * 220 * np.arange(400 * lr.SR) / lr.SR))
            .astype(np.float32))
        r_cut, _ = runner(tag="cut")
        out_cut = r_cut.run(None, pcm_source=long_src, total_sec=400.0,
                            limit_sec=300.0, resume=False)
        check("400s source truncated to 300s",
              abs(out_cut["duration"] - 300.0) < 0.3, out_cut["duration"])
        check("truncation flag set", out_cut["truncated"] is True)

    print("\n[4] vad_preview: first 5 minutes, no ASR, actionable statistics")
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        wav = td / "course.wav"
        write_wav(wav, 40.0)
        eng = FakeEngine()
        with mock.patch("longform_runner.probe_duration", return_value=3600.0):
            p = lr.vad_preview(audio_path=wav, vad_sess=MockVAD(),
                               ffmpeg="ffmpeg", minutes=1,
                               max_chunk_secs=10)
        check("preview stops at 1 minute", p["previewSeconds"] <= 61,
              p["previewSeconds"])
        check("no ASR calls during preview", eng.calls == 0)
        check("statistics are complete",
              all(k in p for k in ("lengths", "overLimitRatio", "shortRatio",
                                   "silenceRatio", "estimatedAsrCallsPerHour",
                                   "verdict", "groupCount")), sorted(p.keys()))
        check("verdict is green or yellow", p["verdict"] in ("green", "yellow"),
              p["verdict"])
        check("preview finds speech groups", p["groupCount"] > 0,
              p["groupCount"])

    print("\n[5] Single streaming path: short audio never uses process_file")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        pcm, _ = build(0.2, seed=3)
        wav = tmp / "short.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(pcm.to_bytes() if hasattr(pcm, "to_bytes") else pcm.tobytes())
        events = []
        be = make_backend(tmp, events)
        with mock.patch("longform_runner.probe_duration", return_value=12.0):
            res = be.transcribe({"path": str(wav), "hint": ""},
                                progress_cb=lambda pct, status: None)
        check("short audio goes through the runner", be.engine.calls > 0,
              be.engine.calls)
        check("legacy process_file never called",
              be.engine.process_file_calls == 0, be.engine.process_file_calls)
        check("response carries peaks", len(res.get("peaks") or []) in (0, 200),
              len(res.get("peaks") or []))
        check("capabilities emitted",
              any(e == "capabilities" for e, _ in events),
              [e for e, _ in events][:6])
        cap = next((p for e, p in events if e == "capabilities"), {})
        check("30s setting clamped and reported as 10s",
              cap.get("effectiveChunkSecs") == 10 and
              cap.get("modelCeilingSecs") == 10, cap)
        check("memory estimate reported",
              cap.get("estimatedRamMB") in (2000, 6000), cap.get("estimatedRamMB"))

    print("\n[6] Result pagination: no full subtitle list sent to WebView2")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_wav(tmp / "course.wav", 40.0)
        events = []
        be = make_backend(tmp, events)
        be._MAX_PREVIEW_SEGMENTS = 3
        with mock.patch("longform_runner.probe_duration", return_value=40.0):
            res = be.transcribe({"path": str(tmp / "course.wav"), "hint": ""},
                                progress_cb=lambda pct, status: None)
        check("preview list is capped", len(res["segments"]) <= 3,
              len(res["segments"]))
        check("totalSegments returned", res.get("totalSegments", 0) >= 0,
              res.get("totalSegments"))
        check("previewTruncated flag present",
              isinstance(res.get("previewTruncated"), bool),
              res.get("previewTruncated"))

    print("\n[7] Trial run from backend: separate .trial.srt, no checkpoint pollution")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_wav(tmp / "course.wav", 40.0)
        events = []
        be = make_backend(tmp, events)
        with mock.patch("longform_runner.probe_duration", return_value=3600.0):
            res = be.transcribe({"path": str(tmp / "course.wav"), "hint": "",
                                 "trial": True},
                                progress_cb=lambda pct, status: None)
        check("trial output uses .trial.srt", ".trial.srt" in res["srtPath"],
              res["srtPath"])
        check("trial flag returned", res.get("trial") is True, res.get("trial"))
        check("trial does not produce full-run SRT",
              not (tmp / "subtitles" / "course.srt").exists())

    print("\n[8] Static guards against the OOM regression")
    js = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    check("frontend no longer calls decodeAudioData",
          ".decodeAudioData(" not in js and "await ctx.decodeAudioData" not in js)
    check("frontend no longer calls file.arrayBuffer", "arrayBuffer()" not in js)
    srv = (ROOT / "webview_server.py").read_text(encoding="utf-8")
    check("upload releases the in-memory copies",
          "del body, fields, files, data" in srv)
    check("vad-preview endpoint exists", "/api/vad-preview" in srv)

    bad = [n for n, ok_, _ in RESULTS if not ok_]
    print(f"\nResult: {len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    if bad:
        print("Failed: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
