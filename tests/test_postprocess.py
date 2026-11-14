"""Enrichment synthesis tests (pure consumer).

Run directly, no pytest:
    python tests/test_postprocess.py

Covers:
  1. 三份产物（段级清单 / 词级 / 说话人向量）合成 rich.json；
  2. 产物缺失、坏行、半行时只丢字段，不抛异常；
  3. 增强器不写字幕、不改行文本；
  4. rich.json 原子写出，layout 版本正确。
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

from postprocess import StreamEnhancer, load_rich  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


class _Diar:
    """两个簇：向量首元素 > 0.5 归 1，否则归 2。"""

    def _cluster_auto(self, vecs):
        return [1 if float(np.asarray(v)[0]) > 0.5 else 2 for v in vecs]

    def _cluster_fixed_n(self, vecs, n):
        return self._cluster_auto(vecs)


def _segments(path: Path, rows):
    with path.open("w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(rows, 1):
            f.write(json.dumps({"index": i, "start": a, "end": b, "text": t},
                               ensure_ascii=False) + "\n")
    return path


def _fa(path: Path, rows: dict):
    with path.open("w", encoding="utf-8") as f:
        for idx, words in rows.items():
            f.write(json.dumps({"index": idx, "words": words},
                               ensure_ascii=False) + "\n")
    return path


def _diar(path: Path, items):
    with path.open("w", encoding="utf-8") as f:
        for a, b, vec in items:
            f.write(json.dumps({"t0": a, "t1": b, "vec": vec},
                               ensure_ascii=False) + "\n")
    return path


def test_compose():
    print("\n[1] 三份产物 → rich.json")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        seg = _segments(d / "x.segments.jsonl",
                        [(0.0, 2.0, "第一行"), (2.0, 4.0, "第二行"),
                         (4.0, 6.0, "第三行")])
        fa = _fa(d / "x.fa.jsonl", {
            1: [["第", 0.0, 0.5], ["一", 0.5, 1.0]],
            2: [["第", 2.0, 2.5], ["二", 2.5, 3.0]],
            3: [["第", 4.0, 4.5], ["三", 4.5, 5.0]],
        })
        di = _diar(d / "x.diar.jsonl", [
            (0.0, 2.0, [0.9, 0.1]), (2.0, 4.0, [0.1, 0.9]),
            (4.0, 6.0, [0.9, 0.1])])

        e = StreamEnhancer(d / "x.rich.json", enable_align=True,
                           enable_diar=True, diar_engine=_Diar())
        check("load_words 读到 3 行", e.load_words(fa) == 3)
        check("load_speakers 读到 3 个向量", e.load_speakers(di) == 3)
        segs = e.finalize(jsonl_path=seg)
        check("行数一致", len(segs) == 3, str(len(segs)))
        check("词级挂到正确的行",
              [len(s["words"]) for s in segs] == [2, 2, 2],
              str([len(s["words"]) for s in segs]))
        check("说话人标签已写入", all("speaker" in s for s in segs),
              str([s.get("speaker") for s in segs]))
        check("行文本未被改写",
              [s["text"] for s in segs] == ["第一行", "第二行", "第三行"])
        rich = json.loads((d / "x.rich.json").read_text(encoding="utf-8"))
        check("rich.json 可读且 layout=5", rich.get("layout") == 5,
              str(rich.get("layout")))
        check("align / diarize 标记正确",
              rich.get("align") is True and rich.get("diarize") is True)
        check("load_rich 能读回", len(load_rich(d / "x.rich.json") or []) == 3)


def test_degrade():
    print("\n[2] 产物缺失 / 损坏：只丢字段，不抛异常")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        seg = _segments(d / "y.segments.jsonl",
                        [(0.0, 2.0, "甲"), (2.0, 4.0, "乙")])
        fa = d / "y.fa.jsonl"
        fa.write_text('{"index": 1, "words": [["甲", 0, 1]]}\n'
                      '{"index": 2, "words": \n'
                      'not json at all\n', encoding="utf-8")
        e = StreamEnhancer(d / "y.rich.json", enable_align=True)
        n = e.load_words(fa)
        segs = e.finalize(jsonl_path=seg)
        check("坏行被跳过、好行保留", n == 1, str(n))
        check("没有词级的行 words 为空",
              segs[0]["words"] and segs[1]["words"] == [],
              str([len(s["words"]) for s in segs]))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        seg = _segments(d / "z.segments.jsonl", [(0.0, 1.0, "丙")])
        e = StreamEnhancer(d / "z.rich.json", enable_align=True)
        check("产物缺失时 load_words 返回 0", e.load_words(d / "none.jsonl") == 0)
        segs = e.finalize(jsonl_path=seg)
        check("仍然产出字幕行", len(segs) == 1 and segs[0]["words"] == [])

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        e = StreamEnhancer(d / "nope" / "w.rich.json", enable_align=True)
        segs = e.finalize(jsonl_path=d / "missing.jsonl")
        check("段级清单缺失时返回空列表且不抛", segs == [], str(segs))


def test_no_writeback():
    print("\n[3] 增强器不写字幕")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        srt = d / "v.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n原文\n\n",
                       encoding="utf-8")
        before = srt.read_text(encoding="utf-8")
        seg = _segments(d / "v.segments.jsonl", [(0.0, 2.0, "原文")])
        fa = _fa(d / "v.fa.jsonl", {1: [["原", 0.0, 1.0]]})
        e = StreamEnhancer(d / "v.rich.json", enable_align=True)
        e.load_words(fa)
        e.finalize(jsonl_path=seg)
        check("SRT 未被改动", srt.read_text(encoding="utf-8") == before)


def main() -> int:
    test_compose()
    test_degrade()
    test_no_writeback()
    print(f"\n{'ALL PASS' if FAIL == 0 else 'FAILED'} — {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
