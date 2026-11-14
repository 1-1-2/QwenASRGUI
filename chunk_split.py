"""chunk_split.py — 超長語音段的切分（全項目唯一實現）。

VAD 分組對「單一連續語音段」不生效：一段 66 秒不停頓的講話會整塊吐出，
必須在下游再切。原先的等長硬切（``chunk[pos:pos+limit]`` 每 limit 一刀）
刀口落在哪個字中間全憑運氣 —— 實測 600 秒語音有 33 個刀口，每刀都有
機會把一個字劈成兩半（前半段和後半段分屬兩次辨識，兩邊都會錯）。

本模組把刀口挪到能量最低的位置：字與字之間、換氣點、句讀處的 RMS 通常
比音節中心低 10~20 dB，在 limit 前 1.5 秒的窗口內找最低點就能命中。

限制：只能看振幅，看不到語義（VAD 的逐幀概率在 detect_speech_groups
裡算完就丟了，這裡只有 PCM）。爆破音、氣聲會判錯，但不影響正確性 ——
代價只是這刀的落點不夠理想，仍好於固定步長。

為什麼不搜全段最低點：那會把 20 秒的段切成 3 秒 + 17 秒，段數暴漲、
每段都要重新付固定開銷（encoder 前向），速度反而崩。
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16_000
SEARCH_SEC = 1.5      # 只在 limit 前 1.5 秒內找低谷
FRAME_SEC = 0.02      # 能量窗 20 ms
MIN_TAIL_SEC = 1.0    # 尾巴至少留 1 秒，否則並回前一片（原實現直接丟棄）


def _quiet_cut(sub: np.ndarray, hi: int, sample_rate: int,
               search_sec: float, frame_sec: float) -> int:
    """在 ``[hi - search_sec, hi]`` 內找 RMS 最低的窗口，回傳切點（樣本）。"""
    win = max(1, int(frame_sec * sample_rate))
    lo = max(sample_rate // 2, hi - int(search_sec * sample_rate))
    n = (hi - lo) // win
    if n < 2:
        return hi
    frames = sub[lo:lo + n * win].reshape(n, win).astype(np.float32)
    rms = np.sqrt((frames * frames).mean(axis=1))
    i = int(np.argmin(rms))
    return lo + i * win + win // 2


def split_oversized(
    groups: list[tuple[float, float, np.ndarray, "str | None"]],
    max_secs: int | float,
    *,
    sample_rate: int = SAMPLE_RATE,
    search_sec: float = SEARCH_SEC,
    frame_sec: float = FRAME_SEC,
) -> list[tuple[float, float, np.ndarray, "str | None"]]:
    """把超過 ``max_secs`` 的段落按能量低谷切開，時間軸隨之平移。

    每片保證 ≥1 秒且 ≤max_secs；尾巴不足 1 秒時切點前移（原實現會把
    <1 秒的殘片直接丟掉，等於吞掉最後半句話）。
    """
    limit = int(max_secs * sample_rate)
    if limit <= 0:
        return list(groups)
    min_tail = int(MIN_TAIL_SEC * sample_rate)
    result: list[tuple[float, float, np.ndarray, "str | None"]] = []

    for t0, t1, chunk, spk in groups:
        n = len(chunk)
        if n <= limit:
            result.append((t0, t1, chunk, spk))
            continue

        start = 0
        while n - start > limit:
            # 右界要同時滿足：不超過 limit，且不留下 <1 秒的尾巴
            hi = min(start + limit, n - min_tail)
            if hi - start < sample_rate:
                break                       # 剩餘太短，整塊交出（上游再兜底）
            cut = start + _quiet_cut(chunk[start:], hi - start,
                                     sample_rate, search_sec, frame_sec)
            if cut <= start:                # 防禦：切點必須前進
                cut = hi
            piece = chunk[start:cut]
            result.append((t0 + start / sample_rate,
                           t0 + cut / sample_rate,
                           piece, spk))
            start = cut

        rest = chunk[start:]
        if len(rest) >= sample_rate // 2:
            result.append((t0 + start / sample_rate,
                           t0 + n / sample_rate,
                           rest, spk))
        elif result:
            # 理論上到不了（上方已保證尾巴 ≥1 秒），保險起見併回前一片，
            # 不讓音頻憑空消失。
            pt0, _pt1, pchunk, pspk = result[-1]
            result[-1] = (pt0, t0 + n / sample_rate,
                          np.concatenate([pchunk, rest]), pspk)
    return result
