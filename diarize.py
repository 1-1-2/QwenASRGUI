"""
diarize.py
─────────────────────────────────────────────────────────────────────
說話者分離（Speaker Diarization）引擎

兩個 ONNX 模型（共 32.5 MB，位於 ov_models/diarization/）：
  segmentation-community-1.onnx  — 偵測語音段落 + 粗略說話者類別
  embedding_model.onnx           — 提取說話者聲紋向量（WeSpeaker）

聚類演算法（2026-08 改版）：
  三階段設計 — 長段落先切成約 3 秒子窗逐窗提聲紋（避免段內換人
  被單一聲紋攤平），再對全部子窗做一次全局聚類，最後把短段落
  指派給最近的群（不再丟棄，修正漏字）。

  ┌─ 自動模式（n_speakers=None）
  │   Average-linkage 層次聚類；對 2..6 人各切一次樹狀圖，
  │   以 silhouette 分數挑分離度最佳的人數；整體分數過低
  │   → 判定整檔僅一位說話者。（取代舊版固定 0.38 距離閾值——
  │   固定閾值對相近音色會併人、對環境變化大會拆人）
  └─ 指定人數（n_speakers=N）
      強制分成 N 組（maxclust criterion）
      → 適合已知說話者數量的情境，避免過度分割

使用方式：
  from diarize import DiarizationEngine
  eng = DiarizationEngine(model_dir / "diarization")
  segments = eng.diarize(audio_float32_16khz, n_speakers=2)
  # → [(0.40, 4.55, "說話者1"), (4.85, 9.28, "說話者2"), ...]
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort

# ── 常數（從 pyannote-rs 原始碼 segment.rs 取得）─────────────────
SAMPLE_RATE    = 16_000
WINDOW_SAMPLES = SAMPLE_RATE * 10          # 160,000 samples = 10 秒
FRAME_SIZE     = 270                       # samples per output frame
FRAME_START    = 721                       # initial sample offset
MIN_SEG_SEC    = 0.8                       # 過短段落過濾門檻（秒）
MERGE_GAP_SEC  = 0.30                      # 相鄰同說話者合併間距（秒）

# 自動模式的 cosine distance 切割閾值
# cosine_dist = 1 - cosine_sim；值越小 → 聚類越嚴格（說話者數越多）
# 2026-08 起僅作為「樣本數 < 4」時的 fallback；主要人數判定改用 silhouette
AUTO_DIST_THRESH = 0.38

# ── 聚類品質改良參數（2026-08）───────────────────────────────────
EMB_WIN_SEC       = 3.0    # 長段落切子窗提聲紋的視窗長度（秒）
KEEP_MIN_SEC      = 0.30   # 段落保留下限：低於此秒數視為雜訊丟棄
MAX_AUTO_SPEAKERS = 6      # 自動模式最多嘗試的說話者數
# 以下兩個閾值由 test_audio 實測校準（真1人 sil≤0.17/中心距≤0.25；
# 真多人 sil≥0.41/中心距≥0.39，中間有明確空隙）：
SIL_SINGLE_THRESH   = 0.25  # silhouette 低於此值 → 判定整檔僅一位說話者
CENTROID_MERGE_DIST = 0.30  # 群中心 cosine 距離低於此值 → 兩群視為同一人合併


def _subwindows(t0: float, t1: float,
                win: float = EMB_WIN_SEC) -> list[tuple[float, float]]:
    """把 [t0, t1] 均分成每窗約 win 秒的子窗（尾窗併入均分，不產生殘段）。"""
    n = max(1, int(round((t1 - t0) / win)))
    edges = np.linspace(t0, t1, n + 1)
    return list(zip(edges[:-1].tolist(), edges[1:].tolist()))


def _silhouette_from_dist(dist: np.ndarray, labels) -> float:
    """由預先算好的距離矩陣計算平均 silhouette 分數。

    單元素群的 silhouette 依標準定義記 0（避免噪音子窗自成一群
    反而拉高分數）。回傳值域約 [-1, 1]，越高代表群間分得越開。
    """
    labels = np.asarray(labels)
    uniq   = np.unique(labels)
    if len(uniq) < 2:
        return -1.0
    scores = np.zeros(len(labels))
    for i in range(len(labels)):
        same    = labels == labels[i]
        same_i  = same.copy()
        same_i[i] = False
        if not same_i.any():          # 單元素群 → 0
            continue
        a = dist[i, same_i].mean()
        b = min(dist[i, labels == c].mean() for c in uniq if c != labels[i])
        m = max(a, b)
        scores[i] = (b - a) / m if m > 1e-12 else 0.0
    return float(scores.mean())


class DiarizationEngine:
    """
    說話者分離引擎（thread-safe）。

    屬性：
        ready : bool  — 模型已載入且可用
    """

    def __init__(self, diar_dir: Path):
        self._lock    = threading.Lock()
        self.ready    = False
        self.seg_sess = None
        self.emb_sess = None
        self._diar_dir = diar_dir
        self._load()

    # ── 模型載入 ──────────────────────────────────────────────────

    def _load(self):
        seg_path = self._diar_dir / "segmentation-community-1.onnx"
        emb_path = self._diar_dir / "embedding_model.onnx"
        if not seg_path.exists() or not emb_path.exists():
            return   # 靜默失敗，self.ready 保持 False

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 2
        opts.log_severity_level   = 3   # 隱藏 ONNX Runtime 警告
        self.seg_sess = ort.InferenceSession(
            str(seg_path), sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.emb_sess = ort.InferenceSession(
            str(emb_path), sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.ready = True

    # ── 公開 API ─────────────────────────────────────────────────

    def diarize(
        self,
        audio: np.ndarray,
        n_speakers: int | None = None,
    ) -> list[tuple[float, float, str]]:
        """
        對 16kHz float32 音訊執行說話者分離。

        n_speakers : 指定說話者總人數（None = 自動偵測）。
                     已知人數時強烈建議指定，可避免過度分割。

        回傳：[(start_sec, end_sec, "說話者N"), ...]
        已過濾靜音與過短段落，可直接作為 ASR 的分段依據。
        """
        with self._lock:
            raw  = self._segment(audio)
            return self._embed_and_cluster(audio, raw, n_speakers=n_speakers)

    # ── 分割（Segmentation Model）────────────────────────────────

    def _segment(
        self, audio: np.ndarray
    ) -> list[tuple[float, float, int]]:
        """
        回傳：[(start_sec, end_sec, local_class), ...]
        local_class: 1-6（非 0 靜音）
        """
        input_name = self.seg_sess.get_inputs()[0].name   # "input_values"

        n   = len(audio)
        pad = (WINDOW_SAMPLES - n % WINDOW_SAMPLES) % WINDOW_SAMPLES
        padded = np.pad(audio.astype(np.float32), (0, pad))

        raw_segs: list[tuple[float, float, int]] = []

        for win_start in range(0, len(padded), WINDOW_SAMPLES):
            window = padded[win_start: win_start + WINDOW_SAMPLES]
            inp    = window[np.newaxis, np.newaxis, :]
            logits = self.seg_sess.run(None, {input_name: inp})[0]
            frame_labels = np.argmax(logits[0], axis=-1)

            def _frame_to_sec(fi: int, _ws: int = win_start) -> float:
                return (_ws + FRAME_START + fi * FRAME_SIZE) / SAMPLE_RATE

            cur_lbl   = int(frame_labels[0])
            seg_start = 0
            for fi in range(1, len(frame_labels)):
                lbl = int(frame_labels[fi])
                if lbl != cur_lbl:
                    if cur_lbl != 0:
                        raw_segs.append((_frame_to_sec(seg_start),
                                         _frame_to_sec(fi), cur_lbl))
                    seg_start = fi
                    cur_lbl   = lbl
            if cur_lbl != 0:
                raw_segs.append((_frame_to_sec(seg_start),
                                 _frame_to_sec(len(frame_labels)), cur_lbl))

        # 裁剪 + 過濾 + 合併相鄰同類段落
        total_dur = n / SAMPLE_RATE
        merged: list[tuple[float, float, int]] = []
        for t0, t1, lbl in raw_segs:
            t0 = min(t0, total_dur)
            t1 = min(t1, total_dur)
            if t1 - t0 < 0.1:
                continue
            if (merged and merged[-1][2] == lbl
                    and t0 - merged[-1][1] < MERGE_GAP_SEC):
                merged[-1] = (merged[-1][0], t1, lbl)
            else:
                merged.append((t0, t1, lbl))

        return merged

    # ── 嵌入提取（Embedding Model）──────────────────────────────

    def _kaldi_fbank(self, samples_f32: np.ndarray) -> np.ndarray:
        """Kaldi-style 80 維 mel filter bank（WeSpeaker 標準前處理）。"""
        import kaldi_native_fbank as knf

        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq       = float(SAMPLE_RATE)
        opts.frame_opts.frame_length_ms = 25.0
        opts.frame_opts.frame_shift_ms  = 10.0
        opts.mel_opts.num_bins          = 80
        opts.frame_opts.dither          = 0.0

        fbank = knf.OnlineFbank(opts)
        fbank.accept_waveform(
            float(SAMPLE_RATE),
            (samples_f32 * 32768.0).tolist(),
        )
        fbank.input_finished()

        n_frames = fbank.num_frames_ready
        if n_frames == 0:
            return np.zeros((1, 80), dtype=np.float32)

        feats = np.array(
            [fbank.get_frame(i) for i in range(n_frames)],
            dtype=np.float32,
        )
        feats -= feats.mean(axis=0, keepdims=True)   # CMN 正規化
        return feats

    def _get_embedding(
        self, audio: np.ndarray, t0: float, t1: float
    ) -> np.ndarray | None:
        """從 [t0, t1] 秒音訊提取 L2 正規化後的 256 維說話者向量。"""
        s0    = int(t0 * SAMPLE_RATE)
        s1    = int(t1 * SAMPLE_RATE)
        chunk = audio[s0:s1]
        if len(chunk) < SAMPLE_RATE * 0.5:
            return None

        feats = self._kaldi_fbank(chunk)
        out   = self.emb_sess.run(
            None, {"fbank_features": feats[np.newaxis, :]}
        )[0][0]
        norm = np.linalg.norm(out)
        return out / norm if norm > 1e-9 else out

    # ── 兩階段聚類 ───────────────────────────────────────────────

    def _embed_and_cluster(
        self,
        audio: np.ndarray,
        raw_segs: list[tuple[float, float, int]],
        n_speakers: int | None = None,
    ) -> list[tuple[float, float, str]]:
        """
        三階段設計（2026-08 改版）：
          Stage 1  — 長段落切成約 EMB_WIN_SEC 秒的子窗逐窗提聲紋，
                     避免長段落單一聲紋把段內換人攤平成「混合聲紋」
          Stage 2  — 對所有子窗聲紋統一聚類（自動模式以 silhouette
                     選最佳人數），並做段內標籤平滑
          Stage 3  — 短段落不再丟棄：有聲紋者指派最近群中心，
                     無聲紋者跟隨時間上最近的鄰居（修正舊版
                     < 0.8 秒短句整段從 ASR 消失的漏字問題）
        """
        # Stage 1：子窗聲紋。units: (seg_id, w0, w1, emb)
        units:  list[tuple[int, float, float, np.ndarray]] = []
        shorts: list[tuple[float, float, np.ndarray | None]] = []
        for seg_id, (t0, t1, _) in enumerate(raw_segs):
            if t1 - t0 < KEEP_MIN_SEC:
                continue                       # 過短雜訊，仍然丟棄
            if t1 - t0 < MIN_SEG_SEC:          # 短段：留待 Stage 3 指派
                shorts.append((t0, t1, self._get_embedding(audio, t0, t1)))
                continue
            for w0, w1 in _subwindows(t0, t1):
                emb = self._get_embedding(audio, w0, w1)
                if emb is not None:
                    units.append((seg_id, w0, w1, emb))

        if not units:
            # 沒有任何可靠長段落：短段全數視為單一說話者輸出
            return [(t0, t1, "說話者1") for t0, t1, _ in shorts]

        embeddings = [u[3] for u in units]

        # Stage 2：聚類取得說話者標籤
        if len(embeddings) == 1:
            labels = [1]
        elif n_speakers is not None:
            labels = self._cluster_fixed_n(embeddings, n_speakers)
        else:
            labels = self._cluster_auto(embeddings)

        # 段內平滑：單一子窗與前後鄰居（同一原始段落）不同
        # → 跟隨鄰居，消除聲紋抖動造成的假換人
        for i in range(1, len(units) - 1):
            if (units[i - 1][0] == units[i][0] == units[i + 1][0]
                    and labels[i - 1] == labels[i + 1] != labels[i]):
                labels[i] = labels[i - 1]

        # Stage 3：短段落指派
        centroids: dict[int, np.ndarray] = {}
        for lbl in set(labels):
            c = np.mean([e for e, l in zip(embeddings, labels) if l == lbl],
                        axis=0)
            nrm = np.linalg.norm(c)
            centroids[lbl] = c / nrm if nrm > 1e-9 else c

        placed: list[tuple[float, float, int]] = [
            (w0, w1, lbl) for (_, w0, w1, _), lbl in zip(units, labels)
        ]
        for t0, t1, emb in shorts:
            if emb is not None:                # ≥0.5 秒：最近群中心
                lbl = min(centroids,
                          key=lambda k: 1.0 - float(emb @ centroids[k]))
            else:                              # <0.5 秒：時間上最近的鄰居
                mid = (t0 + t1) / 2.0
                lbl = min(placed,
                          key=lambda p: abs((p[0] + p[1]) / 2.0 - mid))[2]
            placed.append((t0, t1, lbl))
        placed.sort(key=lambda p: p[0])

        # 標籤依首次出現順序重編為 1..N（「說話者1」永遠最先開口）
        remap: dict[int, int] = {}
        for _, _, lbl in placed:
            if lbl not in remap:
                remap[lbl] = len(remap) + 1

        # 合併相鄰的同說話者段落
        merged: list[tuple[float, float, str]] = []
        for t0, t1, lbl in placed:
            spk = f"說話者{remap[lbl]}"
            if (merged and merged[-1][2] == spk
                    and t0 - merged[-1][1] < MERGE_GAP_SEC):
                merged[-1] = (merged[-1][0], t1, spk)
            else:
                merged.append((t0, t1, spk))

        return merged

    def _cluster_fixed_n(
        self, embeddings: list[np.ndarray], n: int
    ) -> list[int]:
        """
        指定人數模式：Average-linkage 層次聚類，強制分成 n 組。
        回傳 1-indexed 標籤列表。
        """
        import cluster_core

        n = max(1, min(n, len(embeddings)))  # 限制在合理範圍
        if len(embeddings) <= n:
            return list(range(1, len(embeddings) + 1))

        emb_mat = np.stack(embeddings)           # (M, 256)
        sim     = emb_mat @ emb_mat.T            # cosine similarity
        dist    = np.clip(1.0 - sim, 0.0, 2.0)  # cosine distance
        np.fill_diagonal(dist, 0.0)

        Z      = cluster_core.average_linkage(dist)
        labels = cluster_core.fcluster_maxclust(Z, n)      # 1-indexed
        return labels.tolist()

    def _cluster_auto(
        self, embeddings: list[np.ndarray]
    ) -> list[int]:
        """
        自動模式（2026-08 改版）：對 2..MAX_AUTO_SPEAKERS 人各切一次
        層次聚類樹，以 silhouette 分數挑分離度最佳的人數；整體分數
        低於 SIL_SINGLE_THRESH（群間根本分不開）→ 判定僅一位說話者。

        取代舊版固定距離閾值（AUTO_DIST_THRESH）切割：固定閾值對
        「同性別相近音色」會把兩人併成一人、對「錄音環境變化大」
        會把同一人拆成多人——正是實際使用回報的兩大症狀。
        固定閾值僅保留給樣本數 < 4 的極短音訊當 fallback。

        回傳 1-indexed 標籤列表。
        """
        import cluster_core

        M = len(embeddings)
        if M < 2:
            return [1] * M

        emb_mat = np.stack(embeddings)
        dist    = np.clip(1.0 - emb_mat @ emb_mat.T, 0.0, 2.0)
        np.fill_diagonal(dist, 0.0)
        Z = cluster_core.average_linkage(dist)

        if M < 4:   # 樣本太少 silhouette 不可靠 → 退回固定閾值
            return cluster_core.fcluster_distance(Z, AUTO_DIST_THRESH).tolist()

        best_sil, best_labels = -1.0, None
        for k in range(2, min(MAX_AUTO_SPEAKERS, M - 1) + 1):
            labels = cluster_core.fcluster_maxclust(Z, k)
            sil = _silhouette_from_dist(dist, labels)
            if sil > best_sil:
                best_sil, best_labels = sil, labels.tolist()

        if best_labels is None or best_sil < SIL_SINGLE_THRESH:
            return [1] * M      # 群間分不開 → 整檔視為一位說話者

        # 第二重防護：群中心距離過近（同一人被拆）→ 反覆合併最近的兩群。
        # 實測同一人的子群中心距 ≤0.25、不同人 ≥0.39，0.30 落在空隙中間。
        labels = np.asarray(best_labels)
        while True:
            uniq = np.unique(labels)
            if len(uniq) < 2:
                break
            cents = {}
            for c in uniq:
                v = emb_mat[labels == c].mean(axis=0)
                nrm = np.linalg.norm(v)
                cents[c] = v / nrm if nrm > 1e-9 else v
            pair, mind = None, 2.0
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    d = 1.0 - float(cents[uniq[i]] @ cents[uniq[j]])
                    if d < mind:
                        pair, mind = (uniq[i], uniq[j]), d
            if mind >= CENTROID_MERGE_DIST:
                break
            labels[labels == pair[1]] = pair[0]
        return labels.tolist()
