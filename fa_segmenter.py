"""fa_segmenter.py — 用 ForcedAligner 字级时间戳决定字幕行边界（可选增强）

为什么需要这个模块
==================
Qwen3-ASR 会在文本里输出真实标点（实测 20s 片段 11 个），但旧的
``_split_to_lines`` 把标点当切行信号并丢弃，行内因此一个标点都不剩；
而字幕行的时间轴又由 ``_assign_ts`` 按字数比例硬分配。结果是：

  · 行边界来自「哪里有标点」，与声音无关；
  · 行时间来自「字数占比」，与声音无关；
  · 8186 行样本里 8.5% 有负间隔（重叠），最深 2.11s。

ForcedAligner（0.6B, NAR）给出每个字/词的毫秒时间戳。有了它，行边界可以
同时用三条**声学**信号决定，时间轴直接取首字 start / 末字 end：

  1. 强标点（。？！；）→ 立即断行
  2. 弱标点（，、：…—）且当前累积 >= ``weak_min`` 字 → 断行
  3. 字间间隔 >= ``gap_break`` 秒 → 强制断行（**这是标点给不出的信息**）

设计约束
--------
* **不改变默认行为**：只有显式 ``enabled=True`` 且 FA 模型可用时才接管，
  否则调用方继续走 ``_split_to_lines`` + ``_assign_ts``，输出逐字节不变。
* **失败必须降级**：FA 缺失 / 子进程崩溃 / 输出无法解析 → 返回 ``None``，
  调用方回退到旧路径。字幕永远不能因为增强失败而丢失。
* **无新依赖**：纯 Python，时间戳来自已有的 ``ChatLLMAligner.align_chunk``。

实测（30s 真实音频，235~265s 段）
--------------------------------
=================  ======  ======  ======  ======
方案               行数    均字数  ≤3字    重叠
=================  ======  ======  ======  ======
现状（比例分配）    16      9.4     4       1
FA 切行             10      13.5    1       0
=================  ======  ======  ======  ======
"""
from __future__ import annotations

import re

# 句末标点：见到就断。依据是「这里是不是一句话的终点」，不是标点种类多少。
STRONG_PUNCT = frozenset("。？！；!?;")
# 句内停顿：只在累积够长时断，否则丢弃继续累积（避免「呃，」「对吧，」碎片行）
WEAK_PUNCT = frozenset("，、：,…—·")
ALL_PUNCT = STRONG_PUNCT | WEAK_PUNCT | frozenset(". ")

# 默认参数（实测较优，见模块文档的对比表）
WEAK_MIN_CHARS = 12      # 弱标点断行所需的最短累积字数
MAX_CHARS = 20           # 单行上限（与 subtitle_lines.MAX_CHARS 一致）
MAX_OVERFLOW = 2         # 为保住完整词，允许临时超出 MAX_CHARS 的字数
GAP_BREAK = 0.35         # 字间间隔超过此值强制断行（秒）
GAP_MERGE = 0.20         # 相邻行间隔小于此值可合并（秒）
MIN_GAP = 0.08           # 相邻行之间的最小留白（秒）


def _expand(items):
    """把 (word, start, end) 展开成逐字，同一词内各字沿用词的时间段。

    FA 对中文通常逐字返回，但英文会整词返回；展开后与 raw_text 的字符
    序列一一对应，避免中英混排时错位。
    """
    out = []
    for w, s, e in items:
        n = len(w)
        if n <= 0:
            continue
        if n == 1:
            out.append((w, s, e))
        else:
            # 词内均分，保证单调且不越界
            step = (e - s) / n
            for k in range(n):
                out.append((w[k], s + step * k, s + step * (k + 1)))
    return out


def build_lines(items, raw_text: str, **kw):
    """字级时间戳 + 原文 → 字幕行 ``[(start, end, text), ...]``。

    薄封装：切行逻辑只有一份，在 ``build_lines_marked``（它多留一个
    hard_cut 标记给后续合并判断）。旧版把同一套逻辑抄了两份，改规则时
    很容易只改到一半 —— 两份只在“是否保留标记”上不同。

    参数与 ``build_lines_marked`` 完全一致（weak_min / max_chars /
    gap_break / offset / word_bounds），返回 None 表示输入不可用。
    """
    rows = build_lines_marked(items, raw_text, **kw)
    return [(s, e, t) for s, e, t, _hard in rows] if rows else None


