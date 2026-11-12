"""subtitle_lines.py — 字級時間軸 → 字幕行（後端無關，全引擎共用）

把「字級 (word, start_sec, end_sec) + ASR 原文」轉成字幕行的邏輯集中於此，
讓 OpenVINO / chatllm / CrispASR(Whisper) 三種引擎產出**一致**的字幕斷句與
時間軸（標點切行 + MAX_CHARS/MAX_WORDS 保護 + 孤兒行合併）。

公開符號：
    MAX_CHARS, _ZH_CLAUSE_END, _EN_SENT_END   斷句常數
    _srt_ts(s)                                秒 → SRT 時間戳
    _merge_orphan_lines(lines)                合併過短孤兒行
    _ts_chatllm_to_subtitle_lines(...)        字級 list → [(start,end,text,spk)]
"""
from __future__ import annotations

# ── 斷句常數（與舊 app.py 行為一致）──────────────────────────────────
MAX_CHARS      = 20
_ZH_CLAUSE_END = frozenset('，。？！；：…—、·')
_EN_SENT_END   = frozenset('.,!?;')

# 強標點：句末，必定切行。其餘（逗號、頓號、冒號、破折號、省略號）為弱標點，
# 只在累積夠長時切。分級的依據是「這裡是不是一句話的終點」，不是標點種類多少。
_STRONG_PUNCT  = frozenset('。？！；!?;')

# ── 時間軸分配常數（原 app.py / app-gpu.py / chatllm_engine.py 三份副本共用）──
MIN_SUB_SEC    = 0.6     # 單行最短顯示時間；不足者向後借時間
GAP_SEC        = 0.08    # 相鄰兩行之間的固定留白


def _assign_ts(lines: list[str], g0: float, g1: float,
               min_sub: float = MIN_SUB_SEC,
               gap: float = GAP_SEC) -> list[tuple[float, float, str]]:
    """把「一堆文字行」鋪進 [g0, g1] 這段時間，回傳 [(start, end, line), ...]。

    為什麼不能按「字數比例 + 每行至少 min_sub + 憑空插入 gap」直接累加
    （舊實作的做法，見 git 歷史）：

      1. 每行 `max(min_sub, 比例)` 的下限會讓總時長超過 `g1 - g0`；
      2. 每行結束後再 `cur = end + gap` 又把 gap 算了一次；
      3. 末行再被 `max(end, g1)` 拉長。
    三者疊加的結果是行與行互相重疊，實測 77715 行樣本裡有 8.5% 負間隔
    （最深 2.11s），而且行間間隔恆等於 `gap`，完全不含聲學資訊。

    這裡改成「先預算、後分配、再強制單調」：

      • 預算 `budget = dur - gap * (n - 1)`，gap 只花一次，且從總時長裡扣；
      • 按字數比例分配預算，比例分配天然保證 sum == budget（無累積誤差）；
      • 逐行取 `max(min_sub, 比例額)`；若該行被擡高到 min_sub，多出來的時間
        從後續「仍有富餘」的行裡等比扣回，仍然保證總長不超預算；
      • 最後一趟強制 `start[i] = end[i-1] + gap`，並把末行釘在 `g1`。

    保證：0 <= start[i] < end[i] <= g1、`start[i] >= end[i-1] + gap`，
    即不重疊、不倒退、不超界。音頻太短以致塞不下 n 行時，改為均分
    （此時 min_sub 讓位——寧可短也不能重疊）。
    """
    n = len(lines)
    if n == 0:
        return []
    total_chars = sum(len(l) for l in lines)
    if total_chars == 0:
        total_chars = n                  # 全空行時退化為等長分配
    dur = max(0.0, g1 - g0)

    # gap 只在「行與行之間」出現，共 n-1 個
    gap_total = gap * (n - 1)
    if dur <= gap_total:
        # 連 gap 都塞不下：放棄留白與最短時長，硬均分，優先保證不重疊
        gap = dur / n if n > 1 else 0.0
        min_sub = max(0.0, dur / n - gap)
        budget = max(0.0, dur - gap * (n - 1))
    else:
        budget = dur - gap_total

    # ── 第一趟：按字數比例 ────────────────────────────────────────────
    raw = [budget * len(l) / total_chars for l in lines]

    # ── 第二趟：擡高短行到 min_sub，超支部分從「有富餘」的行等比扣回 ──
    #
    # 注意：若 n * min_sub > budget，min_sub 本身就不成立（時間不夠），
    # 此時必須放棄 min_sub 而不是讓總長超支——否則末行會被 g1 截斷，
    # 中間行溢出到 g1 之後。所以先做一次可行性判斷。
    if min_sub * n > budget:
        # 時間塞不下最短時長：均分，min_sub 讓位
        raw = [budget / n] * n
    else:
        for _ in range(6):                  # 迭代收斂，正常 1~2 輪即穩定
            deficit = sum(max(0.0, min_sub - v) for v in raw)
            if deficit <= 1e-9:
                break
            spare = sum(max(0.0, v - min_sub) for v in raw)
            if spare <= 1e-9:
                break                       # 已無可扣，交給下方守恆修正
            take = min(deficit, spare)
            factor = 1.0 - take / spare
            new = []
            for v in raw:
                if v <= min_sub:
                    new.append(min_sub)
                else:
                    new.append(min_sub + (v - min_sub) * factor)
            raw = new

    # 守恆收尾：無論上面怎麼調整，都強制總和 == budget
    cur_sum = sum(raw)
    if cur_sum > 1e-12:
        scale = budget / cur_sum
        raw = [v * scale for v in raw]      # 最後一次整體歸一，確保不超支
    else:
        raw = [budget / n] * n

    # ── 第三趟：累加並強制單調 ────────────────────────────────────────
    res: list[tuple[float, float, str]] = []
    cur = g0
    for i, (line, span) in enumerate(zip(lines, raw)):
        start = cur
        end = start + span
        if i == n - 1:
            end = g1                        # 末行精確收在 g1
        if end <= start:                    # 保底：絕不產生零長度
            end = start + 1e-3
        res.append((start, end, line))
        cur = end + gap
    return res


