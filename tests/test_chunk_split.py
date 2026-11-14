"""chunk_split 回归：低谷切分的落点、守恒与边界。

    python tests/test_chunk_split.py

不需要模型权重：全部由合成信号驱动。核心断言有三条 ——
  1. 每片不超过上限、不短于 1 秒；
  2. 刀口落在人为埋的静音缝上，而不是十分钟整点；
  3. 切完的样本总数与原始一致（不丢音频，这是旧实现的坑）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunk_split import split_oversized  # noqa: E402

SR = 16_000


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def speech(total_sec: float, gaps: list[float], gap_ms: int = 60) -> np.ndarray:
    """连续「语音」= 低频正弦，在指定时刻插入绝对静音缝。"""
    n = int(total_sec * SR)
    t = np.arange(n, dtype=np.float32) / SR
    x = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    w = int(gap_ms / 1000 * SR)
    for g in gaps:
        c = int(g * SR)
        x[max(0, c - w // 2):c + w // 2] = 0.0
    return x


def main() -> int:
    r = Result()

    print("\n[1] 未超限的段落原样返回")
    a = speech(5, [])
    out = split_oversized([(0.0, 5.0, a, None)], 10, sample_rate=SR)
    r.check("段数不变", len(out) == 1, str(len(out)))
    r.check("内容不变", out[0][2] is a)

    print("\n[2] 超限段：每片 ≤ 上限且 ≥ 1 秒")
    long = speech(45, [8.7, 17.3, 28.1, 36.5])
    out = split_oversized([(0.0, 45.0, long, None)], 10, sample_rate=SR)
    lens = [len(c) / SR for _, _, c, _ in out]
    r.check("切出多片", len(out) >= 5, f"{len(out)} 片")
    r.check("每片 ≤ 10s", all(x <= 10.0 + 1e-6 for x in lens),
            " ".join(f"{x:.2f}" for x in lens))
    r.check("每片 ≥ 1s", all(x >= 1.0 - 1e-6 for x in lens),
            " ".join(f"{x:.2f}" for x in lens))

    print("\n[3] 刀口落在静音缝（8.7s）而不是 10s 整点")
    first = lens[0]
    r.check("首刀 ≈8.7s", abs(first - 8.7) < 0.15, f"{first:.3f}s")

    print("\n[4] 样本守恒：不丢音频")
    r.check("总采样数一致", sum(len(c) for _, _, c, _ in out) == len(long),
            f"{sum(len(c) for _, _, c, _ in out)} vs {len(long)}")

    print("\n[5] 时间轴连续、不重叠、随切前移")
    ok = True
    prev_end = 0.0
    for t0, t1, c, _ in out:
        if abs(t0 - prev_end) > 1e-6 or abs((t1 - t0) - len(c) / SR) > 1e-6:
            ok = False
        prev_end = t1
    r.check("时间轴首尾相接", ok)
    r.check("终点 = 原段终点", abs(out[-1][1] - 45.0) < 1e-6, f"{out[-1][1]:.3f}")

    print("\n[6] 尾巴不足 1 秒时切点前移（旧实现会丢弃 <1s 残片）")
    # 10.4 秒：按上限切会剩 0.4 秒，必须改切在 ~9.4 秒处
    a2 = speech(10.4, [9.4])
    out2 = split_oversized([(0.0, 10.4, a2, None)], 10, sample_rate=SR)
    r.check("切成 2 片", len(out2) == 2, str(len(out2)))
    r.check("尾片 ≥ 1 秒", len(out2[-1][2]) / SR >= 1.0, f"{len(out2[-1][2])/SR:.2f}s")
    r.check("样本守恒", sum(len(c) for _, _, c, _ in out2) == len(a2))

    print("\n[7] 边界：空列表 / 恰好等于上限 / 非语音静音")
    r.check("空列表", split_oversized([], 10, sample_rate=SR) == [])
    exact = np.zeros(10 * SR, dtype=np.float32)
    r.check("恰好 10s 不切",
            len(split_oversized([(0.0, 10.0, exact, None)], 10, sample_rate=SR)) == 1)
    silence = np.zeros(25 * SR, dtype=np.float32)
    out3 = split_oversized([(0.0, 25.0, silence, None)], 10, sample_rate=SR)
    r.check("全静音也能切完", all(len(c) / SR <= 10.0 + 1e-6 for _, _, c, _ in out3),
            f"{len(out3)} 片")
    r.check("全静音不丢样本", sum(len(c) for _, _, c, _ in out3) == len(silence))

    print("\n[8] 说话人标签在切片后保留")
    out4 = split_oversized([(0.0, 45.0, long, "SPK_2")], 10, sample_rate=SR)
    r.check("标签跟随每片", all(spk == "SPK_2" for _, _, _, spk in out4))

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