def build_lines_marked(
    items,
    raw_text: str,
    **kwargs,
):
    """同 ``build_lines``，但保留 hard_cut 标记（供合并判断用）。"""
    max_chars = kwargs.get("max_chars", MAX_CHARS)
    weak_min = kwargs.get("weak_min", WEAK_MIN_CHARS)
    gap_break = kwargs.get("gap_break", GAP_BREAK)
    offset = kwargs.get("offset", 0.0)
    word_bounds = kwargs.get("word_bounds")

    if not items or not raw_text or not raw_text.strip():
        return None

    seq = [c for c in raw_text if not c.isspace()]
    expanded = _expand(items)
    consumed = 0
    rows: list[list] = []
    buf: list[str] = []
    start: float | None = None
    end: float = 0.0
    last_end: float | None = None
    ci = 0

    def emit(is_hard: bool = False) -> None:
        nonlocal buf, start, end, last_end
        if buf and start is not None:
            text = "".join(buf).strip()
            if text:
                rows.append([start + offset,
                             max(end, start + 1e-3) + offset, text, is_hard])
        buf, start, end, last_end = [], None, 0.0, None

    def hard_emit() -> None:
        nonlocal buf, start, end, last_end, consumed
        cut = len(buf)
        if word_bounds:
            local = {k - consumed for k in word_bounds}
            n = len(buf)
            after = [k for k in local if n <= k <= n + MAX_OVERFLOW]
            before = [k for k in local if k <= max_chars]
            if after:
                cut = min(after)
            elif before:
                cut = max(before)
        if 0 < cut < len(buf):
            head, tail = buf[:cut], buf[cut:]
            text = "".join(head).strip()
            if text:
                rows.append([start + offset, last_end + offset, text, True])
            consumed += cut
            buf = tail
            start = last_end
            end = last_end
        else:
            emit(True)

    for idx, ch in enumerate(seq):
        if ch in ALL_PUNCT:
            if ch in STRONG_PUNCT:
                emit()
            elif len(buf) >= weak_min:
                emit()
            continue
        if ci < len(expanded):
            _c, s, e = expanded[ci]
            ci += 1
        else:
            s = e = last_end if last_end is not None else 0.0
        if last_end is not None and (s - last_end) >= gap_break:
            emit()
        if start is None:
            start = s
        buf.append(ch)
        end = max(end, e) if end else e
        last_end = e
        if len(buf) >= max_chars:
            ahead = seq[idx + 1: idx + 1 + MAX_OVERFLOW + 1]
            if len(buf) >= max_chars + MAX_OVERFLOW:
                hard_emit()
            elif not any(c in ALL_PUNCT for c in ahead):
                hard_emit()
    emit()
    return rows or None


def merge_hard_cuts(rows, *, max_chars: int = MAX_CHARS):
    """合并「被硬切开」的相邻行，时间取外沿。

    只合并标了 ``hard_cut`` 的那一行——否则会把「右」这类跨句碎片
    并进下一句（实测反例：「右因为前面这个哲学呢…」）。
    """
    if not rows:
        return rows
    out = []
    for r in rows:
        if out and out[-1][3] and len(out[-1][2]) + len(r[2]) <= max_chars:
            out[-1][1] = r[1]
            out[-1][2] += r[2]
            out[-1][3] = r[3]
            continue
        out.append(list(r))
    return out


def merge_by_gap(lines, *, gap: float = GAP_MERGE, max_chars: int = MAX_CHARS):
    """合并「间隔极小」的相邻行（同一句被切开的情况）。

    只在合并后不超长时进行，时间取外沿。没有 FA 的旧路径做不到这件事——
    那里的行间间隔恒为 ``GAP_SEC=0.08``，无法区分「同一句被切开」与
    「两句之间的自然停顿」。
    """
    if not lines:
        return lines
    out = []
    for s, e, t in lines:
        if out:
            ps, pe, pt = out[-1]
            if (s - pe) < gap and len(pt) + len(t) <= max_chars:
                out[-1] = (ps, e, pt + t)
                continue
        out.append((s, e, t))
    return out


def normalize_gaps(lines, *, min_gap: float = 0.08):
    """给相邻行留出最小间隔，避免「上一行 end == 下一行 start」的瞬切。

    FA 的逐字时间戳是紧密相接的（一个字结束即下一个字开始），直接当行边界
    会出现大量 ``end[i] == start[i+1]``。播放器上表现为字幕无缝跳变、看不清。
    这里从**前一行**尾部让出 ``min_gap``（不移动 start，避免连锁推移），
    并保证每行至少保留 0.12s，否则干脆不留白。
    """
    if not lines or len(lines) < 2:
        return lines
    out = [list(x) for x in lines]
    for i in range(len(out) - 1):
        s, e, t = out[i]
        nxt_start = out[i + 1][0]
        if e + min_gap > nxt_start:
            new_e = nxt_start - min_gap
            if new_e - s >= 0.12:          # 留白后仍够看，才让
                out[i][1] = new_e
    return [tuple(x) for x in out]


def align_to_lines(
    items,
    raw_text: str,
    *,
    offset: float = 0.0,
    merge: bool = True,
    weak_min: int = WEAK_MIN_CHARS,
    max_chars: int = MAX_CHARS,
    gap_break: float = GAP_BREAK,
    gap_merge: float = GAP_MERGE,
    word_bounds: set[int] | None = None,
):
    """一步到位：FA 字级时间戳 → 最终字幕行。

    流程：
      1. 三条信号切行（标点强度 + 声学间隔 + 上限前瞻），保留 hard_cut 标记
      2. 只合并「被硬切开」的相邻行（不跨句乱并）
      3. 强制留出最小间隔，避免 ``end[i] == start[i+1]`` 的字幕瞬切

    ``word_bounds`` 非空时，硬切会优先切在词边界（不劈词）；默认 None。

    返回 ``None`` 时调用方必须回退旧路径。
    """
    rows = build_lines_marked(items, raw_text, weak_min=weak_min,
                              max_chars=max_chars, gap_break=gap_break,
                              offset=offset, word_bounds=word_bounds)
    if not rows:
        return None
    if merge:
        rows = merge_hard_cuts(rows, max_chars=max_chars)
    lines = [(r[0], r[1], r[2]) for r in rows]
    lines = normalize_gaps(lines)
    return lines