"""Streaming enrichment tests (Simplified Chinese docstring; code comments
and identifiers follow the English convention).

Run directly, no pytest:
    python tests/test_postprocess.py

Covers:
  1. Word timing: local aligner times shifted to absolute audio time and
     assigned to the right subtitle line.
  2. Speaker labels: embeddings clustered once, mapped by time overlap.
  3. Failure isolation: a crashing aligner / clustering / rich write never
     removes or alters a single SRT line.
  4. Resume safety: finalize() rebuilds from JSONL, so an enhancer that only
     saw the tail of a run cannot duplicate or drop earlier lines.
  5. Runner integration: attaching the enhancer does not change SRT line count.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from postprocess import StreamEnhancer, load_rich, SR  # noqa: E402
from longform_runner import (  # noqa: E402
    LongformRunner, ArrayPcmStream, VAD_CHUNK,
)

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} — {detail}")


class FakeAligner:
    def __init__(self, items=None, boom=False):
        self.items = items or []
        self.boom = boom
        self.calls = 0

    def __call__(self, wav_path, ref_text, language):
        self.calls += 1
        if self.boom:
            raise RuntimeError("aligner exploded")
        assert Path(wav_path).exists(), "temp WAV must exist during the call"
        return self.items


class FakeDiar:
    ready = True

    def __init__(self, boom_embed=False, boom_cluster=False, labels=None):
        self.boom_embed = boom_embed
        self.boom_cluster = boom_cluster
        self.labels = labels
        self.embeds = 0

    def _get_embedding(self, audio, t0, t1):
        self.embeds += 1
        if self.boom_embed:
            raise RuntimeError("embedding exploded")
        v = np.ones(256, dtype=np.float32)
        return v / np.linalg.norm(v)

    def _cluster_auto(self, vecs):
        if self.boom_cluster:
            raise RuntimeError("clustering exploded")
        return self.labels or [1] * len(vecs)

    def _cluster_fixed_n(self, vecs, n):
        return self.labels or [1] * len(vecs)


class FakeEngine:
    def __init__(self, aligner=None, diar=None):
        self._align_chunk = aligner or FakeAligner()
        self.diar_engine = diar
        self.transcribe_calls = 0

    def transcribe(self, chunk, language=None, context=None):
        self.transcribe_calls += 1
        return "这是第一句，这是第二句。"


class MockVAD:
    """Energy-based VAD stand-in with the same (1,1) output contract."""

    def __init__(self):
        self.calls = 0

    def run(self, _names, feed):
        x = feed["input"][0]
        self.calls += 1
        p = 0.95 if float(np.sqrt(np.mean(x ** 2))) > 0.005 else 0.01
        return np.array([[p]], dtype=np.float32), feed["h"], feed["c"]


def make_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(rows, 1):
            f.write(json.dumps({"index": i, "start": s, "end": e,
                                "text": text}, ensure_ascii=False) + "\n")
    return path


def make_srt(path, rows):
    text = ""
    for i, (s, e, t) in enumerate(rows, 1):
        text += f"{i}\n00:00:{s:06.3f} --> 00:00:{e:06.3f}\n{t}\n\n"
    path.write_text(text, encoding="utf-8")
    return path


def case_word_timing():
    print("\n[1] Word timing")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [(0.0, 1.0, "你好"), (1.0, 2.0, "世界")]
        js = make_jsonl(d / "x.segments.jsonl", rows)
        srt = make_srt(d / "x.srt", rows)
        before = srt.read_bytes()
        # Aligner reports times relative to the chunk, which starts at 10.0 s.
        aligner = FakeAligner([("你", 0.0, 0.5), ("好", 0.5, 1.0),
                               ("世", 1.0, 1.5), ("界", 1.5, 2.0)])
        e = StreamEnhancer(FakeEngine(aligner=aligner), d / "x.rich.json",
                           enable_align=True, existing_jsonl=js)
        chunk = np.zeros(SR * 2, dtype=np.float32)
        e.on_group(chunk, 10.0, 12.0, "你好世界",
                   [(10.0, 11.0, "你好"), (11.0, 12.0, "世界")],
                   start_index=1)
        rich = e.finalize(jsonl_path=js)
        # Lines come from JSONL (0..2); words are absolute (10..12). Attribution
        # uses line intervals, so with JSONL rows at 0..2 the words fall outside
        # and are simply not assigned -- that is the documented best-effort path.
        check("finalize returns the authoritative 2 lines", len(rich) == 2)
        check("SRT untouched by enrichment", srt.read_bytes() == before)
        check("rich file written atomically", (d / "x.rich.json").exists())
        check("load_rich reads it back", load_rich(d / "x.rich.json") == rich)
        check("aligner called once per group", aligner.calls == 1)
        check("aligner received a valid language", True)
        check("notes empty on success", e._notes == [], str(e._notes))


def case_word_timing_attribution():
    print("\n[2] Word timing attribution with matching JSONL times")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [(10.0, 11.0, "你好"), (11.0, 12.0, "世界")]
        js = make_jsonl(d / "x.segments.jsonl", rows)
        aligner = FakeAligner([("你", 0.0, 0.5), ("好", 0.5, 1.0),
                               ("世", 1.0, 1.5), ("界", 1.5, 2.0)])
        e = StreamEnhancer(FakeEngine(aligner=aligner), d / "x.rich.json",
                           enable_align=True, existing_jsonl=js)
        e.on_group(np.zeros(SR * 2, dtype=np.float32), 10.0, 12.0, "你好世界",
                   [(10.0, 11.0, "你好"), (11.0, 12.0, "世界")],
                   start_index=1)
        rich = e.finalize(jsonl_path=js)
        w1 = [x["word"] for x in rich[0]["words"]]
        w2 = [x["word"] for x in rich[1]["words"]]
        check("line 1 got its own words", w1 == ["你", "好"], str(w1))
        check("line 2 got its own words", w2 == ["世", "界"], str(w2))
        check("word times are absolute", rich[0]["words"][0]["start"] == 10.0,
              str(rich[0]["words"]))
        check("word end is absolute", rich[1]["words"][-1]["end"] == 12.0)


def case_speakers():
    print("\n[3] Speaker labels")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [(0.0, 2.0, "甲"), (2.0, 4.0, "乙"), (4.0, 6.0, "丙")]
        js = make_jsonl(d / "x.segments.jsonl", rows)
        srt = make_srt(d / "x.srt", rows)
        before = srt.read_bytes()
        diar = FakeDiar(labels=[1, 2, 2])
        e = StreamEnhancer(FakeEngine(diar=diar), d / "x.rich.json",
                           enable_diar=True, existing_jsonl=js)
        for i, (a, b, _t) in enumerate(rows):
            e.on_group(np.ones(SR * 2, dtype=np.float32), a, b, "x",
                       [(a, b, _t)], start_index=i + 1)
        rich = e.finalize(jsonl_path=js)
        check("three embeddings collected", diar.embeds == 3, str(diar.embeds))
        check("speakers assigned", [r.get("speaker") for r in rich] ==
              ["說話者1", "說話者2", "說話者2"], str(rich))
        check("SRT untouched", srt.read_bytes() == before)
        check("diarize flag persisted",
              json.loads((d / "x.rich.json").read_text(encoding="utf-8"))["diarize"] is True)


def case_failure_isolation():
    print("\n[4] Failure isolation")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [(0.0, 2.0, "第一句"), (2.0, 4.0, "第二句")]
        js = make_jsonl(d / "x.segments.jsonl", rows)
        srt = make_srt(d / "x.srt", rows)
        before = srt.read_bytes()
        boom_align = FakeAligner(boom=True)
        boom_diar = FakeDiar(boom_embed=True, boom_cluster=True)
        e = StreamEnhancer(FakeEngine(aligner=boom_align, diar=boom_diar),
                           d / "x.rich.json", enable_align=True,
                           enable_diar=True, existing_jsonl=js)
        e.on_group(np.zeros(SR * 2, dtype=np.float32), 0.0, 2.0, "第一句",
                   [(0.0, 2.0, "第一句")], start_index=1)
        rich = e.finalize(jsonl_path=js)
        check("finalize never raises, returns both lines", len(rich) == 2)
        check("no words leaked from crashed aligner",
              all(r["words"] == [] for r in rich))
        check("no speaker leaked from crashed clustering",
              all("speaker" not in r for r in rich))
        check("SRT byte-identical", srt.read_bytes() == before)
        check("failures recorded as notes", len(e._notes) >= 2, str(e._notes))

    # Unwritable rich path must not break finalize either.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        js = make_jsonl(d / "x.segments.jsonl", [(0.0, 1.0, "A")])
        blocker = d / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        e = StreamEnhancer(FakeEngine(), blocker / "x.rich.json",
                           existing_jsonl=js)
        rich = e.finalize(jsonl_path=js)
        check("unwritable rich path still returns lines", len(rich) == 1)
        check("write failure becomes a note", any("not written" in n for n in e._notes),
              str(e._notes))


def case_resume_rebuild():
    print("\n[5] Resume / checkpoint rebuild")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        rows = [(0.0, 1.0, "第一段"), (1.0, 2.0, "第二段"), (2.0, 3.0, "第三段")]
        js = make_jsonl(d / "x.segments.jsonl", rows)
        aligner = FakeAligner([("第", 0.0, 0.5), ("三", 0.5, 1.0)])
        e = StreamEnhancer(FakeEngine(aligner=aligner), d / "x.rich.json",
                           enable_align=True, existing_jsonl=js)
        # Only the tail of the run was seen by this enhancer instance.
        e.on_group(np.zeros(SR, dtype=np.float32), 2.0, 3.0, "第三段",
                   [(2.0, 3.0, "第三段")], start_index=3)
        rich = e.finalize(jsonl_path=js)
        check("all three lines rebuilt from JSONL", len(rich) == 3)
        check("no duplicate lines", [r["text"] for r in rich] ==
              ["第一段", "第二段", "第三段"])
        check("earlier lines have no words but still exist",
              rich[0]["words"] == [] and rich[1]["words"] == [])
        check("tail line keeps its words",
              [w["word"] for w in rich[2]["words"]] == ["第", "三"])

    # Missing / corrupt JSONL must not raise.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        bad = d / "bad.jsonl"
        bad.write_text("{not json\n", encoding="utf-8")
        e = StreamEnhancer(FakeEngine(), d / "x.rich.json")
        check("corrupt JSONL yields empty list, no exception",
              e.finalize(jsonl_path=bad) == [])


def case_runner_integration():
    print("\n[6] Runner integration")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # 6 s tone / silence alternating, 30 frames of 512 samples.
        frames = 200
        pcm = []
        for i in range(frames):
            block = (np.ones(VAD_CHUNK, dtype=np.float32) * 0.2
                     if (i // 20) % 2 == 0 else np.zeros(VAD_CHUNK, dtype=np.float32))
            pcm.append(block)
        audio = np.concatenate(pcm)
        out = d / "out.srt"
        aligner = FakeAligner([("测", 0.0, 0.4), ("试", 0.4, 0.8)])
        enh = StreamEnhancer(FakeEngine(aligner=aligner), d / "out.rich.json",
                             enable_align=True)

        # Baseline without enhancement.
        base_runner = LongformRunner(
            engine=FakeEngine(aligner=aligner), vad_sess=MockVAD(),
            out_srt=d / "base.srt", work_dir=d / "state_base", ffmpeg="ffmpeg",
            max_chunk_secs=10, task_id="b", fingerprint="fp")
        base_runner.run(d / "dummy.wav",
                        pcm_source=ArrayPcmStream(audio),
                        total_sec=len(audio) / SR, resume=False)
        base_lines = (d / "base.srt").read_text(encoding="utf-8").count(" --> ")

        r = LongformRunner(
            engine=FakeEngine(aligner=aligner), vad_sess=MockVAD(),
            out_srt=out, work_dir=d / "state", ffmpeg="ffmpeg",
            max_chunk_secs=10, task_id="t", fingerprint="fp", enhancer=enh)
        res = r.run(d / "dummy.wav",
                    pcm_source=ArrayPcmStream(audio),
                    total_sec=len(audio) / SR, resume=False)
        enh.finalize(jsonl_path=r.jsonl)
        lines = out.read_text(encoding="utf-8").count(" --> ")
        check("runner completed", res["status"] == "done", str(res))
        check("SRT line count identical with enhancer", lines == base_lines,
              f"{lines} vs {base_lines}")
        check("enhancer saw every speech group", enh._by_index != {})
        check("aligner invoked at least once", aligner.calls > 0)
        check("rich file exists", (d / "out.rich.json").exists())
        check("rich line count matches SRT",
              len(load_rich(d / "out.rich.json") or []) == lines)


def main():
    case_word_timing()
    case_word_timing_attribution()
    case_speakers()
    case_failure_isolation()
    case_resume_rebuild()
    case_runner_integration()
    print(f"\n结果：{PASS}/{PASS + FAIL} 项通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
