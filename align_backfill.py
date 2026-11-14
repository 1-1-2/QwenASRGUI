"""align_backfill.py — 對既有產物「補做詞級對齊」（純 FA，不跑 ASR）。

背景
----
一份已經定稿的 .srt / .rich.json 若當初沒開「詞級優化」，就沒有字級時間軸，
卡拉OK 只能按字數比例插值。本模組只做強制對齊（ForcedAligner），
**不重新識別**：文字沿用 rich.json 的 segments，對齊結果寫回同一份 JSON。

為什麼不能沿用線上那條路徑
--------------------------
線上（longform_runner）用 VAD 語音組當窗，逐窗 ASR 後立刻 FA，再用「詞的
時間中點落在哪一行」把詞分回行——那條路徑有兩把不同的尺子，
`post.aligned_ratio` 之外的錯位就是這樣來的（33 行裡 30 行接錯）。

本模組改用**字符偏移**：FA 的輸出與參考文本是嚴格 1:1 同序的，
所以「第 k 行」就是「第 off..off+len(第k行) 個字」，時間只當屬性用，
不參與歸屬判斷。只有當 1:1 不成立（數字、英文、口癖被吞）時才整窗放棄，
寧可留空讓前端內插，也不寫錯位資料。

產物
----
* `<name>.rich.json`：segments[].words、align=true、post{at,win,aligned,total,lanes}
* `<name>.srt`：由 rich.json 的 segments 重建（原文無標點 → 不會再冒出逗號）
* `_backup/align/<name>.<時間戳>/`：改動前的 srt + rich.json 快照
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import app_paths

# 每窗目標長度（秒）。60s 是實測甜點：10s 要 67 分、60s 40 分、300s 94 分
# （237 分鐘音檔，FA 耗時隨長度超線性成長）。
WINDOW_SEC = 60.0
# 每處理幾窗落盤一次（原子替換）。太小會頻繁重寫 4 MB JSON，太大斷電損失多。
FLUSH_EVERY = 8

_PUNCT = set("，。！？；：、,.!?;:…—－·「」『』（）()《》〈〉\"'“”‘’ \t\r\n")


def _log(msg: str) -> None:
    pass


def _model_dir() -> Path:
    # 打包後模型在 <app>/ov_models；開發期可用環境變數指向執行目錄那一份，
    # 免得在 repo 裡再放一份 984 MB 的 .bin。
    env = os.environ.get("QWENASR_MODEL_DIR")
    if env:
        return Path(env)
    return app_paths.app_dir() / "ov_models"


def _srt_ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1_000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _clean_indices(text: str) -> list[int]:
    """去掉空白與標點後，保留字元在原字串中的下標。

    FA 對標點的處理各版本不一（可能自成一 token，也可能吞掉），
    所以「第 n 個 token ↔ 第 n 個有效字元」比「第 n 個字元」穩。
    """
    return [i for i, ch in enumerate(text) if ch not in _PUNCT]


def build_windows(segments: list[dict], target: float = WINDOW_SEC) -> list[list[int]]:
    """把連續 segment 下標切成 ~target 秒的窗，**絕不從中間切斷一行**。

    一行只有上下兩半會讓字符偏移對不上（FA 拿到的是半句），
    所以判斷點是「加入這行後是否超過 target」，超過就從這行起新窗。
    """
    out: list[list[int]] = []
    cur: list[int] = []
    for i, seg in enumerate(segments):
        if cur:
            span = float(segments[cur[-1]]["end"]) - float(segments[cur[0]]["start"])
            if span >= target:
                out.append(cur)
                cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def _extract(ff: str, audio: Path, t0: float, dur: float, dst: Path) -> bool:
    """切出一段 16k 單聲道 wav。失敗回 False（該窗留空，不影響其他窗）。"""
    import subprocess

    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{max(0.0, t0):.3f}", "-t", f"{max(0.2, dur):.3f}",
           "-i", str(audio), "-ar", "16000", "-ac", "1",
           "-c:a", "pcm_s16le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=180)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1024
    except Exception:
        return False


def _assign(texts: list[str], items: list[tuple[str, float, float]],
            t0: float) -> list[list[dict]] | None:
    """把一個窗的 FA 結果按字符偏移分給每一行。

    回傳 None 表示這個窗的 1:1 對應不成立（整個窗放棄，
    而不是把錯位的詞硬塞給某一行）。
    """
    clean: list[tuple[int, int]] = []      # (原字元下標, 該字元的第幾個有效字)
    for li, text in enumerate(texts):
        for ci in _clean_indices(text):
            clean.append((li, ci))
    if len(clean) != len(items):
        return None

    words: list[list[dict]] = [[] for _ in texts]
    for k, (li, _ci) in enumerate(clean):
        w, s, e = items[k]
        words[li].append({"word": str(w),
                          "start": round(t0 + float(s), 3),
                          "end": round(t0 + float(e), 3)})
    return words


def snapshot(srt: Path, rich: Path, tag: str = "align") -> Path:
    """改動前快照到 _backup/<tag>/<stem>.<時間戳>/，回傳目錄。

    tag 讓「補對齊」與「只重建字幕」各自的快照分開放，事後想回滾時
    不會把兩種改動混在一起。
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = srt.parent.parent / "_backup" / tag / f"{srt.stem}.{ts}"
    root.mkdir(parents=True, exist_ok=True)
    for p in (srt, rich):
        try:
            if p.exists():
                shutil.copy2(p, root / p.name)
        except OSError:
            pass
    return root


