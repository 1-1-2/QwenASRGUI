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

    # 3 小时任务 + 门控日志约 300~400 条，再加任务边界/异常，300 会挤掉
    # 「任务开始」那条 —— 事后回溯时最需要的恰恰是它。
    DEFAULT_MAXLEN = 2000

    def __init__(self, maxlen: int = DEFAULT_MAXLEN, on_event=None):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._on_event = on_event
        self._seq = 0
        # 门控去重：只在状态翻转时记录，同状态最多每 60 秒一条心跳。
        self._gate_state: str | None = None
        self._gate_ts = 0.0
        self._gate_reason: str | None = None

    def add(self, message, level: str = "info", source: str = "backend",
            event: str | None = None, **fields) -> dict:
        """Append one entry.

        ``event`` / ``fields`` 是新增的结构化字段；不传时行为与旧的自由
        文本完全一致（前向兼容），旧调用方一行都不用改。
        """
        message = str(message).strip()
        if not message:
            return {}
        if level not in self.LEVELS:
            level = "info"
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": _now(), "level": level,
                     "source": source, "message": message[:1000]}
            if event:
                entry["event"] = event
                if fields:
                    entry["fields"] = {k: v for k, v in fields.items()
                                       if v is not None}
            self._buf.append(entry)
        if self._on_event:
            try:
                self._on_event("log", entry)
            except Exception:
                pass
        return entry

    def gate(self, action: str, avail_gb: float, threshold_gb: float,
             reason: str = "", heartbeat_sec: float = 60.0) -> dict | None:
        """记录一次内存门控判定，**只在状态翻转时真正落一条**。

        500ms 检查一次、每次都打会把缓冲刷满，也会把真正有用的事件挤掉。
        返回写出的 entry；被去重时返回 None。
        """
        import time as _time
        now = _time.monotonic()
        with self._lock:
            flipped = action != self._gate_state
            stale = (now - self._gate_ts) >= heartbeat_sec
            if not flipped and not stale:
                self._gate_reason = reason or self._gate_reason
                return None
            self._gate_state = action
            self._gate_ts = now
        level = "info" if flipped else "debug"
        return self.add(
            f"内存门控 {action}（可用 {avail_gb:.1f} GB / 阈值 "
            f"{threshold_gb:.1f} GB{('，原因 ' + reason) if reason else ''}）",
            level=level, source="fa", event="gate.change",
            action=action, availGb=round(float(avail_gb), 2),
            thresholdGb=round(float(threshold_gb), 2),
            reason=reason or None)

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
