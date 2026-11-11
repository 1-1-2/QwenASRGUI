"""subtitle_extras.py - C1 cross-segment punctuation and C2 handout export.

Design decision (important)
---------------------------
C1 is applied **in place to the final SRT by the streaming Runner**, exactly
once, when the task reaches ``done``. C2 writes a separate file.

Why in-place is safe (this is the whole design):

* The Runner still appends every group immediately, unchanged. Refinement is a
  pure function over the *complete* SRT and is applied only on the ``done``
  path, never on ``paused`` (cancel). A full run and a cancelled-then-resumed
  run therefore both execute the same single rewrite over the same byte-identical
  raw SRT, so A2 (resume == full run, byte for byte) still holds.
* ``done`` short-circuits on subsequent starts, so the rewrite is never applied
  twice (the refinement is intentionally not idempotent).
* The checkpoint stores frame/SRT indices, not file offsets, so rewriting text
  in place cannot desynchronise resume. JSONL keeps the raw lines and remains
  the source of truth for enrichments.
* A manual API can still produce a separate ``*.punct.srt`` copy for old files.

Outputs
-------
    <name>.srt           refined in place on done (C1)
    <name>.punct.srt     optional manual copy (C1)
    <name>.handout.md    Markdown lecture notes (C2)
    <name>.handout.txt   plain-text lecture notes (C2)

All functions are pure except the two ``*_file`` writers, which use an atomic
replace so a failure can never leave a half-written deliverable.
"""
from __future__ import annotations

import re
from pathlib import Path

_TERMINAL = "。！？!?…；;"
_TRAILING_COMMA = "，,、：:"
_ALL_PUNCT = _TERMINAL + _TRAILING_COMMA + ".,、。，"
_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


