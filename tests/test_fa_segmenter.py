"""fa_segmenter 单测：三条硬性质 + 降级行为。

  1. 单调不重叠：start[i] >= end[i-1]（除非允许重叠，本实现不允许）
  2. 不越界：0 <= start < end <= 音频时长
  3. 降级：items 为空 / 原文为空 → 返回 None（绝不抛异常）
"""
import sys

sys.path.insert(0, r"C:\Green\QwenASR-WebView.dev\01_repo")
from fa_segmenter import (build_lines, merge_by_gap, align_to_lines,  # noqa: E402
                         STRONG_PUNCT)

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" - " + str(extra) if extra else ""))


def mono_ok(lines):
    prev = None
    for s, e, _t in lines:
        if e <= s:
            return False, f"end<=start {s:.3f}->{e:.3f}"
        if prev is not None and s < prev - 1e-9:
            return False, f"重叠 {prev - s:.3f}s"
        prev = e
    return True, ""


def main():
    # ── [1] 基础：标点切行 ──
    print("[1] 标点切行")
    items = [(c, i * 0.2, i * 0.2 + 0.18) for i, c in
             enumerate("點之間的邏輯然後特別麻煩浪費時間我還沒有時間複習")]
    raw = "點之間的邏輯，然後特別麻煩，浪費時間，我還沒有時間複習"
    lines = build_lines(items, raw)
    ok, msg = mono_ok(lines)
    check("单调不重叠", ok, msg)
    check("行数合理", lines and 2 <= len(lines) <= 5, f"{len(lines)} 行")
    check("标点不进字幕", all(not any(c in STRONG_PUNCT for c in t) for _s, _e, t in lines))

    # ── [2] 声学间隔强制断行（标点给不出的信号）──
    print("\n[2] 声学间隔强制断行")
    # 前 6 字连续，然后静默 1.5s，再 6 字；中间无标点
    chars = "前面六个字在这里后面六个字"
    times = []
    t = 0.0
    for i, c in enumerate(chars):
        if i == 6:
            t += 1.5           # 长静默
        times.append((c, t, t + 0.18))
        t += 0.18
    lines2 = build_lines(times, chars, gap_break=0.35)
    ok, msg = mono_ok(lines2)
    check("无标点也能按静默断行", len(lines2) == 2, f"{len(lines2)} 行")
    check("断点落在静默处", lines2 and lines2[0][2] == chars[:6],
          str([l[2] for l in lines2]))
    check("单调不重叠", ok, msg)

    # ── [3] 弱标点够长才切 ──
    print("\n[3] 弱标点够长才切")
    items3 = [(c, i * 0.15, i * 0.15 + 0.13) for i, c in
              enumerate("呃大家晚上好哈我先提前进来一下")]
    raw3 = "呃，大家晚上好哈，我先提前进来一下"
    lines3 = build_lines(items3, raw3, weak_min=12)
    check("短行不因逗号被切碎", len(lines3) == 1, f"{len(lines3)} 行")

    # ── [4] 降级 ──
    print("\n[4] 降级行为")
    check("空 items → None", build_lines([], "有文字") is None)
    check("空原文 → None", build_lines([("字", 0, 0.2)], "   ") is None)
    check("align_to_lines 空 → None", align_to_lines([], "文字") is None)

    # ── [5] 合并 ──
    print("\n[5] 按间隔合并")
    seg = [(0.0, 1.0, "前半句"), (1.05, 2.0, "后半句"), (3.0, 4.0, "第三句")]
    merged = merge_by_gap(seg, gap=0.2, max_chars=20)
    check("小间隔合并", len(merged) == 2, f"{len(merged)} 行")
    check("合并后时间取外沿", merged[0][0] == 0.0 and merged[0][1] == 2.0,
          str(merged[0]))
    over = merge_by_gap([(0, 1, "很长的一行字" * 3), (1.05, 2, "再来一行字")],
                        gap=0.2, max_chars=20)
    check("超长不合并", len(over) == 2, f"{len(over)} 行")

    # ── [6] 偏移 ──
    print("\n[6] offset")
    lines6 = build_lines([("字", 0.0, 0.5), ("幕", 0.5, 1.0)], "字幕", offset=100.0)
    check("offset 生效", abs(lines6[0][0] - 100.0) < 1e-9, str(lines6[0]))

    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"\nResult: {ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())