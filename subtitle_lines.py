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

# ── 排版模型：一條一行，但「按句均分」而不是「塞滿為止」（2026-11 改）──
#
# 舊模型的死角不是「20 字太少」，而是**分配方式**：邊掃邊累積、超過 20
# 才切，於是 21 字的話被切成 20 + 1，那 1 個字單獨閃 0.6 秒。實測 002
# 有 268 處、003 有 215 處這種「主體 + 短尾巴」，且硬切常落在兩字詞中間
# （「具｜体」「轉｜化」）。
#
# 關鍵觀察：條數本來就是 ceil(L/20)，任何方案都逃不掉；能動的只有**切點**。
# 所以改成「先算條數 k = ceil(L/20)，再把 L 均分成 k 段」（21 字 → 11+10，
# 41 字 → 14+14+13）。條數不變、時長不變，只是把尾巴攤回前面幾條。
# 均分點再往詞邊界、聲學停頓上吸附，順帶治好劈詞。
#
# 為什麼不做「一條兩行」（40 字一屏）：播放器/剪輯軟體對多行條目的支援
# 不一致，且導出後常被壓成一行導致溢出。這裡強制一條只有一行。
MIN_SPLIT_CHARS = 6      # 均分時每段的軟下限（只影響評分權重，不強制）
MAX_SEG_CHARS   = 80     # 細行累積兜底上限；超過就先落一段再均分（防無標點長串）
_AVG_CHARS_PER_SEC = 5.9  # 中文口語語速，用於「一屏停留過久」的軟約束


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


def split_sentences(text: str, max_chars: int = MAX_CHARS,
                   weak_min: int = 12) -> list[str]:
    """按標點切句、去標點，每句一行（FA 不可用時的兜底路徑）。

    強標點（。？！；!?;）立即切；弱標點（，、：…—）只在緩衝 >= ``weak_min``
    字時切，否則丟掉標點繼續累積 —— 不然「呃，」「對吧，」都會變成獨立一行
    （2026-11 實測：門檻 12 時行數 8827→4677、≤3 字碎片 →0）。

    英文按整詞收、詞前補空格；中文/數字逐字累積，到 ``max_chars`` 硬切。

    這是三個引擎共用的唯一一份。舊版桌面 / GPU / chatllm 各存一份，規則還
    不一樣（GPU 版見標點就切），同一段音頻會切出不同字幕。
    """
    if "<asr_text>" in text:              # chatllm 原始輸出前綴
        text = text.split("<asr_text>", 1)[1]
    text = text.strip()
    if not text:
        return []

    punct = _ZH_CLAUSE_END | _EN_SENT_END
    lines: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        ch = text[i]

        if ch in punct:
            if ch in _STRONG_PUNCT or len(buf.strip()) >= weak_min:
                if buf.strip():
                    lines.append(buf.strip())
                buf = ""
            i += 1
            continue

        if ch.isalpha() and ord(ch) < 128:        # 英文整詞，詞界補空格
            j = i
            while j < len(text) and text[j].isalpha() and ord(text[j]) < 128:
                j += 1
            word = text[i:j]
            prefix = " " if buf and not buf.endswith(" ") else ""
            if len(buf) + len(prefix) + len(word) > max_chars and buf.strip():
                lines.append(buf.strip())
                buf = word
            else:
                buf += prefix + word
            i = j
            continue

        if ch == " ":                             # 保留分詞間距
            if buf and not buf.endswith(" "):
                buf += " "
            i += 1
            if len(buf.rstrip()) >= max_chars:
                lines.append(buf.strip())
                buf = ""
            continue

        buf += ch
        i += 1
        if len(buf) >= max_chars:
            # 舊版這裡還有一段「回退到最近弱標點」的邏輯 —— 死代碼：標點
            # 在上面的分支就 continue 了，buf 裡不可能有標點，cut 恆為 -1。
            lines.append(buf.strip())
            buf = ""

    if buf.strip():
        lines.append(buf.strip())
    return [l for l in lines if l.strip()]


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
            (f"{spk}：{t}" if spk else t).replace("\n", "")
            for (_s, _e, t, spk) in lines
        )
    # 純文字稿的目標是「連續可讀」：兩行版面的換行在這裡只是版位，
    # 不是語意斷點，攤平才不會把一句話從中間截斷。
    return "".join(str(t).replace("\n", "") for (_s, _e, t, _spk) in lines)


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
    lines: list[tuple],
    min_chars: int = 1,
    max_gap: float = 0.8,
) -> list[tuple]:
    """把過短的孤立行（如句尾「吧」「了」單獨成行）併回相鄰行。

    4/5/6 元組通用：第 5 元素 ``words`` 會一起串接（卡拉OK 逐字高亮必須與
    字幕卡用同一套斷句），第 6 元素原樣保留；**輸出的欄位數與輸入相同**，
    呼叫方不必知道自己拿的是哪一種。

    只併「單字」孤兒：單字幾乎都是句尾語助詞，向前併最安全；兩字以上可能
    是句首短詞，向前併會把兩句話接錯。含拉丁字母的行不併（英文/數字詞被
    切開時，空格拼接的語義和中文不同）。
    """
    if not lines:
        return lines
    width = len(lines[0])

    def _has_latin(t: str) -> bool:
        return any(c.isascii() and c.isalpha() for c in t)

    def _is_orphan(t: str) -> bool:
        return not _has_latin(t) and 0 < len(t.replace(" ", "")) <= min_chars

    def _join(a: str, b: str) -> str:
        sep = " " if (_has_latin(a) or _has_latin(b)) else ""
        return f"{a}{sep}{b}"

    def _unpack(ln):
        t = str(ln[2]).replace("\n", "")
        words = list(ln[4]) if width > 4 and ln[4] else []
        return (ln[0], ln[1], t, ln[3], words, tuple(ln[5:]))

    def _pack(rec):
        s, e, t, spk, words, extra = rec
        return (s, e, t, spk, words) + extra if width > 4 else (s, e, t, spk)

    out: list[tuple] = []
    for rec in (_unpack(ln) for ln in lines):
        s, e, t, spk, words, _extra = rec
        if _is_orphan(t) and out and out[-1][3] == spk and s - out[-1][1] <= max_gap:
            ps, _pe, pt, pspk, pw, pex = out[-1]
            out[-1] = (ps, e, _join(pt, t), pspk, pw + words, pex)
        else:
            out.append(rec)

    # 首行仍是孤兒（無前一行可併）→ 併入下一行
    if len(out) >= 2 and _is_orphan(out[0][2]):
        s0, _e0, t0, spk0, w0, _x0 = out[0]
        s1, e1, t1, spk1, w1, x1 = out[1]
        if spk0 == spk1 and s1 - out[0][1] <= max_gap:
            out[1] = (s0, e1, _join(t0, t1), spk1, w0 + w1, x1)
            out.pop(0)

    return [_pack(rec) for rec in out]