def _strip_pua(s: str) -> str:
    """濾除 Unicode 私用區字元（BMP U+E000–U+F8FF 與兩個補充平面）。

    合法的中文／日文／英文字幕不會用到私用區；出現在這裡的一律是下游元件把
    「無法映射的內部 ID」渲染成字元的產物，屬於亂碼。濾掉比留著好——留著會
    一路污染 SRT、JSON 與外部 Agent。
    """
    if not s:
        return s
    return "".join(c for c in s
                   if not (0xE000 <= ord(c) <= 0xF8FF
                           or 0xF0000 <= ord(c) <= 0xFFFFD
                           or 0x100000 <= ord(c) <= 0x10FFFD))


def _srt_ts(s: float) -> str:
    ms = int(round(s * 1000))
    hh = ms // 3_600_000; ms %= 3_600_000
    mm = ms // 60_000;    ms %= 60_000
    ss = ms // 1_000;     ms %= 1_000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


# ── 全域輸出格式（"srt" | "txt"）────────────────────────────────────────
# 由 app 啟動 / 設定變更時同步（app._on_output_format_change → 改寫此值）。
# write_transcript() 在 out_format=None 時讀此值，使「批次 / 單檔 / 錄製」
# 三條路徑全域一致；端點需內部解析 SRT，固定以 out_format="srt" 覆寫。
OUTPUT_FORMAT = "srt"


def lines_to_txt(lines: list[tuple[float, float, str, str | None]]) -> str:
    """字幕行 → 純文字（沿用端點既有慣例，與 api_server._parse_srt 後處理一致）。

    • 無說話者：整段文字相連成一行（中文不插空白，重建連續逐字稿）。
    • 有說話者：每段一行，保留「說話者N：」前綴，便於分辨發言者。
    """
    has_spk = any(spk for (_s, _e, _t, spk) in lines)
    if has_spk:
        return "\n".join(
            (f"{spk}：{t}" if spk else t) for (_s, _e, t, spk) in lines
        )
    return "".join(t for (_s, _e, t, _spk) in lines)


