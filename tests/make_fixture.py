"""生成几分钟的长音频测试集（离线、可复现、无需联网）。

用法：
    python tests/make_fixture.py                 # 生成约 5 分钟
    python tests/make_fixture.py --minutes 3     # 生成约 3 分钟
    python tests/make_fixture.py --out /tmp/x.wav

素材来自 tests/fixtures/sources/ 内已随仓库提交的 5 段公开短音频，
均为 16kHz / 单声道 / 16bit PCM，直接用标准库 wave 拼接，不依赖 ffmpeg。
来源与许可见同目录 SOURCES.md。

设计意图：模拟网课的「有声段 + 句间停顿 + 章节长静音」结构，
用于验证流式 VAD 分段、增量落盘、断点续跑与时间轴是否随文件长度漂移。
重复拼接是为了用很小的仓库体积得到足够长的压力测试输入，
**不是准确率评测集**；准确率请用真实中文网课音频另测。
"""

from __future__ import annotations

import argparse
import random
import wave
from pathlib import Path

import numpy as np

SR = 16000
HERE = Path(__file__).resolve().parent
SOURCES = HERE / "fixtures" / "sources"
DEFAULT_OUT = HERE / "fixtures" / "longform_4min.wav"
ORDER = ["jfk_pd.wav", "funasr_zh.wav", "aishell_zh.wav",
         "arctic_en.wav", "new_home_en.wav"]


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2):
            raise ValueError(f"素材格式不符（需 16k/mono/s16le）：{path}")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def build(minutes: float, seed: int = 20261110) -> tuple[np.ndarray, int]:
    clips = [read_wav(SOURCES / n) for n in ORDER]
    rng = random.Random(seed)
    target = int(minutes * 60 * SR)
    out: list[np.ndarray] = []
    rounds = 0
    while sum(len(x) for x in out) < target:
        order = clips[:]
        rng.shuffle(order)
        for a in order:
            out.append(a)
            gap = rng.choice([0.6, 0.9, 1.2, 1.6, 2.2])
            out.append(np.zeros(int(gap * SR), dtype=np.int16))
        out.append(np.zeros(int(2.0 * SR), dtype=np.int16))   # 章节停顿
        rounds += 1
    pcm = np.concatenate(out)[:target]
    return pcm.astype("<i2"), rounds


def main() -> int:
    ap = argparse.ArgumentParser(description="生成长音频测试集（简体中文输出）")
    ap.add_argument("--minutes", type=float, default=4.9, help="目标时长（分钟），默认 4.9")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 wav 路径")
    ap.add_argument("--seed", type=int, default=20261110, help="随机种子，保证可复现")
    args = ap.parse_args()

    pcm, rounds = build(args.minutes, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"已生成：{args.out}")
    print(f"时长：{len(pcm)/SR/60:.2f} 分钟；{rounds} 轮；"
          f"{args.out.stat().st_size/1024/1024:.1f} MB；16kHz/mono/s16le")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
