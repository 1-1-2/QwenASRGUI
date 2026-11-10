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
import threading
import traceback
import uuid
from pathlib import Path

import app_paths

# Terminal states for an item.
_DONE = "done"
_FAILED = "failed"
_CANCELED = "canceled"
_TERMINAL = {_DONE, _FAILED, _CANCELED}


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
        self._load()

    # ── persistence ────────────────────────────────────────────────────
    def _load(self):
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            items = data.get("items", [])
            if not isinstance(items, list):
                return
            for it in items:
                if not isinstance(it, dict) or "id" not in it:
                    continue
                # A process death leaves "running" items with no worker. Put
                # them back to pending; their SRT/checkpoint survives, so
                # transcribe() resumes from the last completed speech group.
                if it.get("status") == "running":
                    it["status"] = "pending"
                    it["progress"] = 0
                # Files deleted between runs cannot be retried.
                p = it.get("path")
                if it.get("status") == "pending" and p and not Path(p).exists():
                    it["status"] = "failed"
                    it["error"] = "上傳檔案已遺失，請重新加入"
                self.items.append(it)
        except (OSError, ValueError, TypeError):
            # A corrupt state file must not prevent the app from starting.
            self.items = []

    def _save(self):
        tmp = self.state_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(
                {"version": 1, "items": self.items},
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
    def summary(self) -> dict:
        items = self.items
        done = sum(1 for i in items if i.get("status") == _DONE)
        failed = sum(1 for i in items if i.get("status") == _FAILED)
        total = len(items)
        avg = (sum(float(i.get("progress") or 0) for i in items) / total) if total else 0.0
        return {"total": total, "done": done, "failed": failed,
                "active": self.active_id, "running": self.running,
                "progress": round(avg, 4)}

    def snapshot(self) -> dict:
        with self._lock:
            return {"summary": self.summary(),
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
            }
            self.items.append(item)
            self._save()
            self._emit(item, "added")
            return dict(item)

    def start(self) -> dict:
        with self._lock:
            if self.running:
                return self.summary()
            if not any(i.get("status") == "pending" for i in self.items):
                return self.summary()
            self.cancel_requested = False
            self.running = True
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="batch-worker")
            self._worker.start()
            return self.summary()

    def cancel(self) -> dict:
        """Cancel the active item and drop pending ones; done items stay."""
        with self._lock:
            self.cancel_requested = True
            for it in self.items:
                if it.get("status") in ("pending", "running"):
                    it["status"] = _CANCELED
                    it["error"] = None
            self.active_id = None
            self._save()
        try:
            self.backend.cancel()
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
                    self.items.pop(idx)
                    self._save()
                    self._broadcast()
                    return True
        return False

    def clear_finished(self) -> int:
        with self._lock:
            keep = [i for i in self.items if i.get("status") not in _TERMINAL]
            removed = len(self.items) - len(keep)
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

    # ── worker ─────────────────────────────────────────────────────────
    def _run(self):
        try:
            while True:
                with self._lock:
                    if self.cancel_requested:
                        break
                    nxt = next((i for i in self.items if i.get("status") == "pending"), None)
                    if nxt is None:
                        break
                    nxt["status"] = "running"
                    nxt["progress"] = 0.0
                    nxt["error"] = None
                    self.active_id = nxt["id"]
                    self._save()
                    item_id = nxt["id"]
                    opts = dict(nxt.get("opts") or {})
                    self._emit(nxt, "start")

                def _progress(pct, status, _id=item_id):
                    with self._lock:
                        cur = next((i for i in self.items if i["id"] == _id), None)
                        if cur:
                            cur["progress"] = max(0.0, min(1.0, float(pct) / 100.0))
                            cur["statusText"] = status
                            self._emit(cur, "progress")

                try:
                    if self.cancel_requested:
                        raise RuntimeError("已取消")
                    result = self.backend.transcribe(opts, progress_cb=_progress)
                    with self._lock:
                        cur = next((i for i in self.items if i["id"] == item_id), None)
                        if cur:
                            cur["status"] = _DONE
                            cur["progress"] = 1.0
                            cur["srtPath"] = result.get("srtPath")
                            cur["totalSegments"] = result.get("totalSegments")
                            self._save()
                            self._emit(cur, "done")
                except Exception as exc:
                    with self._lock:
                        cur = next((i for i in self.items if i["id"] == item_id), None)
                        if cur:
                            cur["status"] = _CANCELED if self.cancel_requested else _FAILED
                            cur["error"] = str(exc).lstrip("⚠ ").strip()[:300]
                            self._save()
                            self._emit(cur, "error")
                    if not self.cancel_requested:
                        traceback.print_exc()
                finally:
                    with self._lock:
                        self.active_id = None
            with self._lock:
                self.running = False
                self._save()
                self._broadcast()
        except Exception:
            with self._lock:
                self.running = False
                self._save()
            traceback.print_exc()
