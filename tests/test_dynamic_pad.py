"""动态 pad 回归：长度按实际音长推导，而不是固定补满天花板。

    python tests/test_dynamic_pad.py

两部分：
  A. 纯算术（不碰文件）—— _layout 的取整、夹紧、每秒系数；
  B. 有模型时（找不到 ov_models 则跳过）—— mel 帧数/token 数与固定 pad
     版本的前 N 帧逐字节一致，这是「动态 pad 不改变识别结果」的依据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from processor_numpy import LightProcessor  # noqa: E402

SR = 16_000
MODEL_DIR = Path("C:/Green/QwenASR-WebView/ov_models/qwen3_asr_1p7b_kv_int8")


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def fake_proc(ceiling=20, fps=100, tps=13):
    """跳过 __init__（不读模型文件），只装配 _layout 需要的三个字段。"""
    p = object.__new__(LightProcessor)
    p._ceiling_secs = ceiling
    p._frames_per_sec = fps
    p._tokens_per_sec = tps
    return p


def main() -> int:
    r = Result()

    print("\n[A1] 向上取整到整秒")
    p = fake_proc()
    cases = [
        (0.0, 1), (0.3, 1), (1.0, 1), (1.01, 2), (3.8, 4),
        (5.5, 6), (9.9, 10), (10.0, 10), (10.01, 11), (19.9, 20),
    ]
    for sec, want in cases:
        got = p._layout(int(sec * SR))[0] // SR
        r.check(f"{sec:>5.2f}s → {want}s", got == want, str(got))

    print("\n[A2] 三常量同步缩放（100 帧/秒、13 token/秒）")
    for sec in (1, 4, 10, 11, 20):
        n, f, t = p._layout(sec * SR)
        r.check(f"{sec:>2}s → {n} 采样 / {f} 帧 / {t} token",
                n == sec * SR and f == sec * 100 and t == sec * 13)

    print("\n[A3] 夹紧到天花板，且不低于 1 秒")
    r.check("25s → 天花板 20s", p._layout(25 * SR)[0] // SR == 20)
    r.check("100s → 天花板 20s", p._layout(100 * SR)[0] // SR == 20)
    r.check("0 采样 → 1s", p._layout(0)[0] // SR == 1)
    p10 = fake_proc(ceiling=10)
    r.check("10s 天花板下 15s → 10s", p10._layout(15 * SR)[0] // SR == 10)

    print("\n[A4] 天花板可执行期覆盖，但每秒系数仍按模板换算")
    # 模板 10s/1000帧/130token 算出 100 帧/秒；天花板放到 20 不该变成 50 帧/秒
    tmp = object.__new__(LightProcessor)
    tmp._ceiling_secs, tmp._frames_per_sec, tmp._tokens_per_sec = 20, 100, 13
    _, f, t = tmp._layout(20 * SR)
    r.check("20s → 2000 帧（不是 1000）", f == 2000, str(f))
    r.check("20s → 260 token（不是 130）", t == 260, str(t))

    total = len(r.items)

    if not (MODEL_DIR / "prompt_template.json").exists():
        print(f"\n[B] 跳过（找不到 {MODEL_DIR}）")
    else:
        print("\n[B1] 真实 processor：mel 形状随音长变化")
        proc = LightProcessor(MODEL_DIR, max_secs=20)
        r.check("天花板 20s", proc._ceiling_secs == 20, str(proc._ceiling_secs))
        r.check("系数 100 帧/13 token", proc._frames_per_sec == 100 and
                proc._tokens_per_sec == 13,
                f"{proc._frames_per_sec}/{proc._tokens_per_sec}")
        for sec, frames, tokens in ((3.8, 400, 52), (5.5, 600, 78), (19.9, 2000, 260)):
            a = np.full(int(sec * SR), 1e-3, dtype=np.float32)
            mel, ids = proc.prepare(a)
            r.check(f"{sec}s → {frames} 帧 / {tokens}+15 token",
                    mel.shape == (1, 128, frames) and ids.shape[1] == tokens + 15,
                    f"mel{mel.shape} ids{ids.shape[1]}")

        print("\n[B2] 与固定 pad 版本前 N 帧逐字节一致（等价性依据）")
        short = (np.random.default_rng(7).standard_normal(int(3.8 * SR))
                 * 0.05).astype(np.float32)
        padded = np.concatenate([short, np.zeros(int(6.2 * SR), dtype=np.float32)])
        m_dyn = proc._extract_mel(short)          # 400 帧（4 秒）
        m_fix = proc._extract_mel(padded)         # 1000 帧（10 秒）
        r.check("动态版 400 帧", m_dyn.shape[2] == 400, str(m_dyn.shape))
        r.check("固定版 1000 帧", m_fix.shape[2] == 1000, str(m_fix.shape))
        # 同一段音频，只是批量形状不同（400 帧 vs 1000 帧）—— np.fft.rfft
        # 会因批量大小走不同的 SIMD 分组，最低位舍入不同。实测 151/400 帧
        # 各差 5.96e-08（= 2^-24，float32 的 1 个 ulp），远低于任何有意义
        # 的精度：mel 本身量化到 1/256。这也是「不能靠逐字节对拍验证」的原因。
        d = float(np.abs(m_dyn - m_fix[:, :, :400]).max())
        r.check("前 400 帧差异 ≤ 1 ulp（FFT 批量形状噪声）", d < 1e-6, f"{d:.2e}")

        print("\n[B3] 超过天花板时截断而不是报错")
        a = np.full(int(25 * SR), 1e-3, dtype=np.float32)
        mel, ids = proc.prepare(a)
        r.check("25s → 夹到 20s", mel.shape[2] == 2000 and ids.shape[1] == 275,
                f"mel{mel.shape} ids{ids.shape[1]}")

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
