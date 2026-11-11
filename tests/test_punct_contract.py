"""C1 contract: punctuation is refined in place on `done`, never on `paused`,
and A2 (resume == full run, byte for byte) still holds with refinement on.

Runs without model weights; synthetic WAV + mock VAD/ASR only.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from longform_runner import LongformRunner, SR
from subtitle_extras import parse_srt, refine_srt_text

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" - " + str(extra) if extra else ""))


def make_wav(path: Path, seconds: float = 8.0) -> None:
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    env = ((t % 4.0) < 3.0).astype(np.float32)
    pcm = (0.25 * np.sin(2 * np.pi * 220 * t) * env * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


class MockVAD:
    def run(self, _names, feed):
        x = feed["input"][0]
        active = bool(np.abs(x).mean() > 0.002)
        return (np.array([[0.9 if active else 0.05]], dtype=np.float32),
                feed["h"], feed["c"])


class CjkASR:
    """Deterministic output that depends only on audio, never on call count.

    A call-count-dependent engine would make a resumed run produce a different
    text sequence than a full run, which is a fixture artefact that would falsify
    the A2 assertion. Leading-comma handling is covered by the pure-function
    checks in section [1] instead.
    """
    max_chunk_secs = 30

    def __init__(self):
        self.n = 0

    def transcribe(self, chunk, language=None, context=None):
        self.n += 1
        return "今天我們來討論市場的走向"


def runner(td: Path, wav: Path, cancel=None, refine=True) -> LongformRunner:
    return LongformRunner(
        engine=CjkASR(), vad_sess=MockVAD(),
        out_srt=td / "out.srt", work_dir=td / "state",
        ffmpeg="ffmpeg", task_id="punct", fingerprint="fp-1",
        cancel=cancel, max_chunk_secs=30, refine_punct=refine,
    )


def texts_of(td: Path) -> list[str]:
    return [e["text"] for e in parse_srt((td / "out.srt").read_text(encoding="utf-8"))]


def main() -> int:
    print("[1] Pure refinement helper")
    raw = ("1\n00:00:01,000 --> 00:00:03,000\n今天我們來討論市場的走向\n\n"
           "2\n00:00:03,200 --> 00:00:06,000\n，以及後續的風險。\n")
    refined = refine_srt_text(raw, cjk_only=True)
    ents = parse_srt(refined)
    check("timestamp lines preserved verbatim",
          [e["ts"] for e in ents] ==
          ["00:00:01,000 --> 00:00:03,000", "00:00:03,200 --> 00:00:06,000"])
    check("unterminated line gains a comma", ents[0]["text"].endswith("，"),
          ents[0]["text"])
    check("leading comma stripped", not ents[1]["text"].startswith("，"),
          ents[1]["text"])
    check("non-CJK lines untouched when cjk_only",
          "hello" in refine_srt_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n",
                                     cjk_only=True))

    print("\n[2] Runner applies refinement on done, in place")
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wav = td / "src.wav"
        make_wav(wav)
        res = runner(td, wav).run(wav, total_sec=8.0)
        txt = (td / "out.srt").read_text(encoding="utf-8")
        check("run completes", res["status"] == "done", str(res["status"]))
        check("no separate .punct.srt is produced",
              not (td / "out.punct.srt").exists())
        check("final SRT lines are punctuated",
              len(texts_of(td)) > 0 and all(
                  t[-1] in "，。！？!?…；;" for t in texts_of(td)),
              str(texts_of(td)[:3]))
        check("final SRT has no leading punctuation",
              all(t[0] not in "，,、：:" for t in texts_of(td)))
        check("refined file is not the raw append stream",
              "今天我們來討論市場的走向\n" not in txt or
              "今天我們來討論市場的走向，" in txt)

    print("\n[3] opt-out keeps the raw append stream")
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wav = td / "src.wav"
        make_wav(wav)
        runner(td, wav, refine=False).run(wav, total_sec=8.0)
        raw_texts = texts_of(td)
        check("refine_punct=False leaves text verbatim",
              any(t.startswith("，") or not t[-1] in "，。！？!?…；;"
                  for t in raw_texts), str(raw_texts[:3]))

    print("\n[4] paused path is NOT refined; resume then refines exactly once")
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wav = td / "src.wav"
        # 40 s gives enough PCM chunks for the cancel flag to be observed at a
        # group boundary; an 8 s clip finishes before the flag is checked.
        make_wav(wav, 40.0)
        full = runner(td, wav).run(wav, total_sec=40.0)
        full_text = (td / "out.srt").read_text(encoding="utf-8")

        # Clean slate, then cancel partway through.
        (td / "out.srt").write_text("", encoding="utf-8")
        (td / "state" / "punct.segments.jsonl").write_text("", encoding="utf-8")
        from longform_runner import Checkpoint
        Checkpoint(td / "state" / "checkpoints.db", "punct").save(
            0, 1, "new", fingerprint="fp-1", vad_state="")
        ev = threading.Event()
        r2 = runner(td, wav, cancel=ev)
        orig = r2.engine.transcribe

        def stop_after(chunk, language=None, context=None):
            if r2.engine.n >= 2:
                ev.set()
            return orig(chunk, language=language, context=context)

        r2.engine.transcribe = stop_after
        paused = r2.run(wav, total_sec=40.0)
        paused_text = (td / "out.srt").read_text(encoding="utf-8")
        check("cancel reports paused", paused["status"] == "paused",
              str(paused["status"]))
        check("paused SRT keeps raw lines (not refined)",
              paused_text != full_text and len(paused_text) > 0)

        resumed = runner(td, wav).run(wav, total_sec=40.0)
        resumed_text = (td / "out.srt").read_text(encoding="utf-8")
        check("A2: resumed final SRT is byte-identical to a full run",
              resumed["status"] == "done" and resumed_text == full_text,
              f"full={len(full_text)}B resumed={len(resumed_text)}B")
        check("resume refined exactly once (not double-applied)",
              "。。" not in resumed_text)

    print("\n[5] done short-circuits without re-refining")
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wav = td / "src.wav"
        make_wav(wav)
        runner(td, wav).run(wav, total_sec=8.0)
        once = (td / "out.srt").read_text(encoding="utf-8")
        again = runner(td, wav).run(wav, total_sec=8.0)
        check("second start reports resumed", again.get("resumed") is True)
        check("SRT unchanged on second start",
              (td / "out.srt").read_text(encoding="utf-8") == once)

    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\nResult: {ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
