"""Wave 4 regression: H4 single instance, H1 run log, H2 button state
machine, B5.2 stable locale enums, H6/H7 backend text localisation.

No GUI, no network, no model weights.

    python tests/test_ui_ops.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def main() -> int:
    r = Result()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── H1: bounded server-side run log ─────────────────────────────
        print("\n[H1] run log ring buffer")
        import app_paths
        app_paths.user_path = lambda *p: tmp.joinpath(*p)
        from run_log import RunLog
        events = []
        log = RunLog(maxlen=5, on_event=lambda ev, payload: events.append((ev, payload)))
        for i in range(9):
            log.add(f"line {i}", level="warn" if i % 2 else "info")
        snap = log.snapshot()
        r.check("buffer is bounded", len(snap["items"]) == 5 and snap["capacity"] == 5)
        r.check("oldest entries evicted", snap["items"][0]["message"] == "line 4")
        r.check("sequence numbers are monotonic",
                [e["seq"] for e in snap["items"]] == [5, 6, 7, 8, 9])
        r.check("SSE log events emitted", len(events) == 9 and
                all(e == "log" for e, _ in events))
        r.check("levels preserved", snap["items"][0]["level"] == "info")
        r.check("empty messages are ignored", log.add("   ") == {} and
                len(log.snapshot()["items"]) == 5)
        r.check("snapshot limit works", len(log.snapshot(2)["items"]) == 2)
        log.clear()
        r.check("clear empties the buffer", log.snapshot()["total"] == 0)

        # ── H4: single instance guard ───────────────────────────────────
        print("\n[H4] single instance")
        from single_instance import SingleInstance
        os.environ.pop("QWENASR_ALLOW_MULTI", None)
        port = 47900 + (os.getpid() % 500)
        os.environ["QWENASR_LOCK_PORT"] = str(port)
        first = SingleInstance()
        ok1, _ = first.acquire()
        r.check("first instance acquires the lock", ok1)
        first.publish("http://127.0.0.1:12345/")
        r.check("lock file records the live URL",
                json.loads(first.lock_file.read_text(encoding="utf-8"))["url"]
                == "http://127.0.0.1:12345/")
        second = SingleInstance()
        ok2, existing = second.acquire()
        r.check("second instance is refused", ok2 is False)
        r.check("second instance receives the existing URL",
                existing == "http://127.0.0.1:12345/", existing)
        first.release()
        third = SingleInstance()
        ok3, _ = third.acquire()
        r.check("lock is re-acquirable after release", ok3)
        third.release()

        # A crashed process closes the socket but leaves the lock file: the
        # lock must still be obtainable (the port, not the file, owns it).
        crashed = SingleInstance()
        crashed.acquire()
        crashed.publish("http://127.0.0.1:9999/")
        crashed._sock.close()          # simulate OS socket release on death
        crashed._sock = None           # deliberately do NOT call release()
        after_crash = SingleInstance()
        ok4, _ = after_crash.acquire()
        r.check("crashed process leaves no stale lock", ok4)
        after_crash.release()

        os.environ["QWENASR_ALLOW_MULTI"] = "1"
        bypass1, bypass2 = SingleInstance(), SingleInstance()
        r.check("QWENASR_ALLOW_MULTI bypasses the guard",
                bypass1.acquire()[0] and bypass2.acquire()[0])
        bypass1.release(); bypass2.release()
        os.environ.pop("QWENASR_ALLOW_MULTI", None)
        os.environ.pop("QWENASR_LOCK_PORT", None)

        # ── H2 / B5.2 / H6/H7: static front-end contracts ───────────────
        print("\n[H2/B5.2/H6/H7] front-end contracts")
        appjs = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
        i18n = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
        bridge = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
        html = (ROOT / "webview/index.html").read_text(encoding="utf-8")
        server = (ROOT / "webview_server.py").read_text(encoding="utf-8")
        backend = (ROOT / "webview_backend.py").read_text(encoding="utf-8")

        r.check("one state machine owns button states",
                "function syncActionButtons()" in appjs and
                "function markBusy(" in appjs)
        r.check("no scattered manual run-button re-enabling",
                'btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>開始轉換`' not in appjs)
        r.check("run button label follows i18n (run / cancel)",
                '"file.run"' in i18n and '"file.cancel"' in i18n
                and '"file.run"' in appjs and '"file.cancel"' in appjs)
        r.check("log toggle / clear are wired",
                '"#btn-log-toggle"' in appjs and '"#btn-log-clear"' in appjs and
                'id="btn-log-toggle"' in html)
        r.check("SSE log event renders rows", 'API.on("log"' in appjs)
        r.check("server exposes GET /api/logs and clear route",
                '"/api/logs"' in server and '"/api/logs/clear"' in server)
        r.check("backend owns a RunLog", "self.run_log = RunLog" in backend
                and "def get_logs(" in backend)
        r.check("status/progress messages feed the log (structured values flattened)",
                'self.run_log.add(_t.plain(msg)' in backend
                and 'import ui_text as _t' in backend)

        # B5.2 stable enum
        r.check("language select uses stable enum values",
                'id="set-lang"><option value="hant"' in html and
                '<option value="hans"' in html and '<option value="en"' in html)
        r.check("i18n exposes normalize()", "function normalize(uiLang)" in i18n
                and "normalize," in i18n)
        r.check("legacy Chinese labels still map",
                '"繁體中文": "hant"' in i18n and '"简体中文": "hans"' in i18n)
        r.check("settings restore normalises legacy values",
                "I18N.normalize(s.uiLang" in appjs)
        r.check("settings save persists the enum",
                "API.setSettings({ uiLang: v })" in appjs)

        # H6/H7 backend text localisation
        r.check("i18n exposes L() for backend strings",
                "function L(value, keyHint)" in i18n and " L," in i18n)
        r.check("H6/H7 structured key+vars dictionary exists",
                "const M = {" in i18n and "function renderMsg(" in i18n
                and "msgKeys" in i18n and "ui_text.py" in i18n)
        r.check("Simplified conversion is word-level first (no misaligned char pairs)",
                "function toSimplified(" in i18n and "W2S_WORDS" in i18n
                and "const S2S = {" in i18n and "S2S_PAIRS" not in i18n)
        r.check("English phrase dictionary exists", "EN_MAP" in i18n)
        r.check("model card labels are localised at display time",
                "I18N.L(m.label)" in appjs and "o.value = m.label;" in appjs)
        r.check("canonical ids are NOT translated",
                "card.dataset.core = core.label" in appjs)
        r.check("health / profile / progress text localised",
                "I18N.L(g.label)" in appjs and "I18N.L(p.title)" in appjs and
                "I18N.L(status)" in appjs)
        r.check("language change re-renders dynamic views",
                "renderHealth();" in appjs and "loadLanguages();" in appjs)
        r.check("bridge exposes log API", "async getLogs(" in bridge
                and "async clearLogs(" in bridge)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