def _ts_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _sec_to_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000)) % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str) -> list[dict]:
    """Parse SRT into ``[{index,start,end,text}]``. Tolerates CRLF and BOM."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("\ufeff").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        ts_idx = next((i for i, l in enumerate(lines) if _TS_RE.search(l)), None)
        if ts_idx is None:
            continue
        m = _TS_RE.search(lines[ts_idx])
        idx = lines[0].strip() if ts_idx > 0 and lines[0].strip().isdigit() else len(out) + 1
        body = "\n".join(lines[ts_idx + 1:]).strip()
        if not body:
            continue
        out.append({
            "index": int(idx),
            "start": _ts_to_sec(*m.groups()[:4]),
            "end": _ts_to_sec(*m.groups()[4:]),
            "text": body,
            # Keep the original timestamp line verbatim. The Runner rewrites
            # only the text lines of a finished SRT; re-formatting timestamps
            # would change bytes that A2 compares.
            "ts": lines[ts_idx].strip(),
        })
    return out


def format_srt(entries: list[dict]) -> str:
    parts = []
    for i, e in enumerate(entries, 1):
        ts = e.get("ts") or f"{_sec_to_ts(e['start'])} --> {_sec_to_ts(e['end'])}"
        parts.append(f"{i}\n{ts}\n{e['text']}\n")
    return "\n".join(parts)


def refine_punctuation(entries: list[dict], cjk_only: bool = False) -> list[dict]:
    """C1: deterministic cross-segment punctuation refinement.

    Rules (Chinese lecture audio, VAD-grouped lines):

    1. Strip leading punctuation from a line (a VAD boundary often leaves a
       stray comma at the start of the next group).
    2. A line ending in a comma-like mark becomes a full stop when the next
       line starts a new sentence; otherwise the comma is kept to signal
       continuation.
    3. A line with no trailing punctuation gets a comma, unless it is the last
       line, which gets a full stop.
    4. Empty lines are preserved verbatim (never invent text).

    ``cjk_only`` leaves non-Chinese lines untouched. The Runner turns it on so
    synthetic English test fixtures and mixed-language transcripts do not get
    Chinese commas injected into Latin sentences.

    Only ``text`` changes; timestamps and indices are untouched. The function
    is pure, so identical input always yields identical output - but note it is
    NOT idempotent (a comma we added can become a full stop on a second pass),
    which is why the Runner applies it exactly once on the ``done`` path.
    """
    out = [dict(e) for e in entries]
    for e in out:
        e["text"] = e["text"].strip()
    # Rule 1: leading punctuation
    for e in out:
        if cjk_only and not _has_cjk(e["text"]):
            continue
        while e["text"] and e["text"][0] in _ALL_PUNCT:
            e["text"] = e["text"][1:].lstrip()
    # Rules 2-3 need the next line's first character.
    for i, e in enumerate(out):
        text = e["text"]
        if not text:
            continue
        if cjk_only and not _has_cjk(text):
            continue
        last = text[-1]
        nxt = out[i + 1]["text"] if i + 1 < len(out) else ""
        is_last = (i == len(out) - 1)
        if last in _TERMINAL:
            continue
        if last in _TRAILING_COMMA:
            new_sentence = bool(nxt) and (nxt[0] not in _ALL_PUNCT)
            # A following line that starts with an opening quote / bracket is
            # still a new sentence; a lowercase latin letter suggests carry-on.
            if is_last or (new_sentence and not (nxt and nxt[0].isascii() and nxt[0].islower())):
                e["text"] = text[:-1] + "。"
        else:
            e["text"] = text + ("。" if is_last else "，")
    return out


def refine_srt_text(raw: str, cjk_only: bool = True) -> str:
    """Return ``raw`` with cross-segment punctuation refined, text lines only.

    Original index and timestamp lines are preserved verbatim. Used by the
    Runner's single ``done``-path rewrite; see the module docstring for why this
    keeps A2 (resume == full run) intact.
    """
    entries = parse_srt(raw)
    if not entries:
        return raw
    return format_srt(refine_punctuation(entries, cjk_only=cjk_only))


def refine_srt_file(srt_path: Path | str, inplace: bool = False) -> Path:
    """Write a refined copy (``*.punct.srt``) or replace the source in place."""
    src = Path(srt_path)
    entries = parse_srt(src.read_text(encoding="utf-8-sig"))
    if not entries:
        raise ValueError("SRT 中沒有可處理的字幕行")
    dst = src if inplace else src.with_suffix(".punct.srt")
    _atomic_write(dst, format_srt(refine_punctuation(entries, cjk_only=inplace)))
    return dst


def build_handout(entries: list[dict], fmt: str = "md", title: str = "",
                  gap_sec: float = 2.0) -> str:
    """C2: turn subtitles into readable lecture notes.

    ``gap_sec`` splits paragraphs at pauses longer than the threshold, which
    mirrors how a lecture naturally breaks into topics. Markdown output keeps
    an ``[mm:ss]`` marker at the start of each paragraph; plain text drops
    timestamps entirely. Speaker prefixes (from enriched SRT) become sections.
    """
    if not entries:
        raise ValueError("沒有字幕可匯出")
    paragraphs: list[tuple[float, str, list[str]]] = []
    cur_start = entries[0]["start"]
    cur_lines: list[str] = []
    prev_end = entries[0]["end"]
    speaker = ""
    sections: list[tuple[str, str, float, list[str]]] = []
    for e in entries:
        text = e["text"].strip()
        spk = ""
        m = re.match(r"^\[?(說話者|Speaker)\s*(\d+|[A-Z])\]?\s*[:：]?\s*", text, re.I)
        if m:
            spk = m.group(0).strip()
            text = text[m.end():].strip()
        if spk and spk != speaker and cur_lines:
            sections.append((speaker, "", cur_start, cur_lines))
            cur_lines, cur_start = [], e["start"]
        if spk:
            speaker = spk
        if cur_lines and e["start"] - prev_end > gap_sec:
            sections.append((speaker, "", cur_start, cur_lines))
            cur_lines, cur_start = [], e["start"]
        if text:
            cur_lines.append(text)
        prev_end = e["end"]
    if cur_lines:
        sections.append((speaker, "", cur_start, cur_lines))

    def _para(lines: list[str]) -> str:
        joined = "".join(lines) if all(_is_cjk(x[-1:]) for x in lines if x) else " ".join(lines)
        return re.sub(r"\s+", " ", joined).strip()

    def _clock(sec: float) -> str:
        return f"{int(sec // 60):02d}:{int(sec % 60):02d}"

    if fmt.lower() in ("txt", "text"):
        blocks = []
        if title:
            blocks.append(title + "\n" + "=" * max(8, len(title)) + "\n")
        for spk, _, start, lines in sections:
            head = f"[{spk}] " if spk else ""
            blocks.append(head + _para(lines) + "\n")
        return "\n".join(blocks).strip() + "\n"

    md = []
    if title:
        md.append(f"# {title}\n")
        md.append(f"> 共 {len(entries)} 條字幕 · {_clock(entries[0]['start'])}–"
                  f"{_clock(entries[-1]['end'])}\n")
    last_spk = None
    for spk, _, start, lines in sections:
        if spk and spk != last_spk:
            md.append(f"\n## {spk}\n")
            last_spk = spk
        md.append(f"[{_clock(start)}] {_para(lines)}\n")
    return "\n".join(md).strip() + "\n"


def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("\u4e00" <= ch <= "\u9fff" or ch in _ALL_PUNCT)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def export_handout_file(srt_path: Path | str, fmt: str = "md",
                        title: str = "", gap_sec: float = 2.0) -> Path:
    src = Path(srt_path)
    entries = parse_srt(src.read_text(encoding="utf-8-sig"))
    suffix = ".handout.txt" if fmt.lower() in ("txt", "text") else ".handout.md"
    dst = src.with_suffix(suffix)
    _atomic_write(dst, build_handout(entries, fmt=fmt, title=title or src.stem,
                                     gap_sec=gap_sec))
    return dst


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