def _splits_latin(t: str, k: int) -> bool:
    """切点 k 是否落在拉丁词中间（"hello" 切成 "hel|lo"）。"""
    if k <= 0 or k >= len(t):
        return True
    a, b = t[k - 1], t[k]
    return a.isascii() and a.isalpha() and b.isascii() and b.isalpha()


_JIEBA = None          # None=還沒試過、False=不可用、module=可用
_WORD_BOUNDS_CACHE: dict[str, frozenset] = {}


def _word_bounds(text: str) -> frozenset:
    """中文詞邊界（字符下標集合，累計長度）。

    FA 對中文是「一字一 token」，它給得出時間卻給不出詞。要在兩行之間
    「不劈詞」，就只能自己分詞。jieba 是純 Python、離線、無模型下載，
    代價是發行包多一份 ~5 MB 的詞典；裝不上時這裡回傳空集合，
    ``_layout_two_lines`` 會自動退化成「停頓 + 均衡」，功能不受影響。

    為什麼不用「兩字詞表 + 最大匹配」自製：實測「內容」「具體」「轉化」
    這類高頻詞被劈，正是舊版 20 字硬切的病灶，靠幾千個詞的臨時表治不好。
    """
    global _JIEBA
    if not text:
        return frozenset()
    hit = _WORD_BOUNDS_CACHE.get(text)
    if hit is not None:
        return hit
    if _JIEBA is None:
        try:
            import jieba as _jb
            _JIEBA = _jb
        except Exception:
            _JIEBA = False
    if _JIEBA is False:
        return frozenset()          # 不進快取：裝上依賴後不必重啟

    bounds, n = set(), 0
    try:
        for w in _JIEBA.cut(text):
            n += len(w)
            bounds.add(n)
    except Exception:
        return frozenset()
    if len(_WORD_BOUNDS_CACHE) > 8192:
        _WORD_BOUNDS_CACHE.clear()
    bs = frozenset(bounds)
    _WORD_BOUNDS_CACHE[text] = bs
    return bs


