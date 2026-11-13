"""batch_queue.py - server-side batch transcription queue (Wave 4 / B4).

Why this exists
---------------
Before B4 the "batch" page was a client-side ``for`` loop: it held every File
object in the browser and called ``/api/transcribe`` one at a time. Refreshing
the page lost the queue, closing the window killed it, and ``/api/batch`` was a
stub returning empty counters.

This module owns a **persistent, server-side** queue instead:

  * files are uploaded once (``add``) into ``longform_state/batch_uploads/``;
  * a single background worker processes them serially - ASREngine holds a
    mutex, so extra workers would only queue while multiplying memory;
  * state is written to ``longform_state/batch_queue.json`` after every
    transition, so a restart resumes pending items (running -> pending);
  * progress / status changes are pushed as ``batch`` SSE events, so the UI is
    a pure renderer and can be reloaded at any time.

The worker calls ``WebBackend.transcribe()`` unchanged. That keeps the unified
streaming path, checkpoint resume, task fingerprint and optional enrichment
identical between single-file and batch runs - there is no second pipeline.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

import app_paths

# Terminal states for an item.
_DONE = "done"
_FAILED = "failed"
_CANCELED = "canceled"
_TERMINAL = {_DONE, _FAILED, _CANCELED}

# Wave 11：只接受常見音訊／影片容器。add_path() 直接引用本機檔案，必須先
# 擋掉「把任意系統檔當音檔餵給 ffmpeg」的誤用（這是本機單人工具，仍做基本校驗）。
_AUDIO_EXTS = {
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v", ".wmv",
    ".aiff", ".aif", ".amr", ".3gp",
}

# 「開始」時鎖定的執行參數。與音檔頁的設定項一一對應：
#   language / diarize / nSpeakers / align / hint  → 由前端 currentBatchOpts() 帶入
#   vadThreshold / chunkSecs                      → 切分參數面板的兩個滑桿
# trial（試跑前 5 分鐘）刻意不在此列：它只對單檔預覽有意義，
# 對整條佇列套用會產出一堆被截斷的字幕。
RUN_OPT_KEYS = ("language", "diarize", "nSpeakers", "align", "hint",
                "vadThreshold", "chunkSecs")


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


class BatchQueue:
    """Persistent serial queue with one background worker.

    Parameters
    ----------
    backend : WebBackend-like object exposing ``transcribe(opts, progress_cb)``
              and ``cancel()``.
    on_event : optional callable ``(event_name, payload)`` for SSE publishing.
    state_dir : override for tests; defaults to ``<app_dir>/longform_state``.
    """

    def __init__(self, backend, on_event=None, state_dir: Path | None = None):
        self.backend = backend
        self._on_event = on_event
        self.state_dir = Path(state_dir) if state_dir else app_paths.user_path("longform_state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir = self.state_dir / "batch_uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "batch_queue.json"

        self._lock = threading.RLock()
        self.items: list[dict] = []
        self.running = False
        self.active_id: str | None = None
        self.cancel_requested = False
        self._worker: threading.Thread | None = None
        # 本次佇列「開始」時鎖定的執行參數。舊版只在入隊時把
        # language/diarize/nSpeakers/align/hint 快照進 item.opts，
        # 而 vad/chunk 卻是每個檔開跑時現讀 settings —— 同一條佇列裡
        # 一半參數凍結、一半會中途變動，與音檔頁對不上。
        # 現在統一在 start() 收一次，整條佇列（含續跑）都用這一份。
        self.run_opts: dict = {}
        # 佇列級計時（與單檔 elapsedSec 分開，兩層各報各的）。
        # 續跑（停止→開始）時累加，不從零重算；全新一批才歸零。
        self.started_at: float | None = None
        self.elapsed_sec: float = 0.0
        self._gen = 0
        self._load()
        self._sweep_orphans()

    # ── persistence ────────────────────────────────────────────────────
    def _load(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            ro = data.get("runOpts")
            if isinstance(ro, dict):
                self.run_opts = ro
            items = data.get("items", [])
            if not isinstance(items, list):
                return
            for it in items:
                if not isinstance(it, dict) or "id" not in it:
                    continue
                # Wave 11 相容：舊版 state 沒有 ownedPath / localPath 欄位。
                # 只要 path 仍在 upload_dir 內就視為「我們複製的副本」，
                # 否則會被 _sweep_orphans 當孤兒刪掉，導致 retry 找不到檔。
                if "ownedPath" not in it:
                    try:
                        p = Path(str(it.get("path", ""))).resolve()
                        it["ownedPath"] = self.upload_dir.resolve() in p.parents
                    except OSError:
                        it["ownedPath"] = False
                    it.setdefault("localPath", not it["ownedPath"])
                # A process death leaves "running" items with no worker. Put
                # them back to pending; their SRT/checkpoint survives, so
                # transcribe() resumes from the last completed speech group.
                if it.get("status") == "running":
                    it["status"] = "pending"
                    # Wave 14／需求 3：保留 progress，別讓「程序重啟後續跑」
                    # 在界面上顯示 0%。
                # Files deleted between runs cannot be retried.
                p = it.get("path")
                if it.get("status") == "pending" and p and not Path(p).exists():
                    it["status"] = "failed"
                    it["error"] = "上傳檔案已遺失，請重新加入"
                self.items.append(it)
        except (OSError, ValueError, TypeError):
            # A corrupt state file must not prevent the app from starting.
            self.items = []

    def _is_owned(self, item: dict) -> bool:
        """這個 item 的音檔是不是我們自己複製進 upload_dir 的副本。

        外部路徑（add_path / 使用者本機檔案）永遠不能刪——那是使用者的財產。
        """
        return bool(item.get("ownedPath"))

    # ── 時長探測（Wave 14／需求 8）─────────────────────────────────────
    def _probe_duration_async(self, item_id: str):
        """入隊後在背景探測音檔時長，寫回 item 並以 SSE 廣播。

        佇列排隊時前端就能顯示「總音訊時長 / 總剩餘」，不必等第一個檔案
        真的開始識別才知道有多長。探測本身只讀檔頭（ffprobe / wave），
        不載入模型、不佔用 worker，所以即使佇列在跑也不影響識別。
        任何失敗都靜默跳過：時長只是顯示用的估算，缺了不影響流程。
        """
        def _work():
            with self._lock:
                it = next((i for i in self.items if i["id"] == item_id), None)
                path = it.get("path") if it else None
                if not path or it.get("durationSec"):
                    return
            dur = None
            try:
                from longform_runner import probe_duration
                import ffmpeg_utils
                ff = None
                try:
                    ff = str(ffmpeg_utils.find_ffmpeg() or "") or None
                except Exception:
                    ff = None
                dur = probe_duration(path, ff or "ffmpeg")
            except Exception:
                dur = None
            if not dur or dur <= 0:
                return
            with self._lock:
                cur = next((i for i in self.items if i["id"] == item_id), None)
                if not cur:
                    return
                cur["durationSec"] = round(float(dur), 1)
                cur["etaSec"] = round(float(dur), 1)   # 尚未開始：剩餘＝全長
                self._save()
                self._emit(cur, "duration")

        try:
            threading.Thread(target=_work, daemon=True,
                             name=f"probe-{item_id}").start()
        except Exception:
            pass

    def _drop_owned(self, item: dict):
        """刪掉該 item 擁有的上傳副本（僅限 upload_dir 內，防路徑穿越）。"""
        if not self._is_owned(item):
            return
        try:
            p = Path(str(item.get("path", ""))).resolve()
            root = self.upload_dir.resolve()
            if p == root or root not in p.parents:
                return
            p.unlink(missing_ok=True)
        except OSError:
            # 檔案被防毒軟體鎖住時不讓整個操作失敗；下次啟動 _sweep_orphans 會再試。
            pass
        item["ownedPath"] = False

    def _sweep_orphans(self):
        """啟動時清掉 upload_dir 裡沒有任何 item 引用的孤兒檔。

        這是 Wave 11 之前「清掉任務後殘留垃圾」的主因：remove() /
        clear_finished() 以前只從 items 移除，磁碟上的副本永遠留著。
        """
        try:
            referenced = set()
            for it in self.items:
                if self._is_owned(it):
                    try:
                        referenced.add(Path(str(it.get("path", ""))).resolve())
                    except OSError:
                        pass
            for p in self.upload_dir.iterdir():
                if p.is_file():
                    try:
                        if p.resolve() not in referenced:
                            p.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError:
            pass

    def add_path(self, src_path: str | Path, name: str | None = None,
                 opts: dict | None = None) -> dict:
        """直接引用本機檔案，不複製、不上傳（Wave 11）。

        瀏覽器 <input type=file> 出於沙箱限制拿不到本機絕對路徑，所以純
        WebView 無法「零複製」；但使用者可以手動貼上路徑（一次可多行），
        由後端校驗後直接引用。結果 SRT 仍照常落盤，原始音檔絕不更動、
        也絕不刪除。
        """
        src = Path(str(src_path)).expanduser()
        raw = str(src_path).strip()
        if raw.startswith("\\\\") or src_path and str(src_path).startswith("//"):
            raise ValueError("不支援網路路徑（UNC），請用本機磁碟路徑")
        try:
            resolved = src.resolve()
        except OSError as e:
            raise ValueError(f"路徑無效：{e}") from e
        if not resolved.is_file():
            raise ValueError(f"找不到檔案或不是一般檔案：{resolved}")
        if resolved.suffix.lower() not in _AUDIO_EXTS:
            raise ValueError(f"不支援的檔案類型：{resolved.suffix or '(無副檔名)'}")
        # 別把程式自己的狀態目錄當音檔餵進 ffmpeg
        try:
            if self.state_dir.resolve() in resolved.parents:
                raise ValueError("不可加入程式自身的狀態目錄內的檔案")
        except OSError:
            pass

        with self._lock:
            item_id = uuid.uuid4().hex[:12]
            allowed = {k: (opts or {})[k] for k in
                       ("language", "diarize", "nSpeakers", "align", "hint")
                       if k in (opts or {})}
            allowed.update({"path": str(resolved), "name": Path(name or resolved.name).name})
            item = {
                "id": item_id,
                "name": Path(name or resolved.name).name,
                "path": str(resolved),
                "status": "pending",
                "progress": 0.0,
                "error": None,
                "srtPath": None,
                "opts": allowed,
                "createdAt": _now(),
                "ownedPath": False,   # 外部檔案：任何清理都不會動它
                "localPath": True,    # UI 可標示「本機路徑」
                "durationSec": None,  # Wave 14：入隊後背景探測並以 SSE 補上
                "etaSec": None,
            }
            self.items.append(item)
            self._save()
            self._emit(item, "added")
            snapshot = dict(item)
        # 出鎖後再起探測執行緒，避免 ffprobe 在持有佇列鎖時拖慢所有 SSE。
        self._probe_duration_async(snapshot["id"])
        return snapshot

    def _save(self):
        tmp = self.state_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(
                {"version": 1, "items": self.items, "runOpts": self.run_opts},
                ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            pass

    def _emit(self, item: dict, event: str = "item"):
        payload = {"type": event, "item": dict(item), "running": self.running,
                   "activeId": self.active_id, "summary": self.summary()}
        if self._on_event:
            try:
                self._on_event("batch", payload)
            except Exception:
                pass

    # ── public API ─────────────────────────────────────────────────────
    def _freeze_elapsed(self) -> None:
        """把本輪已跑的時間併進累計值（停止／結束時呼叫）。"""
        if self.started_at:
            self.elapsed_sec += max(0.0, time.time() - self.started_at)
            self.started_at = None

    def summary(self) -> dict:
        items = self.items
        done = sum(1 for i in items if i.get("status") == _DONE)
        failed = sum(1 for i in items if i.get("status") == _FAILED)
        total = len(items)
        avg = (sum(float(i.get("progress") or 0) for i in items) / total) if total else 0.0
        # 佇列級耗時：從按下「開始」算起，跟單檔耗時是兩回事（舊版把單檔的
        # elapsedSec 拿來當佇列耗時，於是兩層顯示同一個數字）。
        elapsed = self.elapsed_sec
        if self.running and self.started_at:
            elapsed += time.time() - self.started_at
        return {"total": total, "done": done, "failed": failed,
                "active": self.active_id, "running": self.running,
                "elapsedSec": round(elapsed, 1),
                "progress": round(avg, 4)}

    def snapshot(self) -> dict:
        with self._lock:
            return {"summary": self.summary(),
                    "runOpts": dict(self.run_opts or {}),
                    "items": [dict(i) for i in self.items]}

    def add(self, src_path: Path, name: str, opts: dict | None = None) -> dict:
        """Register an already-uploaded file. The file is *moved* into the
        queue's own upload dir so the caller's temp dir can be cleaned up."""
        with self._lock:
            item_id = uuid.uuid4().hex[:12]
            ext = Path(name).suffix or ".wav"
            dest = self.upload_dir / f"{item_id}{ext}"
            src = Path(src_path)
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())
            opts = dict(opts or {})
            # Never let a client inject a different path, name, or trial flag
            # into the stored opts: only the recognised per-file fields pass.
            allowed = {k: opts[k] for k in
                       ("language", "diarize", "nSpeakers", "align", "hint")
                       if k in opts}
            allowed.update({"path": str(dest), "name": Path(str(name)).name})
            item = {
                "id": item_id,
                "name": Path(str(name)).name,
                "path": str(dest),
                "status": "pending",
                "progress": 0.0,
                "error": None,
                "srtPath": None,
                "opts": allowed,
                "createdAt": _now(),
                "ownedPath": True,    # 這是我們複製的副本，移除時要刪
                "localPath": False,
                "durationSec": None,  # Wave 14：入隊後背景探測並以 SSE 補上
                "etaSec": None,
            }
            self.items.append(item)
            self._save()
            self._emit(item, "added")
            snapshot = dict(item)
        self._probe_duration_async(snapshot["id"])
        return snapshot

    def start(self, opts: dict | None = None) -> dict:
        """開始（或續跑）佇列。

        ``opts`` 是「開始」當下的音檔頁設定快照。傳了就整條佇列都用它，
        包含停止後的續跑；不傳（CLI／舊前端）則沿用每個 item 入隊時記下的
        opts，行為與過去相同。
        """
        with self._lock:
            if self.running:
                return self.summary()
            if not any(i.get("status") == "pending" for i in self.items):
                return self.summary()
            if isinstance(opts, dict):
                merged = dict(self.run_opts or {})
                merged.update({k: opts[k] for k in RUN_OPT_KEYS if k in opts})
                self.run_opts = merged
                self._save()
            # 全新一批（沒有任何已完成項）才把佇列耗時歸零；停止後續跑則累加。
            if not any(i.get("status") == _DONE for i in self.items):
                self.elapsed_sec = 0.0
            self.started_at = time.time()
            self.cancel_requested = False
            self.running = True
            self._gen += 1
            gen = self._gen
            self._worker = threading.Thread(target=self._run, args=(gen,), daemon=True,
                                            name="batch-worker")
            self._worker.start()
            return self.summary()

    def cancel(self) -> dict:
        """暫停佇列：保留待處理項目，只中斷目前正在跑的那一個。

        Wave 11：舊版把「所有 pending」一起標成 canceled，使用者按「停止」
        再按「開始」時佇列就空了（沒有任何 pending 可跑）。現在 pending
        原封不動，running 的那個退回 pending（其 SRT/checkpoint 仍在，
        transcribe() 會從上一段接著跑），所以「停止 → 再開始」能續跑。
        """
        with self._lock:
            self.cancel_requested = True
            self.running = False
            self._freeze_elapsed()
            self._gen += 1
            for it in self.items:
                if it.get("status") == "running":
                    it["status"] = "pending"
                    it["error"] = None
            self.active_id = None
            self._save()
        try:
            self.backend.cancel()
        except Exception:
            pass
        # Wave 14／需求 17、18：取消後**立刻**安排釋放模型記憶體。
        # 後端若 worker 還卡在 transcribe() 裡，release_model_if_enabled()
        # 會把釋放登記成「待執行」，等那輪真正結束時再放 —— 不會出現
        # 「先放再佔」的抖動，也不會讓模型一直掛著不放。
        releaser = getattr(self.backend, "release_model_if_enabled", None)
        if callable(releaser):
            try:
                releaser("batch-canceled")
            except Exception:
                pass
        self._broadcast()
        return self.summary()

    def remove(self, item_id: str) -> bool:
        with self._lock:
            for idx, it in enumerate(self.items):
                if it.get("id") == item_id:
                    # Guard on the item / active id, not the `running` flag:
                    # after a restart the flag may still be False while an
                    # item is being processed.
                    if it.get("status") == "running" or self.active_id == item_id:
                        return False
                    self._drop_owned(it)          # Wave 11：連磁碟副本一起清
                    self.items.pop(idx)
                    self._save()
                    self._broadcast()
                    return True
        return False

    def move(self, item_id: str, to_index: int) -> bool:
        """Move an item to ``to_index`` in the pending order.

        Execution order is simply the list order (``_run`` picks the first
        item whose status is ``pending``), so reordering the list is all it
        takes — no separate priority field to keep in sync.

        Only ``pending`` items may be moved. A running item must not jump
        around (it is already being transcribed, and the UI shows it as the
        active row), and finished/failed items would drift away from the
        chronology the user just watched.
        """
        with self._lock:
            src = next((i for i, it in enumerate(self.items)
                        if it.get("id") == item_id), None)
            if src is None:
                return False
            if self.items[src].get("status") != "pending":
                return False
            n = len(self.items)
            # 允許 dst == n（拖到列表最末尾）。先做位置換算再判斷是否真的移動，
            # 這樣「拖到自己上半部」(dst==src) 與「拖到自己下半部」(dst==src+1)
            # 都歸一成同一個位置，不會白白觸發一次重排與廣播。
            dst = max(0, min(n, int(to_index)))
            if dst > src:
                # ``dst`` 是「移除之前那一版列表」裡的位置，pop 之後 src 之後的
                # 索引全部前移一格，所以要補回這個偏移。少了這步，往後拖會少走
                # 一格（前後互換看起來正常，跨多格就會差一位）。
                dst -= 1
            if dst == src:
                return False
            it = self.items.pop(src)
            self.items.insert(dst, it)
            self._save()
            self._broadcast()
            return True

    def promote(self, item_id: str) -> bool:
        """Move a pending item to the front of the queue (置頂)."""
        with self._lock:
            src = next((i for i, it in enumerate(self.items)
                        if it.get("id") == item_id), None)
            if src is None:
                return False
            if self.items[src].get("status") != "pending":
                return False
            # 目標是「最前面那個 pending」而不是 index 0 —— 已經在跑的那列
            # 必須留在最上面，否則執行中的檔案會突然跳到列表中間。
            first_pending = next((i for i, it in enumerate(self.items)
                                  if it.get("status") == "pending"), 0)
            if src == first_pending:
                return False
            it = self.items.pop(src)
            self.items.insert(first_pending, it)
            self._save()
            self._broadcast()
            return True

    def clear_finished(self) -> int:
        with self._lock:
            keep, removed = [], 0
            for it in self.items:
                if it.get("status") in _TERMINAL:
                    self._drop_owned(it)          # Wave 11：不再留垃圾
                    removed += 1
                else:
                    keep.append(it)
            self.items = keep
            self._save()
            self._broadcast()
            return removed

    def retry(self, item_id: str) -> bool:
        with self._lock:
            for it in self.items:
                if it.get("id") == item_id and it.get("status") in (_FAILED, _CANCELED):
                    if not Path(it.get("path", "")).exists():
                        it["error"] = "上傳檔案已遺失，請重新加入"
                        self._save()
                        return False
                    it["status"] = "pending"
                    it["progress"] = 0.0
                    it["error"] = None
                    self._save()
                    self._emit(it, "retry")
                    return True
        return False

    def _broadcast(self):
        if self._on_event:
            try:
                self._on_event("batch", {"type": "state",
                                         "summary": self.summary(),
                                         "items": [dict(i) for i in self.items],
                                         "running": self.running,
                                         "activeId": self.active_id})
            except Exception:
                pass

    def _set_scope(self, scope):
        """告訴後端「這段轉寫屬於批次」，讓 progress SSE 帶 scope。

        用 getattr 而非直接呼叫：BatchQueue 的 backend 在測試裡是
        FakeBackend，也可能是只實作 transcribe/cancel 的輕量替身。
        """
        setter = getattr(self.backend, "set_progress_scope", None)
        if callable(setter):
            try:
                setter(scope)
            except Exception:
                pass

    # ── worker ─────────────────────────────────────────────────────────
    def _run(self, gen: int):
        try:
            while True:
                with self._lock:
                    if self.cancel_requested or self._gen != gen:
                        break
                    nxt = next((i for i in self.items if i.get("status") == "pending"), None)
                    if nxt is None:
                        break
                    nxt["status"] = "running"
                    # Wave 14／需求 3：**不清零** progress —— 斷點續傳時
                    # 這個值代表上一輪已處理的百分比，續跑會從它往上走；
                    # 真的從頭開始的情況由 backend 回報 resumedFrom=1 後
                    # 在下方 _progress 裏以新值覆蓋。
                    nxt["error"] = None
                    nxt["resumedFrom"] = nxt.get("resumedFrom") or 1
                    self.active_id = nxt["id"]
                    self._save()
                    item_id = nxt["id"]
                    # 開始時鎖定的參數優先於入隊快照：同一條佇列的每個檔
                    # 都用同一組值，且與使用者在音檔頁看到的一致。
                    opts = dict(nxt.get("opts") or {})
                    opts.update(self.run_opts or {})
                    self._emit(nxt, "start")

                def _progress(pct, status, _id=item_id):
                    with self._lock:
                        cur = next((i for i in self.items if i["id"] == _id), None)
                        if cur:
                            cur["progress"] = max(0.0, min(1.0, float(pct) / 100.0))
                            cur["statusText"] = status
                            self._emit(cur, "progress")

                try:
                    if self.cancel_requested or self._gen != gen:
                        raise RuntimeError("已取消")
                    # Wave 11／問題 8：標記進度歸屬，前端才知道要畫在批次頁。
                    self._set_scope("batch")
                    result = self.backend.transcribe(opts, progress_cb=_progress)
                    with self._lock:
                        # gen 已變 = 使用者按了「停止」或又按了「開始」，
                        # 這個舊 worker 的結果不能再寫回佇列。
                        if self._gen != gen:
                            continue
                        cur = next((i for i in self.items if i["id"] == item_id), None)
                        if cur:
                            cur["status"] = _DONE
                            cur["progress"] = 1.0
                            cur["srtPath"] = result.get("srtPath") or result.get("srt")
                            cur["totalSegments"] = result.get("totalSegments")
                            cur["resumedFrom"] = int(result.get("resumedFrom") or 1)
                            cur["durationSec"] = result.get("durationSec") or cur.get("durationSec")
                            self._save()
                            self._emit(cur, "done")
                except Exception as exc:
                    with self._lock:
                        if self._gen != gen:
                            continue          # 已被取消／取代，別覆寫新狀態
                        cur = next((i for i in self.items if i["id"] == item_id), None)
                        if cur:
                            cur["status"] = _CANCELED if self.cancel_requested else _FAILED
                            cur["error"] = str(exc).lstrip("⚠ ").strip()[:300]
                            self._save()
                            self._emit(cur, "error")
                    if not self.cancel_requested:
                        traceback.print_exc()
                finally:
                    self._set_scope(None)
                    with self._lock:
                        if self._gen == gen:
                            self.active_id = None
            with self._lock:
                # 只有「現任」worker 能收網；舊 worker 不能把新 worker 的
                # running 改成 False，否則「停止後立刻再開始」會立刻顯示停了。
                if self._gen == gen:
                    self.running = False
                    self._freeze_elapsed()
                    self._save()
                    self._broadcast()
        except Exception:
            with self._lock:
                if self._gen == gen:
                    self.running = False
                    self._freeze_elapsed()
                    self._save()
            traceback.print_exc()
        finally:
            # Wave 12／問題 1：整隊（或取消）結束才釋放模型，不在每個 item
            # 之間釋放 —— 否則 8 個檔會各付一次數十秒的模型編譯時間。
            # 用 getattr 相容 FakeBackend / 只實作 transcribe 的測試替身。
            releaser = getattr(self.backend, "release_model_if_enabled", None)
            if callable(releaser):
                try:
                    releaser("batch-finished")
                except Exception:
                    pass
