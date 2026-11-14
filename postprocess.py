"""Enrichment synthesis — a pure consumer of pipeline products.

设计规则（这是本模块的全部要点）：

* SRT 是唯一事实来源。增强结果写在同一目录的 ``*.rich.json``，**永不回写 SRT**。
  词级文件缺失、说话人聚类失败、JSON 行损坏，都只让对应字段为空，不影响字幕。
* **本模块不参与识别循环，也不调用对齐或说话人模型。** 它只读三份产物：
  段级清单（``segments.jsonl``）、词级文件（``*.fa.jsonl``）、
  说话人向量文件（``*.diar.jsonl``），在 :meth:`finalize` 里合成 rich.json。
* 以前它挂在识别循环里，靠「复用现成的 PCM chunk」省一次解码；那条捷径把
  对齐重新塞回了识别线程（同步起子进程），成了阻塞源。现在对齐由独立管线
  负责，增强器只剩「收尾时合成」这一件事。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

SR = 16000


class StreamEnhancer:
    """Compose ``*.rich.json`` from the three product files.

    Parameters
    ----------
    rich_path : output path for the enrichment JSON.
    enable_align / enable_diar : user switches AND model availability.
        They only decide whether the corresponding product is read at all.
    diar_engine : only used for clustering the collected vectors at the end.
        May be ``None`` — then speaker labels are simply omitted.
    existing_jsonl : when resuming, seed from prior output.
    """

    def __init__(self, rich_path, language=None, enable_align=False,
                 enable_diar=False, n_speakers=None, status_cb=None,
                 existing_jsonl=None, diar_engine=None):
        self.rich_path = Path(rich_path)
        self.language = language
        self.enable_align = bool(enable_align)
        self.enable_diar = bool(enable_diar)
        self.n_speakers = n_speakers
        self.status_cb = status_cb or (lambda msg: None)
        self.diar_engine = diar_engine
        self._notes: list[str] = []
        # 行号 → 词级列表；行号 → 说话人标签
        self._words: dict[int, list] = {}
        self._speakers: dict[int, str] = {}
        self._embeds: list[tuple[float, float, np.ndarray]] = []
        self.existing_jsonl = Path(existing_jsonl) if existing_jsonl else None

    # ── 状态 ────────────────────────────────────────────────────
    def _note(self, msg: str) -> None:
        self._notes.append(msg)
        try:
            self.status_cb(msg)
        except Exception:
            pass

    @property
    def active(self) -> bool:
        return self.enable_align or self.enable_diar

    # ── 读产物 ──────────────────────────────────────────────────
    def load_words(self, fa_path) -> int:
        """把词级文件读进 ``_words``；返回读到的行数。

        格式：每行 ``{"index": 行号, "words": [[词, 起, 止], ...]}``。
        半行 / 坏行直接跳过 —— 词级是增强，不能因为它让整份失败。
        """
        path = Path(fa_path) if fa_path else None
        if not path or not path.exists():
            return 0
        n = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                        idx = int(d["index"])
                        words = d.get("words") or []
                    except (ValueError, TypeError, KeyError):
                        continue
                    self._words[idx] = self._coerce_words(words)
                    n += 1
        except OSError as exc:
            self._note(f"词级文件读取失败：{exc}")
        return n

    @staticmethod
    def _coerce_words(words) -> list[dict]:
        out = []
        for item in words or []:
            try:
                w, a, b = item[0], float(item[1]), float(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            out.append({"word": str(w), "start": round(a, 3), "end": round(b, 3)})
        return out

    def load_speakers(self, diar_path) -> int:
        """读说话人向量文件；返回读到的向量数。聚类在 finalize 时做。"""
        path = Path(diar_path) if diar_path else None
        if not path or not path.exists():
            return 0
        n = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                        vec = np.asarray(d["vec"], dtype=np.float32)
                        t0, t1 = float(d["t0"]), float(d["t1"])
                    except (ValueError, TypeError, KeyError):
                        continue
                    if vec.size:
                        self._embeds.append((t0, t1, vec))
                        n += 1
        except OSError as exc:
            self._note(f"说话人向量文件读取失败：{exc}")
        return n

    # ── 聚类 ────────────────────────────────────────────────────
    def _speaker_intervals(self) -> list[tuple[float, float, int]]:
        if not self._embeds:
            return []
        eng = self.diar_engine
        vecs = [v for _a, _b, v in self._embeds]
        if eng is None:
            # 没有聚类引擎时退化成「全是一个说话人」，比不给标签更有用。
            return [(a, b, 1) for a, b, _v in self._embeds]
        try:
            if self.n_speakers:
                labels = eng._cluster_fixed_n(vecs, int(self.n_speakers))
            elif len(vecs) == 1:
                labels = [1]
            else:
                labels = eng._cluster_auto(vecs)
        except Exception as exc:
            self._note(f"说话人聚类失败：{exc}")
            return []
        out = []
        for (a, b, _v), lab in zip(self._embeds, labels):
            try:
                out.append((a, b, int(lab)))
            except (TypeError, ValueError):
                continue
        return out

    def _assign_speakers(self, lines: list[dict]) -> None:
        intervals = self._speaker_intervals()
        if not intervals or not lines:
            return
        k = 0
        for line in lines:
            mid = (line["start"] + line["end"]) / 2
            while k < len(intervals) and intervals[k][1] < line["start"]:
                k += 1
            j, best = k, None
            while j < len(intervals) and intervals[j][0] <= mid:
                a, b, lab = intervals[j]
                if a <= mid <= b:
                    best = lab
                    break
                best = lab
                j += 1
            if best is not None:
                line["speaker"] = f"說話者{best}"

    # ── 合成 ────────────────────────────────────────────────────
    def finalize(self, jsonl_path=None) -> list[dict]:
        """Read the products, compose and atomically write ``*.rich.json``.

        Never raises. The authoritative line list comes from the runner's
        JSONL, so the SRT is untouched regardless of outcome and resume /
        restart cannot duplicate lines.
        """
        path = Path(jsonl_path) if jsonl_path else self.existing_jsonl
        lines: list[dict] = []
        try:
            if path and path.exists():
                with path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            continue
                        idx = int(d["index"])
                        rec = {"start": float(d["start"]), "end": float(d["end"]),
                               "text": str(d.get("text", "")),
                               "words": self._words.get(idx) or []}
                        lines.append(rec)
        except Exception as exc:
            self._note(f"增强文件重建失败：{exc}")
            lines = []

        if self.enable_diar:
            self._assign_speakers(lines)

        # 时间轴全局单调化：VAD group 之间本来就有 tail/padding 重叠，而每个
        # group 各自分配时间，拼起来必然有负间隔。这里是最后一道关口，
        # rich.json 一经写出就被 SRT 重建、对齐回填、导出全部沿用。
        try:
            from subtitle_lines import fix_timeline

            fixed = fix_timeline(
                [(x["start"], x["end"], x["text"], x.get("speaker"),
                  x.get("words") or []) for x in lines])
            lines = [{"start": round(float(s), 3), "end": round(float(e), 3),
                      "text": t, "words": w,
                      **({"speaker": spk} if spk else {})}
                     for (s, e, t, spk, w) in fixed]
        except Exception as exc:
            self._note(f"时间轴单调化跳过：{exc}")

        try:
            self.rich_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.rich_path.with_suffix(self.rich_path.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"segments": lines, "notes": self._notes,
                 "align": self.enable_align, "diarize": self.enable_diar,
                 # 版面模型版本：3 = 一条一行、每條 ≤20 字、長句按 ceil(L/20)
                 # 均分。4 = 由 FA 断行驱动。5 = 比例断行 + 独立词级回填。
                 "layout": 5},
                ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.rich_path))
        except Exception as exc:
            self._note(f"增强文件未写出：{exc}")
        return lines


def load_rich(path) -> list[dict] | None:
    """Read a previously written rich file; return None on any problem."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        segs = d.get("segments")
        return segs if isinstance(segs, list) and segs else None
    except Exception:
        return None
