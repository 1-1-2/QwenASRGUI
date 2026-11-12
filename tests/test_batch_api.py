"""Wave 4 / B4 integration: real WebViewServer batch routes over HTTP.

Starts the actual ``WebViewServer`` on an ephemeral loopback port with a
lightweight stand-in backend (no OpenVINO, no GUI toolkit), then drives the
batch endpoints with real multipart uploads. This catches wiring mistakes
that unit tests on BatchQueue alone cannot see (missing route, wrong method,
bad Host guard, upload temp dir leaked).

    python tests/test_batch_api.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.request
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


class FakeBackend:
    """Minimal stand-in: WebViewServer only needs cancel/transcribe here."""

    def __init__(self, on_event=None):
        self.calls = []
        self.cancelled = False
        self.on_event = on_event

    def cancel(self):
        self.cancelled = True

    def transcribe(self, opts, progress_cb=None):
        self.calls.append(Path(opts["name"]).name)
        if progress_cb:
            progress_cb(50, "half")
            progress_cb(100, "done")
        return {"srtPath": f"/subtitles/{Path(opts['name']).stem}.srt",
                "totalSegments": 3}


def multipart(name: str, data: bytes = b"RIFF-fake"):
    boundary = "----QwenASRB4Test"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + data + tail, boundary


def request(base: str, path: str, method="GET", body=None, ctype=None):
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(base + path, data=body, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    r = Result()
    with tempfile.TemporaryDirectory() as td:
        # Redirect ALL user data into a scratch dir before importing the
        # server, so no settings.json / longform_state/ is written into the
        # source tree by this test.
        scratch = Path(td)
        import app_paths
        app_paths.user_path = lambda *p: scratch.joinpath(*p)
        app_paths.app_dir = lambda: scratch

        # webview_server imports webview_backend, which imports app.py (and
        # hence Tk) at module level. A stand-in core keeps this test free of
        # GUI dependencies; WebBackend itself is replaced below.
        import types
        fake_core = types.ModuleType("app")
        fake_core.ASREngine = object
        fake_core.BASE_DIR = scratch
        fake_core.SETTINGS_FILE = scratch / "settings.json"
        fake_core.SRT_DIR = scratch / "subtitles"
        fake_core._DEFAULT_MODEL_DIR = scratch / "ov_models"
        fake_core._CHATLLM_DIR = scratch / "chatllm"
        fake_core._BIN_PATH = scratch / "GPUModel" / "qwen3-asr-1.7b.bin"
        sys.modules.setdefault("app", fake_core)

        import webview_server as ws
        ws.WebBackend = FakeBackend

        srv = ws.WebViewServer(port=0)
        base = None
        try:
            port = srv.start()
            base = f"http://127.0.0.1:{port}"
            print("\n[B4 HTTP] server up on", port)

            code, empty = request(base, "/api/batch")
            r.check("GET /api/batch returns a real snapshot", code == 200 and
                    empty["summary"]["total"] == 0 and empty["items"] == [],
                    str(empty.get("summary")))

            # Wave 14／需求 7：上傳路由已停用（410），改由本機路徑直引加入。
            body, boundary = multipart("lecture01.wav")
            try:
                code_up, _ = request(base, "/api/batch/add", "POST", body,
                                     f"multipart/form-data; boundary={boundary}")
            except Exception as e:      # urllib 對 4xx 會拋 HTTPError
                code_up = getattr(e, "code", 0)
            r.check("POST /api/batch/add 已停用（410 Gone）", code_up == 410,
                    str(code_up))

            src1 = scratch / "lecture01.wav"
            src1.write_bytes(b"RIFF....WAVEfmt ")
            code, added = request(base, "/api/batch/add-path", "POST",
                                  json.dumps({"path": str(src1),
                                              "opts": {}}).encode(),
                                  "application/json")
            r.check("POST /api/batch/add-path 加入本機檔案（不複製）",
                    code == 200 and added.get("ok") is True, str(added)[:120])
            r.check("item carries a server-side id", bool(added["item"]["id"]))
            r.check("本機路徑 item 標記為 localPath 且不複製",
                    added["item"].get("localPath") is True and
                    added["item"].get("ownedPath") is False and
                    added["item"]["path"] == str(src1.resolve()))

            code, snap = request(base, "/api/batch")
            r.check("snapshot reflects the new item",
                    snap["summary"]["total"] == 1 and
                    snap["items"][0]["status"] == "pending", str(snap["summary"]))

            src2 = scratch / "lecture02.mp3"
            src2.write_bytes(b"ID3....")
            request(base, "/api/batch/add-path", "POST",
                    json.dumps({"path": str(src2), "opts": {}}).encode(),
                    "application/json")
            code, started = request(base, "/api/batch/start", "POST", b"{}",
                                    "application/json")
            r.check("POST /api/batch/start starts the worker",
                    code == 200 and started["summary"]["running"] is True)

            deadline = time.time() + 5
            while time.time() < deadline:
                _, snap = request(base, "/api/batch")
                if snap["summary"]["done"] == 2:
                    break
                time.sleep(0.02)
            r.check("both items processed by the server worker",
                    snap["summary"]["done"] == 2 and
                    all(i["status"] == "done" for i in snap["items"]),
                    str(snap["summary"]))
            r.check("fake backend saw both files",
                    sorted(srv.backend.calls) == ["lecture01.wav", "lecture02.mp3"],
                    str(srv.backend.calls))

            code, clr = request(base, "/api/batch/clear", "POST", b"{}",
                                "application/json")
            r.check("POST /api/batch/clear removes finished items",
                    clr["removed"] == 2)
            _, snap = request(base, "/api/batch")
            r.check("queue is empty after clear", snap["summary"]["total"] == 0)

            code, cancelled = request(base, "/api/batch/cancel", "POST", b"{}",
                                      "application/json")
            r.check("POST /api/batch/cancel is wired", code == 200 and
                    cancelled.get("ok") is True and srv.backend.cancelled)
            code, retried = request(base, "/api/batch/retry", "POST",
                                    json.dumps({"id": "nope"}).encode(),
                                    "application/json")
            r.check("POST /api/batch/retry is wired", code == 200 and
                    retried.get("ok") is False)
            code, removed = request(base, "/api/batch/remove", "POST",
                                    json.dumps({"id": "nope"}).encode(),
                                    "application/json")
            r.check("POST /api/batch/remove is wired", code == 200 and
                    removed.get("ok") is False)

            # Wave 11／問題 4 的真正根因：HTTP/1.1 keep-alive 下，/api/batch/start
            # 這類「不讀 body」的 POST 會把前端送的 `{}` 留在連線裡，
            # 殘留位元組被 BaseHTTPRequestHandler 當成下一條請求行，
            # 緊接的 GET /api/batch 因此得到 501 → 前端佇列瞬間歸零。
            # 在同一條連線上連續打 POST→GET，直接把這個情境釘死。
            import http.client
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("POST", "/api/batch/start", body=b"{}",
                             headers={"Content-Type": "application/json"})
                rp = conn.getresponse()
                rp.read()
                conn.request("GET", "/api/batch")
                rg = conn.getresponse()
                payload = rg.read()
                r.check("keep-alive POST /start then GET /batch does not 501",
                        rp.status == 200 and rg.status == 200,
                        f"POST={rp.status} GET={rg.status}")
                r.check("GET after POST still returns a full snapshot",
                        b'"summary"' in payload and b'"items"' in payload)

                conn.request("POST", "/api/batch/cancel", body=b"{}",
                             headers={"Content-Type": "application/json"})
                rp2 = conn.getresponse()
                rp2.read()
                conn.request("GET", "/api/batch")
                rg2 = conn.getresponse()
                rg2.read()
                r.check("keep-alive POST /cancel then GET /batch stays 200",
                        rp2.status == 200 and rg2.status == 200,
                        f"POST={rp2.status} GET={rg2.status}")
            finally:
                conn.close()

            # Static contract: the front end must no longer be the queue owner.
            appjs = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
            bridge = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
            # Wave 14：上傳版 batchAdd 已移除，改為本機路徑直引 batchAddPath。
            r.check("bridge exposes batchAddPath/batchStart",
                    "async batchAddPath(" in bridge
                    and "async batchStart(" in bridge
                    and "async batchAdd(" not in bridge)
            r.check("UI renders from batch SSE events", 'API.on("batch"' in appjs)
            r.check("UI refreshes from the server snapshot", "refreshBatch()" in appjs)
            r.check("web mode 不再逐檔上傳（全部走 add-path）",
                    "await API.batchAdd(" not in appjs
                    and "await API.batchAddPath(" in appjs)
        finally:
            srv.stop()

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