def write_transcript(
    ref,
    lines: list[tuple[float, float, str, str | None]],
    out_format: str | None = None,
):
    """把字幕行依格式寫成 .srt 或 .txt，回傳實際輸出路徑（Path）。

    所有引擎（OpenVINO / chatllm / CrispASR）與錄製轉換共用此單一寫出點，
    確保全域輸出格式一致。

    參數：
        ref        : 決定輸出目錄與主檔名的參考路徑（通常為原始音檔）。
        lines      : [(start_sec, end_sec, text, speaker|None), ...]
        out_format : "srt" | "txt"；None 時採用全域 OUTPUT_FORMAT。
                     端點固定傳 "srt"（內部需解析時間軸），不受全域影響。
    """
    from pathlib import Path
    fmt = (out_format or OUTPUT_FORMAT or "srt").lower()
    ref = Path(ref)
    if fmt == "txt":
        out = ref.parent / (ref.stem + ".txt")
        out.write_text(lines_to_txt(lines), encoding="utf-8")
        return out
    out = ref.parent / (ref.stem + ".srt")
    with open(out, "w", encoding="utf-8") as f:
        for idx, (s, e, line, spk) in enumerate(lines, 1):
            prefix = f"{spk}：" if spk else ""
            f.write(f"{idx}\n{_srt_ts(s)} --> {_srt_ts(e)}\n{prefix}{line}\n\n")
    return out


def _merge_orphan_lines(
    lines: list[tuple[float, float, str, str | None]],
    min_chars: int = 1,
    max_gap: float = 0.8,
) -> list[tuple[float, float, str, str | None]]:
    """合併過短的孤立字幕行（如句尾「吧」單獨成行）到相鄰行。

    FA 斷句時 MAX_WORDS 與標點切行偶爾會疊加，在子句中間切一刀，把
    句尾語助詞（吧/啊/呢/了…）留成獨立一行。此處在輸出前把這類「孤兒行」
    併回相鄰行：優先併入前一行（時間連續、同說話者），首行孤兒則併入下一行。
    含拉丁詞時以空格 join，純中文直接相接。

    預設僅併「單字」孤兒：單字幾乎都是句尾語助詞，向後併入前一行最安全；
    兩字以上可能是句首短詞，向後併易誤接，故不處理。
    """
    if not lines:
        return lines

    def _has_latin(t: str) -> bool:
        return any(c.isascii() and c.isalpha() for c in t)

    def _vlen(t: str) -> int:
        return len(t.replace(" ", ""))

    def _is_orphan(t: str) -> bool:
        # 純中文且可見字數極少才視為孤兒；含拉丁詞（英文/數字詞）不併
        return (not _has_latin(t)) and 0 < _vlen(t) <= min_chars

    def _join(a: str, b: str) -> str:
        sep = " " if (_has_latin(a) or _has_latin(b)) else ""
        return f"{a}{sep}{b}"

    merged: list[tuple[float, float, str, str | None]] = []
    for (s, e, t, spk) in lines:
        if (_is_orphan(t) and merged
                and merged[-1][3] == spk
                and s - merged[-1][1] <= max_gap):
            ps, _pe, pt, pspk = merged[-1]
            merged[-1] = (ps, e, _join(pt, t), pspk)
        else:
            merged.append((s, e, t, spk))

    # 首行仍是孤兒（無前一行可併）→ 併入下一行
    if len(merged) >= 2 and _is_orphan(merged[0][2]):
        s0, _e0, t0, spk0 = merged[0]
        s1, e1, t1, spk1 = merged[1]
        if spk0 == spk1 and s1 - merged[0][1] <= max_gap:
            merged[1] = (s0, e1, _join(t0, t1), spk1)
            merged.pop(0)

    return merged


