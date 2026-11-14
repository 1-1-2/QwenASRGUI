"""單行版面 + 時間軸契約測試（一條一行、≤20 字、絕不重疊）。

三條硬契約：

1. **一條一行**：SRT 的 text 裡永遠不出現 ``\\n``。多行條目在播放器/剪輯
   軟體裡支援不一致，導出後常被壓成一行而溢出。
2. **不產生短尾巴**：長句按 ``ceil(字數/20)`` 均分，21 字切成 10+11
   而不是舊版的 20+1。條數不變，只是把尾巴攤回前面。
3. **時間不重疊**：``fix_timeline`` 保證 ``start[i] >= end[i-1] + gap``，
   且優先壓前一條的 end（起始時間不動）。

Run directly, no pytest:
    python tests/test_layout_single_line.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from subtitle_lines import (  # noqa: E402
    MAX_CHARS, _ts_chatllm_to_subtitle_lines, balance_lines, fix_timeline,
)

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


def _mk(text, step=0.24, dur=0.20):
    """逐字時間軸；標點不佔時間（與真實 FA 一致）。"""
    items, t = [], 0.0
    for ch in text:
        if ch in "，。、；：？！":
            continue
        items.append((ch, t, t + dur))
        t += step
    return items


def _lines(text, **kw):
    return _ts_chatllm_to_subtitle_lines(
        _mk(text), text, 0.0, None, None, True, False, True, **kw)


NO_PUNCT = ("第10条就是我刚刚可能刚进来的时候没有那么大声说话"
            "我们复习巩固巩固一下前面的内容然后继续往下讲新的知识点大家注意听")
WITH_PUNCT = ("好，我们今天就来讲一下这个申论的第一道题。第一道题呢，"
              "它考的是归纳概括，也就是说，你要从材料里面把要点提炼出来，"
              "然后分条作答，注意字数限制。")

print("[1] 單行契約（核心：SRT 裡不准有換行）")
lines = _lines(NO_PUNCT)
check("产出条目", len(lines) > 0, f"{len(lines)} 条")
check("絕無換行", all("\n" not in l[2] for l in lines),
      str([l[2] for l in lines if "\n" in l[2]][:2]))
check("每條 ≤20 字", all(len(l[2]) <= MAX_CHARS for l in lines),
      str([len(l[2]) for l in lines]))
check("字数守恒",
      sum(len(l[2]) for l in lines) == len(NO_PUNCT),
      f"{sum(len(l[2]) for l in lines)} vs {len(NO_PUNCT)}")

print("\n[2] 均分：不產生短尾巴")
# 21 字 → 必須是 2 條且兩條都夠長（舊版會切 20 + 1）
t21 = "一二三四五六七八九十一二三四五六七八九十一"
b21 = balance_lines([(0.0, 4.0, t21, None)])
check("21 字切 2 條", len(b21) == 2, f"{len(b21)} 條 {[len(x[2]) for x in b21]}")
check("21 字不出現 1 字尾巴",
      all(len(x[2]) >= 8 for x in b21), str([len(x[2]) for x in b21]))
check("21 字兩條長度差 ≤1",
      max(len(x[2]) for x in b21) - min(len(x[2]) for x in b21) <= 1,
      str([len(x[2]) for x in b21]))

t41 = "一二三四五六七八九十" * 4 + "一"
b41 = balance_lines([(0.0, 8.0, t41, None)])
check("41 字切 3 條", len(b41) == 3, str([len(x[2]) for x in b41]))
check("41 字長度差 ≤1",
      max(len(x[2]) for x in b41) - min(len(x[2]) for x in b41) <= 1,
      str([len(x[2]) for x in b41]))
check("41 字每條 ≤20", all(len(x[2]) <= MAX_CHARS for x in b41))

print("\n[3] 有 words 時按詞時間切、時間不重疊")
rows = _lines(NO_PUNCT)
ok_time = all(rows[i][0] >= rows[i - 1][1] - 1e-6 for i in range(1, len(rows)))
check("條目時間單調不重疊", ok_time,
      str([(round(rows[i - 1][1], 2), round(rows[i][0], 2))
           for i in range(1, len(rows)) if rows[i][0] < rows[i - 1][1]][:3]))
check("words 逐條對得上字數",
      all(sum(len(w.get("text", "")) for w in (l[4] or [])) == len(l[2])
          for l in rows))

print("\n[4] 兼容 4/5/6-tuple 與冪等")
four = [(0.0, 1.0, "一二三四五六七八九十一二三四五", None),
        (1.0, 2.0, "六七八九十一二三四五六七八", None)]
p4 = balance_lines(four)
check("4-tuple 輸入回 4-tuple", all(len(x) == 4 for x in p4))
check("4-tuple 也單行且 ≤20",
      all("\n" not in x[2] and len(x[2]) <= MAX_CHARS for x in p4))
six = [(0.0, 1.0, "一二三四五六七八九十一二三四五", None, [], True)]
check("6-tuple 保留 hard 欄位", len(balance_lines(six)[0]) == 6)
check("balance 冪等",
      [x[2] for x in balance_lines(lines)] == [x[2] for x in lines])
check("短句不動", balance_lines([(0.0, 1.0, "短句", None)])[0][2] == "短句")

print("\n[5] 短尾巴回收 merge_tails")
from subtitle_lines import merge_tails  # noqa: E402

# 間隙小 → 尾巴併回前一條（「說｜話」是同一句話被 20 字硬切）
tail = [(0.0, 3.3, "就是我刚刚可能刚进来的时候没有那么大声说", None),
        (3.38, 3.98, "话", None),
        (4.06, 4.9, "怕吓到大家", None)]
mt = merge_tails(tail)
check("小間隙尾巴併回前條", mt[0][2].endswith("说话"), str(mt[0][2]))
check("合併後條數減少", len(mt) == 2, str([x[2] for x in mt]))
check("合併後無 1 字條目", all(len(x[2]) > 1 for x in mt))

# 間隙大 → 單字應答詞併入下一條（併前面會讀成反問）
gap = [(0.0, 1.5, "应该是声音是可以的", None),
       (3.5, 4.1, "行", None),
       (4.18, 5.7, "如果声音没问题的话", None)]
mg = merge_tails(gap)
check("大間隙單字併入後條", mg[1][2].startswith("行"), str([x[2] for x in mg]))
check("大間隙仍無 1 字條目", all(len(x[2]) > 1 for x in mg))

# 合併不得超過 soft_max
big = [(0.0, 2.0, "一" * 24, None), (2.05, 2.5, "二", None)]
mb = merge_tails(big)
check("超 soft_max 不合併", len(mb) == 2, str([len(x[2]) for x in mb]))

print("\n[6] 時間軸規整 fix_timeline")
# 跨 group 的典型重疊：第二段從第一段內部開始
overlap = [(0.0, 2.0, "A", None), (1.6, 3.0, "B", None), (2.9, 4.0, "C", None)]
fx = fix_timeline(overlap)
check("負間隙歸零",
      all(fx[i][0] >= fx[i - 1][1] for i in range(1, len(fx))),
      str([(fx[i - 1][1], fx[i][0]) for i in range(1, len(fx))]))
check("起始時間優先（B 的 start 不動）", abs(fx[1][0] - 1.6) < 1e-9,
      f"{fx[1][0]}")
check("前一條被壓短而非消失", fx[0][1] < 2.0 and fx[0][1] > fx[0][0],
      f"{fx[0]}")
check("欄位數不變", all(len(x) == 4 for x in fx))

# 前一條本來就很短 → 壓不動，只能推後
tight = [(0.0, 0.2, "A", None), (0.1, 1.0, "B", None)]
ft = fix_timeline(tight, min_dur=0.30)
check("壓不動時推後 start", ft[1][0] >= ft[0][1], str(ft))

# end <= start 的壞行（ASR 分段錯序）
bad = [(5.0, 4.0, "X", None)]
fb = fix_timeline(bad)
check("壞行修成可見長度", fb[0][1] > fb[0][0], str(fb))

# 組合：balance + fix 之後仍然是單行且不重疊
combo = fix_timeline(balance_lines(_lines(NO_PUNCT + WITH_PUNCT)))
check("組合後仍單行", all("\n" not in x[2] for x in combo))
check("組合後不重疊",
      all(combo[i][0] >= combo[i - 1][1] - 1e-6 for i in range(1, len(combo))))

print(f"\n结果：{PASS}/{PASS + FAIL} 项通过")
sys.exit(1 if FAIL else 0)