def merge_tails(
    lines: list[tuple],
    *,
    tail_max: int = 3,
    soft_max: int = 24,
    max_gap: float = 0.60,
) -> list[tuple]:
    """把 ≤``tail_max`` 字的短尾巴併回前一條（必要時併入下一條）。

    這解決的是「20 字硬切 + 尾巴」這個歷史包袱：舊產物裡
    「…就是我刚刚可能刚进来的时候没有那么大声说」20 字 + 「话」1 字
    本來是兩個 segment，``balance_lines`` 只按單條均分，不會回頭併。

    為什麼允許超過 20 字（``soft_max = 24``）：
    20 是版面建議值，不是硬約束；而「1 字單獨閃 0.6 秒」是實打實的觀感
    災難。用「21~24 字一條」換「不出現 1 字屏」是划算的，且不改變任何
    語義邊界——文字順序、時間跨度都原樣保留，只是合併成一條。

    併入方向：**間隙小 → 併前**（尾巴通常是前一句被切斷的末字，「說｜話」）；
    **間隙大 → 併後**（停頓兩秒後單獨說的「行」「對」「嗯」是下一句的應答語，
    跟前一句連起來會讀成反問）。首條沒有前一條時一律併後。
    """
    if not lines:
        return lines

    nf = len(lines[0])

    def _get(ln):
        return (float(ln[0]), float(ln[1]), str(ln[2]).replace("\n", ""),
                ln[3] if len(ln) > 3 else None,
                list(ln[4]) if (len(ln) > 4 and ln[4]) else [],
                bool(ln[5]) if len(ln) > 5 else False)

    def _join(a, b):
        """拼兩條；帶拉丁詞時補一個空格（與其他拼接點一致）。"""
        sep = " " if (a[-1:].isascii() and a[-1:].isalpha()
                      or b[:1].isascii() and b[:1].isalpha()) else ""
        return a + sep + b

    out: list[tuple] = []
    pending: tuple | None = None      # 間隙大、準備併入下一條的短條目

    for ln in lines:
        s, e, t, spk, w, hard = _get(ln)

        if pending is not None:
            ps, pe, pt, pspk, pw, ph = pending
            if pspk == spk and len(_join(pt, t)) <= soft_max:
                out.append((ps, e, _join(pt, t), spk, pw + w, hard))
            else:
                out.append(pending)
                out.append((s, e, t, spk, w, hard))
            pending = None
            continue

        if len(t) <= tail_max and not hard:
            # 先試併前：間隙夠小才併（大間隙代表換氣/換人，併了會讀錯）
            if out:
                ps, pe, pt, pspk, pw, _ph = out[-1]
                if (pspk == spk and len(_join(pt, t)) <= soft_max
                        and s - pe <= max_gap):
                    out[-1] = (ps, e, _join(pt, t), pspk, pw + w, hard)
                    continue
            # 併前不行 → 掛起，併入下一條
            pending = (s, e, t, spk, w, hard)
            continue

        out.append((s, e, t, spk, w, hard))

    if pending is not None:
        ps, pe, pt, pspk, pw, ph = pending
        if (out and out[-1][3] == pspk
                and len(_join(out[-1][2], pt)) <= soft_max):
            last = out[-1]
            out[-1] = (last[0], pe, _join(last[2], pt), pspk, last[4] + pw, ph)
        else:
            out.append(pending)

    return [x[:nf] for x in out]