def _merge_orphan_lines_rich(
    lines: list[tuple],
    min_chars: int = 1,
    max_gap: float = 0.8,
) -> list[tuple]:
    """`_merge_orphan_lines` 的 5-tuple 版（卡拉OK字級用）。

    行為與 4-tuple 版完全相同（同樣的孤兒判定、間隔/說話者條件），差別只在
    併行時連同第 5 元素 ``words`` 字級清單一起串接，使字幕卡與卡拉OK逐字
    高亮的斷句保持一致。
    """
    if not lines:
        return lines

    def _has_latin(t: str) -> bool:
        return any(c.isascii() and c.isalpha() for c in t)

    def _vlen(t: str) -> int:
        return len(t.replace(" ", ""))

    def _is_orphan(t: str) -> bool:
        return (not _has_latin(t)) and 0 < _vlen(t) <= min_chars

    def _join(a: str, b: str) -> str:
        sep = " " if (_has_latin(a) or _has_latin(b)) else ""
        return f"{a}{sep}{b}"

    merged: list[tuple] = []
    for (s, e, t, spk, w) in lines:
        if (_is_orphan(t) and merged
                and merged[-1][3] == spk
                and s - merged[-1][1] <= max_gap):
            ps, _pe, pt, pspk, pw = merged[-1]
            merged[-1] = (ps, e, _join(pt, t), pspk, pw + w)
        else:
            merged.append((s, e, t, spk, w))

    if len(merged) >= 2 and _is_orphan(merged[0][2]):
        s0, _e0, t0, spk0, w0 = merged[0]
        s1, e1, t1, spk1, w1 = merged[1]
        if spk0 == spk1 and s1 - merged[0][1] <= max_gap:
            merged[1] = (s0, e1, _join(t0, t1), spk1, w0 + w1)
            merged.pop(0)

    return merged