_snapshot = snapshot          # 舊名保留：既有呼叫端不必同步改


def _write_json(path: Path, segs: list[dict], meta: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"segments": segs, "notes": meta.get("notes") or [],
               "align": bool(meta.get("align")),
               "diarize": bool(meta.get("diarize"))}
    if meta.get("post"):
        payload["post"] = meta["post"]
    if meta.get("source"):
        payload["source"] = meta["source"]
    if meta.get("layout"):
        payload["layout"] = int(meta["layout"])
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def rebuild_srt(srt: Path, segments: list[dict]) -> None:
    """由 segments 重建 SRT（原子替換）。文字原樣照抄，不加任何標點。"""
    tmp = srt.with_suffix(srt.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for i, seg in enumerate(segments, 1):
            text = str(seg.get("text") or "")
            if not text:
                continue
            f.write(f"{i}\n{_srt_ts(float(seg['start']))} --> "
                    f"{_srt_ts(float(seg['end']))}\n{text}\n\n")
    os.replace(str(tmp), str(srt))


def _explode_newlines(segments: list[dict]) -> list[tuple]:
    """把含 ``\\n`` 的舊條目按換行拆回單行（layout 2 → 3 的還原步驟）。

    layout 2 的產物是「40 字一屏兩行」，換行位置本身就是上一輪按詞邊界
    算出來的切點，資訊比「去掉換行重新均分」多，所以這裡優先沿用。
    words 按字符偏移一起切。
    """
    def _wtext(w) -> str:
        return str(w.get("text") or w.get("word") or "")

    rows: list[tuple] = []
    for seg in segments:
        s = float(seg.get("start") or 0.0)
        e = float(seg.get("end") or 0.0)
        text = str(seg.get("text") or "")
        words = list(seg.get("words") or [])
        parts = text.split("\n")
        if len(parts) == 1:
            rows.append((s, e, text, seg.get("speaker"), words))
            continue
        exact = bool(words) and sum(len(_wtext(w)) for w in words) == len(
            text.replace("\n", ""))
        pos = 0
        for part in parts:
            wpart = []
            if exact:
                nxt = pos + len(part)
                acc = 0
                for w in words:
                    wacc = acc + len(_wtext(w))
                    if wacc > pos and acc < nxt:
                        wpart.append(w)
                    acc = wacc
                    if acc >= nxt:
                        break
                pos = nxt
            rows.append((s, e, part, seg.get("speaker"), wpart))
    return rows


def relayout(segments: list[dict], *, correct_time: bool = True,
             min_gap: float = 0.06) -> list[dict]:
    """把既有 segments 重排成「一條一行、每條 ≤20 字」，並用詞級時間校正時間。

    這裡只做兩件事，都是「把舊產物拉回模型」的補償：

    1. **版面**：舊產物（layout 1）是 20 字硬切留下的「主體 + 1~2 字尾巴」；
       layout 2 是「一屏兩行」。兩種都不再被支援 —— 前者短尾巴獨立閃一屏，
       後者多行條目在播放器/剪輯軟體裡支援不一致。這裡統一按
       ``ceil(字數/20)`` 均分成單行條目（詳見 ``subtitle_lines.balance_lines``）。
    2. **時間**：舊 SRT 的行時間來自 ASR 的粗略分段（實測中位偏差 0.46s，
       最大 1.24s）。rich.json 裡的 words 是 FA 的毫秒級結果，行時間直接
       取首詞 start / 末詞 end 才是它真正被說出來的時刻。

    沒有 words 的行照舊保留原時間（無依據就不改），只做版面重排。
    """
    from subtitle_lines import balance_lines, merge_tails

    rows = merge_tails(balance_lines(_explode_newlines(segments)))

    out: list[dict] = []
    for (s, e, t, spk, w) in rows:
        flat = len(t.replace("\n", ""))
        if correct_time and w and len(w) == flat:
            try:
                s = float(w[0]["start"])
                e = float(w[-1]["end"])
            except (KeyError, TypeError, ValueError, IndexError):
                pass
        rec = {"start": round(s, 3), "end": round(max(e, s + 1e-3), 3),
               "text": t, "words": w}
        if spk:
            rec["speaker"] = spk
        out.append(rec)

    # 全局單調化：跨 chunk/跨 group 拼接必然帶來重疊（實測 003 有 807 處、
    # 最深 −4.88s），這裡一次掃平。起始時間優先，壓不動才推後。
    #
    # 再由語音窗口推出顯示窗口：字幕提前 LEAD_SEC 出現，尾留 HOLD_SEC 後消失，
    # 但永遠不晚於首字發聲（詳見 subtitle_lines.apply_display_lead 的三條規則）。
    from subtitle_lines import fix_timeline, apply_display_lead

    fixed = fix_timeline(
        [(r["start"], r["end"], r["text"], r.get("speaker"),
          r.get("words") or []) for r in out],
        min_gap=min_gap,
    )
    shown = apply_display_lead(fixed)
    return [{"start": round(s, 3), "end": round(e, 3), "text": t,
             "words": w, **({"speaker": spk} if spk else {})}
            for (s, e, t, spk, w) in shown]


def _done_mask(segments: list[dict], windows: list[list[int]]) -> list[bool]:
    """上次跑到哪裡：整窗每一行都有 words 就當作已完成（續跑用）。"""
    return [all(segments[i].get("words") for i in win) for win in windows]


def inspect(srt_path: str | Path) -> dict:
    """不回音檔、只看產物：有沒有可對齊的行、時間軸多長。"""
    srt = Path(srt_path)
    rich = srt.with_suffix(".rich.json")
    out = {"ok": False, "segments": 0, "duration": 0.0, "reason": ""}
    if not rich.is_file():
        out["reason"] = "缺 .rich.json（只有字幕檔無法補對齊）"
        return out
    try:
        segs = json.loads(rich.read_text(encoding="utf-8")).get("segments") or []
    except Exception:
        out["reason"] = "詞級明細損壞"
        return out
    if not segs:
        out["reason"] = "詞級明細裡沒有字幕行"
        return out
    try:
        out["duration"] = round(float(segs[-1]["end"]), 1)
    except Exception:
        out["duration"] = 0.0
    out["segments"] = len(segs)
    out["ok"] = True
    return out


def probe(audio_path: str | Path, srt_path: str | Path,
          language: str = "Chinese") -> dict:
    """試對齊：抽 10% / 50% / 90% 三個窗各跑一次，判斷「這份文本對得上這段音」。

    為什麼要試：舊產物沒有記錄音檔 hash，長度相同不代表同一個檔案。
    FA 本身就是最可靠的内容校驗器——配錯音檔時，它輸出的字序拼不回原文，
    `_assign` 會回 None。三個窗過兩個即視為通過（片頭常有寒暄，
    和正文文本風格差很遠）。
    """
    import ffmpeg_utils

    audio = Path(audio_path)
    srt = Path(srt_path)
    rich = srt.with_suffix(".rich.json")
    out = {"ok": False, "ratio": 0.0, "tried": 0, "reason": ""}
    if not rich.is_file():
        out["reason"] = "缺 .rich.json"
        return out
    try:
        segs = json.loads(rich.read_text(encoding="utf-8")).get("segments") or []
    except Exception:
        out["reason"] = "詞級明細讀不出来"
        return out
    windows = build_windows(segs)
    if not windows:
        out["reason"] = "沒有可切分的字幕行"
        return out

    from fa_aligner import ChatLLMAligner
    ff = ffmpeg_utils.find_ffmpeg()
    if ff is None:
        out["reason"] = "找不到 ffmpeg"
        return out
    fa = ChatLLMAligner(fa_dir=_model_dir())
    if not fa.load():
        out["reason"] = "強制對齊模型不可用"
        return out

    picks = sorted({max(0, min(len(windows) - 1, int(len(windows) * f)))
                    for f in (0.1, 0.5, 0.9)})
    passed = 0
    for wi in picks:
        win = windows[wi]
        t0 = float(segs[win[0]]["start"])
        t1 = float(segs[win[-1]]["end"])
        texts = [str(segs[i].get("text") or "") for i in win]
        ref = "".join(texts)
        fd, tmp = tempfile.mkstemp(prefix="asr_api_fa_", suffix=".wav")
        os.close(fd)
        try:
            if not _extract(str(ff), audio, t0, t1 - t0 + 0.3, Path(tmp)):
                continue
            items = fa.align_chunk(tmp, ref, language) or []
            if items and _assign(texts, items, t0) is not None:
                passed += 1
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    out["tried"] = len(picks)
    out["ratio"] = round(passed / max(1, len(picks)), 3)
    out["ok"] = passed * 3 >= len(picks) * 2      # ≥2/3
    if not out["ok"]:
        out["reason"] = f"內容對不上（{passed}/{len(picks)} 窗通過）"
    return out


def run(    audio_path: str | Path,
    srt_path: str | Path,
    *,
    language: str = "Chinese",
    progress_cb=None,
    should_stop=None,
    want_lanes: int = 2,
    rebuild: bool = True,
) -> dict:
    """對一份既有產物補字級時間軸。

    Parameters
    ----------
    audio_path : 原始音檔。必須與這份字幕同源；呼叫端負責校驗。
    srt_path : 目標 .srt；同名的 .rich.json 是唯一文字來源。
    should_stop : 可選 ``() -> bool``，每個窗之間檢查（單項停止用）。
    want_lanes : 上限路數（實際路數由可用記憶體決定，1~2）。
    """
    import sysmem

    audio = Path(audio_path)
    srt = Path(srt_path)
    rich = srt.with_suffix(".rich.json")
    if not audio.is_file():
        raise FileNotFoundError(f"找不到音檔：{audio}")
    if not rich.is_file():
        raise ValueError("找不到詞級明細（.rich.json），請先完成識別再補對齊")

    try:
        data = json.loads(rich.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"詞級明細讀不出来：{e}") from e
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        raise ValueError("詞級明細裡沒有可對齊的字幕行")
    segs = [dict(s) for s in segs]
    for s in segs:
        s.setdefault("words", [])

    windows = build_windows(segs)
    if not windows:
        raise ValueError("沒有可切分的字幕行")

    from fa_aligner import ChatLLMAligner
    import ffmpeg_utils

    ff = ffmpeg_utils.find_ffmpeg()
    if ff is None:
        raise RuntimeError("找不到 ffmpeg，無法切分音訊（請確認安裝包內的 ffmpeg/ 目錄）")
    fa = ChatLLMAligner(fa_dir=_model_dir())
    if not fa.load():
        raise RuntimeError("強制對齊模型不可用（ov_models/qwen3-focedaligner-0.6b.bin "
                           "或 chatllm/main.exe 缺失）")

    lanes, avail = sysmem.align_lanes(want_lanes)
    backup = _snapshot(srt, rich)

    mask = _done_mask(segs, windows)
    todo = [i for i, d in enumerate(mask) if not d]
    total = len(windows)
    done_n = total - len(todo)
    ok_n = done_n
    failed: list[int] = []
    canceled = False

    meta = {"notes": data.get("notes") or [], "align": False,
            "diarize": bool(data.get("diarize")), "source": data.get("source")}
    post = {"at": datetime.now().isoformat(timespec="seconds"),
            "win": WINDOW_SEC, "aligned": 0, "total": total, "lanes": lanes}

    lock = threading.Lock()
    t_start = time.time()

    def _report(status: str | None = None) -> None:
        if progress_cb is None:
            return
        pct = 100.0 * (ok_n + len(failed)) / max(1, total)
        try:
            progress_cb(pct, status or f"詞級對齊 {ok_n + len(failed)}/{total}（{lanes} 路）")
        except Exception:
            pass

    _report(f"詞級對齊：{total} 窗，{lanes} 路"
            + (f"（可用記憶體 {avail:.1f} GB）" if avail else ""))

    def _work(wi: int):
        if should_stop is not None and should_stop():
            return wi, None, "stop"
        win = windows[wi]
        t0 = float(segs[win[0]]["start"])
        t1 = float(segs[win[-1]]["end"])
        texts = [str(segs[i].get("text") or "") for i in win]
        ref = "".join(texts)
        if not ref.strip():
            return wi, [[] for _ in win], None
        fd, tmp = tempfile.mkstemp(prefix="asr_api_fa_", suffix=".wav")
        os.close(fd)
        try:
            if not _extract(str(ff), audio, t0, t1 - t0 + 0.3, Path(tmp)):
                return wi, None, "extract"
            items = fa.align_chunk(tmp, ref, language) or []
            if not items:
                return wi, None, "fa"
            return wi, _assign(texts, items, t0), None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    if todo and lanes > 1:
        with ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="fa") as pool:
            futs = {pool.submit(_work, wi): wi for wi in todo}
            for fut in as_completed(futs):
                wi, words, why = fut.result()
                if why == "stop":
                    # 使用者按了停止：還沒動的窗不算失敗，也不寫空 words。
                    canceled = True
                    for f in futs:
                        f.cancel()
                    break
                with lock:
                    if words is not None:
                        for k, si in enumerate(windows[wi]):
                            segs[si]["words"] = words[k]
                        ok_n += 1
                    else:
                        failed.append(wi)
                    if (ok_n + len(failed)) % FLUSH_EVERY == 0:
                        _write_json(rich, segs, {**meta, "post": dict(post)})
                    _report()
                if should_stop is not None and should_stop():
                    canceled = True
                    for f in futs:
                        f.cancel()
                    break
    else:
        for wi in todo:
            if should_stop is not None and should_stop():
                canceled = True
                break
            wi, words, why = _work(wi)
            if why == "stop":
                canceled = True
                break
            if words is not None:
                for k, si in enumerate(windows[wi]):
                    segs[si]["words"] = words[k]
                ok_n += 1
            else:
                failed.append(wi)
            if (ok_n + len(failed)) % FLUSH_EVERY == 0:
                _write_json(rich, segs, {**meta, "post": dict(post)})
            _report()

    post.update({"aligned": ok_n, "lanes": lanes,
                 "failed": len(failed), "secs": round(time.time() - t_start, 1)})
    meta["align"] = not canceled and ok_n == total
    meta["post"] = post
    _write_json(rich, segs, meta)

    rebuilt = False
    if rebuild and not canceled:
        # 對齊補上了 words，正好順便把版面按 ceil(字數/20) 均分成單行、
        # 並用詞級時間校正行時間 —— 這兩件事都需要 words 才做得了。
        laid = relayout(segs)
        meta["layout"] = 4
        _write_json(rich, laid, {**meta, "layout": 4})
        rebuild_srt(srt, laid)
        segs = laid                     # 回傳值要反映重排後的條數與尾端時間
        rebuilt = True

    return {
        "srtPath": str(srt),
        "richPath": str(rich),
        "backupDir": str(backup),
        "totalSegments": len(segs),
        "windows": total,
        "alignedWindows": ok_n,
        "failedWindows": len(failed),
        "lanes": lanes,
        "availGb": avail,
        "ratio": round(ok_n / max(1, total), 3),
        "canceled": canceled,
        "rebuilt": rebuilt,
        "durationSec": round(float(segs[-1]["end"]), 1),
    }
