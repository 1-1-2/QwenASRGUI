"""subtitle_extras.py - C1 cross-segment punctuation and C2 handout export.

Design decision (important)
---------------------------
Both features operate on a **completed SRT and write a separate file**. They
never touch the streaming Runner, the checkpoint, or the JSONL. Reasons:

* The streaming path writes SRT line-by-line and A2 guarantees resume is
  byte-identical to a full run. Any per-group "look back and edit the previous
  line" would break that contract (and would need to be replayed from JSONL on
  resume).
* The project decision for optional enrichments is explicit: "do not drop the
  feature, chain it in cheaply, failure must not affect the SRT". A separate
  output file is the cheapest safe chain.

Outputs
-------
    <name>.punct.srt     refined punctuation (C1)
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
        })
    return out


def format_srt(entries: list[dict]) -> str:
    parts = []
    for i, e in enumerate(entries, 1):
        parts.append(f"{i}\n{_sec_to_ts(e['start'])} --> {_sec_to_ts(e['end'])}\n{e['text']}\n")
    return "\n".join(parts)


def refine_punctuation(entries: list[dict]) -> list[dict]:
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

    Only ``text`` changes; timestamps and indices are untouched. The function
    is pure, so identical input always yields identical output - which is why
    this can be re-run safely on any SRT.
    """
    out = [dict(e) for e in entries]
    for e in out:
        e["text"] = e["text"].strip()
    # Rule 1: leading punctuation
    for e in out:
        while e["text"] and e["text"][0] in _ALL_PUNCT:
            e["text"] = e["text"][1:].lstrip()
    # Rules 2-3 need the next line's first character.
    for i, e in enumerate(out):
        text = e["text"]
        if not text:
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


def refine_srt_file(srt_path: Path | str) -> Path:
    """Write ``<stem>.punct.srt`` next to the source; atomic replace."""
    src = Path(srt_path)
    entries = parse_srt(src.read_text(encoding="utf-8-sig"))
    if not entries:
        raise ValueError("SRT 中沒有可處理的字幕行")
    dst = src.with_suffix(".punct.srt")
    _atomic_write(dst, format_srt(refine_punctuation(entries)))
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