def fix_timeline(
    lines: list[tuple],
    *,
    min_gap: float = 0.04,
    min_dur: float = 0.30,
) -> list[tuple]:
    """全局单调化：保證 ``start[i] >= end[i-1] + min_gap``（任何時刻只有一條字幕）。

    為什麼需要這一趟：字幕不是一次算完的。VAD 把長音檔切成 speech group，
    每個 group（以及超長 group 再切的 chunk）**各自呼叫 line_splitter，
    各自從自己的 t0 開始鋪時間**。而 group 之間的音訊本來就有重疊
    （``_TAIL_SEC = 0.35`` 的尾段補捉 + ``_PAD_CH`` 的前後 padding），
    於是後一個 group 的 t0 會早於前一個 group 的 t1 —— 實測 003 有 807 處
    負間隔（10.1%），且數值全是 0.08 的整數倍（−0.24/−0.32/−0.40/−0.48），
    正是 ``GAP_SEC`` 被重複計算的痕跡。

    取舍順序（依使用者的兩個硬要求排）：
      1. **起始時間優先**：能靠壓前一條的 ``end`` 解決的，絕不動 ``start``。
         前一條也不是無限可壓，下限 ``min_dur``（預設 0.3s，短於此看不清）。
      2. **壓不動才推後**：前一條已經很短時，把當前條的 ``start`` 推到
         ``end[i-1] + min_gap``。這時起點會晚一點，但寧可晚也不要重疊。
      3. ``end`` 必須大於 ``start``：資料裡確實有 ``end < start`` 的壞行
         （ASR 分段錯序），一律修成 ``start + 0.2``，不讓它消失。

    行格式與 ``balance_lines`` 相同（4/5/6-tuple 皆可），回傳同樣欄位數。
    """
    if not lines:
        return lines

    nfields = len(lines[0])
    out: list[tuple] = []

    def _mk(ln, s: float, e: float):
        return (s, e) + tuple(ln[2:])

    for ln in lines:
        s = float(ln[0])
        e = float(ln[1])
        if e <= s:                     # 壞行（end <= start）：給一個最小可見長度
            e = s + 0.2

        if out:
            prev = out[-1]
            need = prev[1] + min_gap
            if s < need:
                # 1) 先壓前一條的 end（起始時間優先，一個字都不動）
                cut = s - min_gap
                if cut - prev[0] >= min_dur:
                    out[-1] = _mk(prev, prev[0], max(prev[0] + min_dur, cut))
                else:
                    # 2) 壓不動 → 推後當前條的 start
                    s = prev[1] + min_gap
                    if e <= s:
                        e = s + 0.2
        out.append(_mk(ln, s, e))

    return out


# ── 顯示窗口：字幕必須「不遲到」（2026-11 加）──────────────────────────
#
# 使用者的底線不是「發聲的同時字幕出現」，而是「話還沒出口，字幕已經在了」。
# 實測 003 前 120 秒（54 條，已用 FA 取得毫秒級真值）：字幕出現比首字發聲
# 晚中位 0.43 秒，69% 的條目遲到超過 0.1 秒，最嚴重一條遲到 6.9 秒。
#
# 遲到的根因不是「忘了加提前量」，而是行的 start 本來就是估算的（沒有詞級
# 時間時按字數比例鋪滿 ASR 分組）。所以這一層必須跑在詞級校正**之後**。
LEAD_SEC      = 0.20   # 提前量：字幕比首字發聲早這麼久出現
HOLD_SEC      = 0.25   # 尾留：末字說完後再停留這麼久，夠讀完
MIN_ONSCREEN  = 0.12   # 一屏最短可見時間，短於此會像閃屏


def apply_display_lead(
    lines: list[tuple],
    *,
    lead: float = LEAD_SEC,
    hold: float = HOLD_SEC,
    min_gap: float = 0.04,
) -> list[tuple]:
    """把「語音窗口」轉成「顯示窗口」，保證字幕**永遠不晚於**首字發聲。

    三條規則，逐條可驗算：

      1. 出現時刻 ``appear = onset - lead_i``，其中
         ``lead_i = min(lead, max(0, gap_i - min_gap))``。
         提前量只能從「上一條說完」到「本條開口」之間那段靜音裡擠 ——
         連續說話（gap≈0）時 ``lead_i = 0``，字幕與首字**同時**切換。
         這是物理限制：上一條的字還沒說完，不能提前把它撤掉。
      2. 消失時刻 ``vanish = min(off + hold, appear[i+1])``。
         下一條要出現就必須讓位，所以尾留是「有富餘才給」。
      3. 由 1 可推出 ``appear[i] >= off[i-1]``（因為 ``lead_i <= gap_i``），
         所以顯示窗口天然互不重疊，不需要再跑一趟去重。

    推論：**字幕遲到的唯一可能是「上一條佔著不放」**，而那正是規則 2
    要處理的情況 —— 尾留會被下一個出現時刻自動截短。所以只要 words 準，
    「話說完字幕才出」不會再發生；最差情況是與首字同幀切換。

    只改 ``start``/``end``，不動文字、不動 words（words 是語音真值，
    留著才能在重排時還原語音窗口）。行格式 4/5/6-tuple 皆可，欄位數不變。
    """
    if not lines:
        return lines

    n = len(lines)
    nfields = len(lines[0])
    on = [float(ln[0]) for ln in lines]
    off = [float(ln[1]) for ln in lines]

    appear: list[float] = []
    for i in range(n):
        if i == 0:
            room = lead                      # 首條沒有前一條，給足提前量
        else:
            room = min(lead, max(0.0, (on[i] - off[i - 1]) - min_gap))
        appear.append(max(0.0, on[i] - room))

    out: list[tuple] = []
    for i, ln in enumerate(lines):
        s = appear[i]
        e = off[i] + hold
        if i + 1 < n:
            e = min(e, appear[i + 1])        # 下一條要出現 → 立刻讓位
        if e < s + MIN_ONSCREEN:
            e = max(s + 0.05, e)             # 再擠也要看得見，哪怕極短
        out.append((s, e) + tuple(ln[2:])[:nfields - 2])
    return out