def _ts_chatllm_to_subtitle_lines(
    ts_items,
    raw_text: str,
    chunk_offset: float,
    spk: str | None,
    cc,
    simplified: bool,
    break_on_space: bool = False,
    with_words: bool = False,
):
    """字級 (word, start_sec, end_sec) + ASR 原文 → 字幕行。

    標點切行 + MAX_CHARS/MAX_WORDS 保護；word_list 直接取自字級時間軸，
    與時間 1:1 對應，後端無關（chatllm FA / Whisper 字級皆適用）。

    參數：
        ts_items: list[tuple[str, float, float]]  → (word, start_sec, end_sec)
        break_on_space: True 時把 raw_text 的「空白」也當切點。
            用於 Whisper（無標點，但以空白標記語句邊界）→ 等同 Qwen 在標點切，
            逼近 Qwen 斷句品質。Qwen/chatllm 路徑維持 False（空白僅分隔拉丁詞）。
        with_words: True 時每行多帶一個 ``words`` 字級清單（卡拉OK逐字高亮用），
            回傳 5-tuple ``(start, end, text, spk, words)``；
            ``words = [{"start": 秒, "end": 秒, "text": 顯示字}, ...]``。
            預設 False → 維持既有 4-tuple 行為（SRT/批次/端點完全不受影響）。

    回傳：
        with_words=False → list[(start, end, text, spk)]
        with_words=True  → list[(start, end, text, spk, words)]
    """
    _all_punct = _ZH_CLAUSE_END | _EN_SENT_END
    MAX_WORDS    = 8
    MAX_ZH_CHARS = MAX_CHARS
    # 弱標點門檻：緩衝不足此字數時，句內停頓（，、：…—）不切行。
    # 依據：真實 85680 字樣本，門檻 12 時行數 8827→4677（-47%），
    # ≤3 字碎片 17.6%→0%，且無超長行。見 docs/验证边界.md。
    WEAK_MIN_CHARS = 12
    # 內部一律以 5-tuple (start, end, text, spk, words) 累積，回傳前依 with_words 決定保留與否
    result: list[tuple] = []

    if not ts_items or not raw_text.strip():
        return result

    word_list = [w for (w, _s, _e) in ts_items]
    n = len(ts_items)

    # 私用區（PUA）字元防線：這些不是任何模型的合法輸出，而是下游元件把「無法
    # 映射的類別 ID」直接當成字元渲染的結果（實例：crispasr 的 qwen3 CTC 對齊器
    # 遇到其類別表外的繁體字，會輸出 U+E000+class_id）。它們對使用者是亂碼，
    # 也會污染下游（SRT／JSON／Agent）。在唯一的文字出口統一濾掉，任何引擎都受惠。

    # 繁化：text（整行）沿用既有「整行轉換」確保 SRT 輸出零變化；
    # words[].text 走「逐字轉換」（卡拉OK高亮單位）——兩者在極少數 s2twp
    # 詞組轉換情境可能有細微差異，但卡拉OK檢視只讀 words，不影響字幕卡/SRT。
    def _conv(s: str) -> str:
        s = _strip_pua(s)
        return cc.convert(s) if (not simplified and cc is not None) else s

    seg_idx:   list[int] = []   # 當前行的 ts_items 索引
    seg_words: list[str] = []   # 當前行的原始 word
    ri = 0                      # raw_text 掃描位置

    def _is_latin_word(w: str) -> bool:
        return any(c.isascii() and c.isalpha() for c in w)

    def _emit():
        nonlocal seg_idx, seg_words
        if not seg_idx:
            seg_idx = []; seg_words = []
            return
        start = chunk_offset + ts_items[seg_idx[0]][1]
        end   = chunk_offset + ts_items[seg_idx[-1]][2]
        if any(_is_latin_word(w) for w in seg_words):
            text = " ".join(seg_words)
        else:
            text = "".join(seg_words)
        text = _conv(text)
        # 字級清單：每個 ts_item → 一個高亮單位（中文＝單字、拉丁＝整詞）
        words = [
            {"start": chunk_offset + ts_items[k][1],
             "end":   chunk_offset + ts_items[k][2],
             "text":  _conv(word_list[k])}
            for k in seg_idx
        ]
        if end > start and text.strip():
            result.append((start, end, text.strip(), spk, words))
        seg_idx = []; seg_words = []

    def _over_limit() -> bool:
        if any(_is_latin_word(w) for w in seg_words):
            return len(seg_words) > MAX_WORDS
        return sum(len(w) for w in seg_words) > MAX_ZH_CHARS

    for wi in range(n):
        word = word_list[wi]

        # 在 raw_text 中前進到 word 位置；遇到標點（或 whisper 空白）→ 先切行
        #
        # 強/弱標點分級（2026-11 改）：句末標點（。？！；）是真正的斷句信號，
        # 立即切；句內停頓（，、：…—）只在當前累積夠長時才切，否則視為連接
        # 符丟棄並繼續累積。舊版「遇任何標點即切」是平均 8.7 字/行、17.6%
        # 碎片行的直接成因。
        hit_punct = False
        weak_seen = False
        while ri < len(raw_text):
            c = raw_text[ri]
            if c in _all_punct:
                if c in _STRONG_PUNCT:
                    hit_punct = True
                else:
                    weak_seen = True
                ri += 1; continue
            if c == " ":
                if break_on_space:
                    hit_punct = True   # whisper：空白＝語句邊界，視同切點
                ri += 1; continue
            break

        # 弱標點是否夠長到值得切（用「當前緩衝 + 即將加入的詞」估長度）
        if weak_seen and not hit_punct:
            pending = sum(len(w) for w in seg_words) + len(word)
            if pending >= WEAK_MIN_CHARS:
                hit_punct = True

        if hit_punct:
            _emit()

        seg_idx.append(wi)
        seg_words.append(word)

        # 跳過 word 在 raw_text 中佔用的字元（依長度計數，忽略標點/空格）
        consumed = 0
        word_len = len(word)
        while ri < len(raw_text) and consumed < word_len:
            c = raw_text[ri]
            if c in _all_punct or c == " ":
                ri += 1; continue
            ri += 1; consumed += 1

        if _over_limit():
            _emit()

    _emit()
    merged = _merge_orphan_lines_rich(result)
    if with_words:
        return merged
    return [(s, e, t, spk) for (s, e, t, spk, _w) in merged]
