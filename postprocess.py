"""Optional enrichment of the streaming transcript.

Design rules (these are the whole point of this module):

* The SRT produced by ``LongformRunner`` is the source of truth. Enrichment
  results live in a sibling ``*.rich.json`` file and are never written back
  into the SRT. A missing aligner model, a crashed chatllm subprocess, a
  clustering error or an unreadable JSON line can therefore never cost the
  user their subtitles.
* Both enrichments consume the PCM chunk that is already in memory during
  recognition. There is no second decode of the audio file and no
  whole-waveform allocation, so the O(1)-memory property of the streaming
  pipeline is preserved.
* Everything is best-effort. Each hook catches its own exceptions and reports
  through ``status_cb``; ``finalize()`` always returns a usable segment list.

Word-level timing
-----------------
``engine._align_chunk(wav_path, ref_text, language)`` already exists and takes
a small WAV plus the recognised text. The original ``process_file()`` path
called it once per speech group; we call it at exactly the same granularity,
writing the current chunk to a temporary WAV. No new dependency.

Speaker diarization
-------------------
``DiarizationEngine._get_embedding(audio, t0, t1)`` and ``_cluster_auto`` /
``_cluster_fixed_n`` already exist. Instead of handing them a 5-hour waveform,
we extract one embedding per speech group from the chunk (relative window
0..len/SR), keep only the 256-d vectors (~1 KB each), and cluster at the end.
Labels are assigned to subtitle lines by time overlap.
"""
from __future__ import annotations

import json
import os
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 16000


def _write_temp_wav(chunk: np.ndarray) -> str | None:
    """Write a 16 kHz mono s16le WAV for the aligner subprocess."""
    try:
        arr = np.asarray(chunk, dtype=np.float32)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype("<i2")
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())
        return path
    except Exception:
        return None


