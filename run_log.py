"""run_log.py - bounded, structured runtime log (Wave 4 / H1).

Why
---
Diagnostics used to live only in ``print()`` calls (invisible in the windowed
EXE) and in a browser-local ``logLine()`` div (lost on reload, invisible to
the LAN endpoint). H1 adds one server-side ring buffer that:

  * keeps the last N entries in memory (bounded - a 5-hour lecture must not
    grow a list forever);
  * publishes each entry as a ``log`` SSE event for the live view;
  * is readable via ``GET /api/logs`` so a reloaded page can restore history;
  * is also fed by download / status / progress events, so the log explains
    what the backend actually did, not just what the UI clicked.

The buffer never holds model weights or audio data - messages only.
"""
from __future__ import annotations

import datetime
import threading
from collections import deque


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


class RunLog:
    """Thread-safe bounded log with an optional SSE publisher."""

    LEVELS = {"debug", "info", "warn", "error"}

    def __init__(self, maxlen: int = 300, on_event=None):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._on_event = on_event
        self._seq = 0

    def add(self, message, level: str = "info", source: str = "backend") -> dict:
        message = str(message).strip()
        if not message:
            return {}
        if level not in self.LEVELS:
            level = "info"
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": _now(), "level": level,
                     "source": source, "message": message[:1000]}
            self._buf.append(entry)
        if self._on_event:
            try:
                self._on_event("log", entry)
            except Exception:
                pass
        return entry

    def snapshot(self, limit: int | None = None) -> dict:
        with self._lock:
            items = list(self._buf)
        total = len(items)
        if limit is not None and limit > 0:
            items = items[-int(limit):]
        return {"items": items, "total": total,
                "capacity": self._buf.maxlen or 0}

    def clear(self):
        with self._lock:
            self._buf.clear()
