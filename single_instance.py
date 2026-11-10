"""single_instance.py - keep one GUI instance per installation (Wave 4 / H4).

Why
---
Two running instances both write to the same ``settings.json``, ``ov_models/``,
``longform_state/`` checkpoint and ``subtitles/``. Whichever writes last wins,
so a second window can silently corrupt a long transcription's resume state.
Two instances also each hold a multi-GB IR in memory.

Approach
--------
Bind a fixed loopback port *before* starting the real (random-port) web
server. The OS releases the bound socket automatically on process death -
unlike a lock file, a crashed process cannot leave a stale lock behind. A
small JSON file next to the launcher only carries the running instance's URL
so a second launch can hand the user to the existing window instead of just
exiting with an error.

Set ``QWENASR_ALLOW_MULTI=1`` to bypass (development / automated tests).
Headless CLI mode never goes through this module.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import app_paths

DEFAULT_LOCK_PORT = 47831
LOCK_FILENAME = "app.lock"


class SingleInstance:
    """Context manager holding the instance lock for the process lifetime."""

    def __init__(self, port: int = DEFAULT_LOCK_PORT):
        self.port = int(os.environ.get("QWENASR_LOCK_PORT", port))
        self._sock: socket.socket | None = None
        self.lock_file: Path = app_paths.user_path(LOCK_FILENAME)
        self.url = ""

    def _read_lock(self) -> str:
        try:
            data = json.loads(self.lock_file.read_text(encoding="utf-8"))
            url = str(data.get("url", ""))
            return url if url.startswith("http://127.0.0.1:") else ""
        except (OSError, ValueError, TypeError):
            return ""

    def acquire(self) -> tuple[bool, str]:
        """Return ``(True, "")`` when this process owns the lock, else
        ``(False, existing_url)`` so the caller can focus that window."""
        if os.environ.get("QWENASR_ALLOW_MULTI") == "1":
            return True, ""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
            self._sock = s
            return True, ""
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            return False, self._read_lock()

    def publish(self, url: str):
        """Record the real server URL for a later duplicate launch."""
        self.url = url
        if self._sock is None:
            return
        try:
            tmp = self.lock_file.with_suffix(".lock.tmp")
            tmp.write_text(json.dumps({"pid": os.getpid(), "url": url},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.lock_file)
        except OSError:
            pass

    def release(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            self.lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False