class StreamEnhancer:
    """Collect per-line words / speaker labels while the runner streams.

    Parameters
    ----------
    engine : ASR engine; may expose ``_align_chunk`` and ``diar_engine``.
    rich_path : output path for the enrichment JSON.
    enable_align / enable_diar : user switches AND model availability.
    existing_jsonl : when resuming, seed the line list from prior output so
        checkpoints do not lose words / speakers for already finished groups.
    """

    def __init__(self, engine, rich_path, language=None,
                 enable_align=False, enable_diar=False,
                 n_speakers=None, status_cb=None, existing_jsonl=None):
        self.engine = engine
        self.rich_path = Path(rich_path)
        self.language = language
        self.enable_align = bool(enable_align)
        self.enable_diar = bool(enable_diar)
        self.n_speakers = n_speakers
        self.status_cb = status_cb or (lambda msg: None)
        # Enrichments are keyed by subtitle index. The authoritative line list
        # is re-read from the runner's JSONL in finalize(), so a resumed run or
        # a checkpoint mismatch can never duplicate or lose earlier lines.
        self._by_index: dict[int, dict] = {}
        self._embeds: list[tuple[float, float, np.ndarray]] = []
        self._notes: list[str] = []
        self.existing_jsonl = Path(existing_jsonl) if existing_jsonl else None

    def _note(self, msg: str) -> None:
        self._notes.append(msg)
        try:
            self.status_cb(msg)
        except Exception:
            pass

    @property
    def active(self) -> bool:
        return self.enable_align or self.enable_diar

    def on_group(self, chunk: np.ndarray, t0: float, t1: float,
                 text: str, lines: list, start_index: int = 1) -> None:
        """Called once per speech group, before the chunk leaves scope.

        ``lines`` is the list of ``(start, end, text)`` tuples the runner is
        about to append; ``start_index`` is the 1-based SRT index of its first
        line. Everything here is best-effort: on any failure the lines still
        land in the SRT with empty ``words`` and no speaker.
        """
        for i, (s, e, line) in enumerate(lines):
            self._by_index[start_index + i] = {"words": []}
        if not self.active:
            return

        if self.enable_align:
            self._align(chunk, t0, text, lines, start_index)
        if self.enable_diar:
            self._embed(chunk, t0, t1)

    def _align(self, chunk, t0: float, text: str, lines: list,
               start_index: int) -> None:
        path = None
        try:
            fn = getattr(self.engine, "_align_chunk", None)
            if fn is None or not callable(fn):
                return
            path = _write_temp_wav(chunk)
            if not path:
                return
            lang = self.language if self.language and self.language != "auto" else "Chinese"
            items = fn(path, text, lang) or []
            if not items:
                return
            # align_chunk returns (word, local_start_s, local_end_s); shift to
            # absolute audio time, then attach each word to the line whose
            # interval contains its midpoint.
            words = []
            for w, ws, we in items:
                try:
                    words.append((str(w), t0 + float(ws), t0 + float(we)))
                except (TypeError, ValueError):
                    continue
            for i, (s, e, _line) in enumerate(lines):
                picked = [{"word": w, "start": round(a, 3), "end": round(b, 3)}
                          for w, a, b in words if s <= (a + b) / 2 < e]
                rec = self._by_index.get(start_index + i)
                if rec is not None:
                    rec["words"] = picked
        except Exception as exc:
            self._note(f"Word-level alignment skipped: {exc}")
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _embed(self, chunk, t0: float, t1: float) -> None:
        try:
            eng = getattr(self.engine, "diar_engine", None)
            fn = getattr(eng, "_get_embedding", None)
            if not callable(fn):
                return
            arr = np.asarray(chunk, dtype=np.float32)
            if arr.size < SR // 2:
                return
            vec = fn(arr, 0.0, arr.size / SR)
            if vec is not None:
                self._embeds.append((t0, t1, np.asarray(vec, dtype=np.float32)))
        except Exception as exc:
            self._note(f"Speaker embedding skipped: {exc}")

    def _speaker_labels(self) -> list[tuple[float, float, int]]:
        """Cluster the collected embeddings into (t0, t1, label) intervals."""
        if not self._embeds:
            return []
        eng = getattr(self.engine, "diar_engine", None)
        if eng is None:
            return []
        vecs = [v for _a, _b, v in self._embeds]
        try:
            if self.n_speakers:
                labels = eng._cluster_fixed_n(vecs, int(self.n_speakers))
            elif len(vecs) == 1:
                labels = [1]
            else:
                labels = eng._cluster_auto(vecs)
        except Exception as exc:
            self._note(f"Speaker clustering skipped: {exc}")
            return []
        out = []
        for (a, b, _v), lab in zip(self._embeds, labels):
            try:
                out.append((a, b, int(lab)))
            except (TypeError, ValueError):
                continue
        return out

    def finalize(self, jsonl_path=None) -> list[dict]:
        """Return rich segments and atomically write ``*.rich.json``.

        Never raises. The authoritative line list comes from the runner's
        JSONL, so the runner's SRT is untouched regardless of outcome and
        resume / restart cannot duplicate lines.
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
                        d = json.loads(raw)
                        idx = int(d["index"])
                        rec = self._by_index.get(idx, {})
                        lines.append({
                            "start": float(d["start"]), "end": float(d["end"]),
                            "text": str(d.get("text", "")),
                            "words": rec.get("words") or [],
                            **({"speaker": rec["speaker"]} if rec.get("speaker") else {}),
                        })
        except Exception as exc:
            self._note(f"Enrichment line rebuild failed: {exc}")
            lines = []

        intervals = self._speaker_labels() if self.enable_diar else []
        if intervals and lines:
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

        try:
            self.rich_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.rich_path.with_suffix(self.rich_path.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"segments": lines, "notes": self._notes,
                 "align": self.enable_align, "diarize": self.enable_diar},
                ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.rich_path))
        except Exception as exc:
            self._note(f"Enrichment file not written: {exc}")
        return lines


def load_rich(path) -> list[dict] | None:
    """Read a previously written rich file; return None on any problem."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        segs = d.get("segments")
        return segs if isinstance(segs, list) and segs else None
    except Exception:
        return None
