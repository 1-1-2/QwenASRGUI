"""顯示窗口契約測試：字幕**永遠不晚於**首字發聲。

背景（實測 003 前 120 秒，54 條，FA 毫秒級真值）：
    字幕出現比首字發聲晚中位 0.43 秒，69% 的條目遲到超過 0.1 秒，
    最嚴重一條遲到 6.9 秒。根因是行的 start 本來就是按字數比例估的。

``apply_display_lead`` 的三條規則與它們的不變量：

1. ``appear[i] <= onset[i]``          —— 絕不遲到（硬契約）
2. ``appear[i] >= off[i-1]``          —— 絕不與上一條重疊（硬契約）
3. ``vanish[i] >= off[i]``            —— 話沒說完不能撤（軟契約，衝突時讓位）

Run directly, no pytest:
    python tests/test_display_lead.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from subtitle_lines import (  # noqa: E402
    HOLD_SEC, LEAD_SEC, MIN_ONSCREEN, apply_display_lead,
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


def rows(*pairs):
    """(onset, offset, text) → 4-tuple 行格式。"""
    return [(s, e, t, None) for (s, e, t) in pairs]


def _lag(src, out):
    return [round(out[i][0] - src[i][0], 6) for i in range(len(src))]


def _overlaps(out):
    return [(i, round(out[i][0] - out[i - 1][1], 6))
            for i in range(1, len(out)) if out[i][0] - out[i - 1][1] < -1e-9]


print("[1] 單條：提前一個完整 LEAD_SEC")
src = rows((10.00, 12.00, "大家好"))
out = apply_display_lead(src)
check("首條提前 LEAD_SEC", abs(out[0][0] - (10.00 - LEAD_SEC)) < 1e-9,
      f"{out[0][0]:.3f}")
check("尾留 HOLD_SEC", abs(out[0][1] - (12.00 + HOLD_SEC)) < 1e-9,
      f"{out[0][1]:.3f}")
check("文字原樣", out[0][2] == "大家好")

print("\n[2] 連續說話（gap = 0）：與首字同幀切換，不重疊")
src = rows((10.00, 12.00, "前一條"), (12.00, 14.00, "後一條"))
out = apply_display_lead(src)
check("第二條不遲到", out[1][0] <= 12.00 + 1e-9, f"{out[1][0]:.3f}")
check("第二條無法提前（gap=0）", abs(out[1][0] - 12.00) < 1e-9, f"{out[1][0]:.3f}")
check("第一條被截短讓位", abs(out[0][1] - 12.00) < 1e-9, f"{out[0][1]:.3f}")
check("無重疊", not _overlaps(out), str(_overlaps(out)))

print("\n[3] 有停頓：提前量從靜音裡擠，不超過停頓的一半")
src = rows((10.00, 12.00, "前一條"), (12.10, 14.00, "後一條"))
out = apply_display_lead(src)
lead2 = 12.10 - out[1][0]
check("提前量被停頓限制", 0 < lead2 <= 0.10 + 1e-9, f"{lead2:.3f}")
check("仍不重疊", not _overlaps(out), str(_overlaps(out)))

print("\n[4] 大停頓：用滿 LEAD_SEC")
src = rows((10.00, 12.00, "前一條"), (13.00, 15.00, "後一條"))
out = apply_display_lead(src)
check("提前量 = LEAD_SEC", abs((13.00 - out[1][0]) - LEAD_SEC) < 1e-9,
      f"{13.00 - out[1][0]:.3f}")
check("前一條保留完整尾留", abs(out[0][1] - (12.00 + HOLD_SEC)) < 1e-9,
      f"{out[0][1]:.3f}")

print("\n[5] 首條逼近 0：不能出現負時間")
src = rows((0.05, 1.00, "開頭"))
out = apply_display_lead(src)
check("start >= 0", out[0][0] >= 0.0, f"{out[0][0]:.3f}")
check("仍不遲到", out[0][0] <= 0.05 + 1e-9)

print("\n[6] 極短條目：至少可見 MIN_ONSCREEN 或到下一條為止")
src = rows((10.00, 10.02, "嗯"), (10.05, 11.00, "下一句"))
out = apply_display_lead(src)
check("不為負長度", out[0][1] > out[0][0], f"{out[0][0]:.3f}-{out[0][1]:.3f}")
check("不重疊", not _overlaps(out), str(_overlaps(out)))

print("\n[7] 不變量 fuzz：500 組隨機時間軸")
random.seed(20261113)
bad_late = bad_overlap = bad_order = 0
min_lag, max_lag = 9e9, -9e9
for _ in range(500):
    n = random.randint(1, 40)
    t = random.uniform(0, 5)
    src = []
    for _i in range(n):
        dur = random.uniform(0.4, 4.0)
        gap = random.choice([0.0, 0.0, 0.05, 0.2, 0.5, 1.2])
        src.append((t, t + dur, "字" * random.randint(1, 20), None))
        t += dur + gap
    out = apply_display_lead(src)
    for i in range(n):
        if out[i][0] > src[i][0] + 1e-9:
            bad_late += 1
        lag = out[i][0] - src[i][0]
        min_lag, max_lag = min(min_lag, lag), max(max_lag, lag)
        if out[i][1] <= out[i][0]:
            bad_order += 1
    if _overlaps(out):
        bad_overlap += 1
check("零遲到", bad_late == 0, f"{bad_late} 處")
check("零重疊", bad_overlap == 0, f"{bad_overlap} 組")
check("零非正長度", bad_order == 0, f"{bad_order} 條")
check("提前量落在 [-LEAD_SEC, 0]", min_lag >= -LEAD_SEC - 1e-9 and max_lag <= 1e-9,
      f"[{min_lag:+.3f}, {max_lag:+.3f}]")

print("\n[8] 欄位兼容：4/5/6-tuple 都保留原有欄位數")
for nf in (4, 5, 6):
    src = [(10.0, 12.0, "文字", "A", [], False)[:nf]]
    out = apply_display_lead(src)
    check(f"{nf}-tuple 欄位數不變", len(out[0]) == nf, str(len(out[0])))
check("words 原樣保留",
      apply_display_lead([(10.0, 12.0, "文本", None,
                           [{"word": "文", "start": 10.0, "end": 11.0}])])[0][4]
      == [{"word": "文", "start": 10.0, "end": 11.0}])

print("\n[9] 空輸入與單條邊界")
check("空列表", apply_display_lead([]) == [])
one = apply_display_lead(rows((5.0, 9.0, "獨條")))
check("獨條不重疊、有提前",
      one[0][0] == 5.0 - LEAD_SEC and one[0][1] == 9.0 + HOLD_SEC)

print(f"\n結果：{PASS} 通過 / {FAIL} 失敗")
sys.exit(1 if FAIL else 0)