def _word_text(w) -> str:
    """取詞物件裡的顯示文字。兩種格式並存：字幕行用 ``text``，對齊回填用 ``word``。"""
    if isinstance(w, dict):
        return str(w.get("text") or w.get("word") or "")
    return str(w)


def _words_flat_len(words) -> int:
    """words 攤平後的字符數（用來判斷它與行文本是否嚴格 1:1 對應）。"""
    return sum(len(_word_text(w)) for w in words) if words else 0


def _gap_at(words, k: int) -> float:
    """第 k 個字符之前的聲學停頓（秒）。取不到就當 0。"""
    try:
        return max(float(words[k]["start"]) - float(words[k - 1]["end"]), 0.0)
    except (KeyError, TypeError, ValueError, IndexError):
        return 0.0


def _split_offsets(t: str, words=None,
                   max_chars: int = MAX_CHARS) -> list[int]:
    """把一段文字切成每段 ≤ ``max_chars`` 的**均分**切點（不含 0 與 len）。

    與舊版「塞滿 20 字再硬切」的差別只在一個地方，但決定性：
    舊版對 21 字的句子只切一刀、而且切在第 20 字（20+1）；
    這裡先算 ``k = ceil(n / max_chars)``，再把 n 均分成 k 段（11+10）。

    為什麼是均分而不是「貪心 + 避免尾巴」：均分是唯一能保證
    「最長段與最短段之差 ≤ 1 字」的策略，短尾巴在數學上就不會出現；
    而是先切後補的方案（切完發現尾巴太短再往回收）會和停頓判斷打架。

    評分 = 偏离均分點的程度 − 詞邊界獎勵 − 停頓獎勵。
    中文在 FA 眼裡一字一 token，詞邊界只能靠 ``_word_bounds``（jieba）；
    分詞不可用時退化成「停頓 + 均分」，仍然不會劈出短尾巴。
    """
    n = len(t)
    if n <= max_chars:
        return []

    k = (n + max_chars - 1) // max_chars      # ceil，條數與舊版完全一致
    bounds = _word_bounds(t)
    ws = words if (words and _words_flat_len(words) == n) else None

    cuts: list[int] = []
    start = 0
    for j in range(k - 1):
        rest_cuts = k - 1 - j                 # 這一刀之後還要切幾刀
        pieces = rest_cuts + 1                # 剩下的字要再分成幾段
        rest = n - start
        # 本段長度必須讓「剩下的字」还能被剩下的段裝下（每段 ≤max_chars、≥1）
        lo = max(1, rest - rest_cuts * max_chars)
        hi = min(max_chars, rest - rest_cuts)
        target = rest / pieces
        best, best_score = None, None
        for length in range(lo, hi + 1):
            c = start + length
            if _splits_latin(t, c):
                continue                       # 不在拉丁詞中間落刀
            score = -abs(length - target)
            if bounds and c in bounds:
                score += 2.0                   # 詞邊界：優先
            if ws is not None:
                score += min(_gap_at(ws, c), 0.4) * 6.0   # 停頓：次之
            if best_score is None or score > best_score:
                best, best_score = c, score
        if best is None:
            # 極端情況（整段都是拉丁詞且每段只有 1~2 字符）→ 退回最均衡點
            best = min(max(start + int(round(target)), start + lo), start + hi)
        cuts.append(best)
        start = best
    return cuts


