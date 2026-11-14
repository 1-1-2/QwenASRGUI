"""vad_groups.py — Silero VAD 语音分组（全项目唯一实现）。

原先 ``app.py``（桌面 GUI）与 ``chatllm_engine.py``（核心 DLL）各存一份
**逐行相同**的 ``_detect_speech_groups``（AST 对拍 67/63 行，仅 docstring
与返回值注解不同）。改一处漏一处，两个引擎就会分出不同的段 —— 分段变了，
字幕行数与时间轴全变，属于最难察觉的一类不一致。现在只留这一份。

一个「组」可以包含多段语音以及它们之间的静音（``MERGE`` 与 ``max_group_sec``
两次合并都会跨越静音）。后续按字数比例分配时间时，那些静音会被当成语音算进
预算，这是已知偏差（实测每组多算 0.43~1.25 秒），不是本模块的职责。
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
VAD_CHUNK = 512          # Silero VAD 固定输入：32 ms / 帧
VAD_THRESHOLD = 0.5
MAX_GROUP_SEC = 20


def detect_speech_groups(
    audio: np.ndarray,
    vad_sess,
    max_group_sec: int = MAX_GROUP_SEC,
    stats: dict | None = None,
    *,
    threshold: float = VAD_THRESHOLD,
    sample_rate: int = SAMPLE_RATE,
    chunk_size: int = VAD_CHUNK,
) -> list[tuple[float, float, np.ndarray]]:
    """回傳 ``[(start_s, end_s, chunk_pcm), ...]``。

    ``stats`` 會填入 n_chunks / max_prob / mean_prob / threshold / n_segments，
    供上层在「没侦测到人声」时给出明确诊断（见 ``format_vad_diag``）。
    """
    h = np.zeros((2, 1, 64), dtype=np.float32)
    c = np.zeros((2, 1, 64), dtype=np.float32)
    sr = np.array(sample_rate, dtype=np.int64)
    n = len(audio) // chunk_size
    probs = []
    for i in range(n):
        buf = audio[i * chunk_size:(i + 1) * chunk_size].astype(np.float32)[np.newaxis, :]
        out, h, c = vad_sess.run(None, {"input": buf, "h": h, "c": c, "sr": sr})
        probs.append(float(out[0, 0]))
    if stats is not None:
        stats["n_chunks"] = len(probs)
        stats["max_prob"] = max(probs) if probs else 0.0
        stats["mean_prob"] = (sum(probs) / len(probs)) if probs else 0.0
        stats["threshold"] = threshold
    if not probs:
        return [(0.0, len(audio) / sample_rate, audio)]

    MIN_CH = 16              # 少于 0.5 秒的孤立响应不当作语音
    PAD = 5                  # 两端各补 5 帧（约 0.16 秒），避免吞掉轻声字
    MERGE = 16               # 相邻语音段间隔 <= 16 帧（约 0.5 秒）合成一段
    raw: list[tuple[int, int]] = []
    in_sp = False
    s0 = 0
    for i, p in enumerate(probs):
        if p >= threshold and not in_sp:
            s0 = i
            in_sp = True
        elif p < threshold and in_sp:
            if i - s0 >= MIN_CH:
                raw.append((max(0, s0 - PAD), min(n, i + PAD)))
            in_sp = False
    if in_sp and n - s0 >= MIN_CH:
        raw.append((max(0, s0 - PAD), n))
    if stats is not None:
        stats["n_segments"] = len(raw)
    if not raw:
        return []

    merged = [list(raw[0])]
    for s, e in raw[1:]:
        if s - merged[-1][1] <= MERGE:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    mx_samp = max_group_sec * sample_rate
    groups: list[tuple[int, int]] = []
    gs = merged[0][0] * chunk_size
    ge = merged[0][1] * chunk_size
    for seg in merged[1:]:
        s = seg[0] * chunk_size
        e = seg[1] * chunk_size
        if e - gs > mx_samp:
            groups.append((gs, ge))
            gs = s
        ge = e
    groups.append((gs, ge))

    # 多含 0.35 秒尾巴：Silero 的 ge 常把句尾轻声字（的/了/吗）排除在外，
    # mel 会把不足一帧的部分补零，所以多给一点没有副作用。
    _tail = int(0.35 * sample_rate)
    result = []
    for gs, ge in groups:
        ch = audio[gs: min(len(audio), ge + _tail)].astype(np.float32)
        if len(ch) < sample_rate // 2:      # 最小 0.5 秒
            continue
        result.append((gs / sample_rate, ge / sample_rate, ch))
    return result