def _slice_words(words, a: int, b: int) -> list:
    """按字符區間 [a, b) 切 words（僅在嚴格 1:1 對應時呼叫）。"""
    out, pos = [], 0
    for w in words:
        nxt = pos + len(_word_text(w))
        if nxt > a and pos < b:
            out.append(w)
        pos = nxt
        if pos >= b:
            break
    return out


def balance_lines(
    lines: list[tuple],
    *,
    max_chars: int = MAX_CHARS,
) -> list[tuple]:
    """把每條細行**均分**成 ≤ ``max_chars`` 字的單行條目（絕不含 ``\\n``）。

    行格式：``(start, end, text, spk[, words[, hard_end]])``，欄位數不變、
    順序不變，逐條展開後仍然是同樣欄位數的 tuple。

    這是「一條一行」模型的守門人：任何輸入都不會在輸出裡留下換行。
    舊版這裡是 ``pack_two_lines``（把細行併成 40 字一屏兩行），已廢除 —
    多行條目在播放器/剪輯軟體裡支援不一致，導出後常被壓成一行而溢出。

    時間：有可 1:1 對應的 words 就取「首詞 start / 末詞 end」（FA 的毫秒級
    結果）；沒有就按字符比例在原區間內插（無依據時至少保持單調）。
    """
    if not lines:
        return lines

    nfields = len(lines[0])
    out: list[tuple] = []

    for ln in lines:
        s = float(ln[0])
        e = float(ln[1])
        text = str(ln[2]).replace("\n", "")
        spk = ln[3] if len(ln) > 3 else None
        words = list(ln[4]) if (len(ln) > 4 and ln[4]) else []
        hard = bool(ln[5]) if len(ln) > 5 else False
        n = len(text)

        if not n:
            rec = (s, e, text, spk, words, hard)[:nfields]
            out.append(rec)
            continue

        cuts = _split_offsets(text, words or None, max_chars)
        if not cuts:
            rec = (s, e, text, spk, words, hard)[:nfields]
            out.append(rec)
            continue

        exact = bool(words) and _words_flat_len(words) == n
        pts = [0] + cuts + [n]
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            piece = text[a:b]
            if exact:
                wsub = _slice_words(words, a, b)
                try:
                    ps = float(wsub[0]["start"])
                    pe = float(wsub[-1]["end"])
                except (KeyError, TypeError, ValueError, IndexError):
                    ps = s + (e - s) * a / n
                    pe = s + (e - s) * b / n
            else:
                wsub = []
                ps = s + (e - s) * a / n
                pe = s + (e - s) * b / n
            if pe <= ps:
                pe = ps + 0.001
            out.append(
                (ps, pe, piece, spk, wsub, hard and i == len(pts) - 2)[:nfields])
    return out


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
    # 這裡只是「細行」的兜底上限，不是最終版面：超過就先落一段，之後
    # balance_lines 再按 ceil(L/20) 均分。放寬到 MAX_SEG_CHARS 是為了讓
    # 均分有自由度——若在這裡就硬切在 20 字，尾巴問題又會重生。
    MAX_ZH_CHARS = MAX_SEG_CHARS
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

    def _emit(hard: bool = False):
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
            # 第 6 位 hard：這一段是以句末標點結束的。排版層據此決定
            # 「這裡不准和下一個細行併成一條」——否則兩句話會被讀成一句。
            result.append((start, end, text.strip(), spk, words, hard))
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
        hit_strong = False
        weak_seen = False
        while ri < len(raw_text):
            c = raw_text[ri]
            if c in _all_punct:
                if c in _STRONG_PUNCT:
                    hit_punct = True
                    hit_strong = True
                else:
                    weak_seen = True
                ri += 1; continue
            if c == " ":
                if break_on_space:
                    hit_punct = True   # whisper：空白＝語句邊界，視同切點
                    hit_strong = True
                ri += 1; continue
            break

        # 弱標點是否夠長到值得切（用「當前緩衝 + 即將加入的詞」估長度）
        if weak_seen and not hit_punct:
            pending = sum(len(w) for w in seg_words) + len(word)
            if pending >= WEAK_MIN_CHARS:
                hit_punct = True

        if hit_punct:
            _emit(hard=hit_strong)

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
    merged = _merge_orphan_lines(result)
    balanced = merge_tails(balance_lines(merged))
    if with_words:
        return [(a, b, c, d, e) for (a, b, c, d, e, _h) in balanced]
    return [(a, b, c, d) for (a, b, c, d, _e, _h) in balanced]
