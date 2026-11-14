"""切分预检（VAD preview）在「点击选档」路径下的回归。

复现的 bug：点「選擇檔案」后 picked = {path, name}（没有 File 对象），
旧代码把它直接塞进 FormData.append("file", …)，浏览器抛
"Failed to execute 'append' on 'FormData': parameter 2 is not of type 'Blob'"，
预检在请求发出之前就死了。拖放上传（真 File）反而是好的。

    python tests/test_vad_preview_path.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
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


def wait_gone(path: str, timeout: float = 2.0) -> bool:
    """等 ThreadingHTTPServer 的 finally 跑完（回應先於清理送達客戶端）。"""
    import time
    p = Path(path).parent
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not p.exists():
            return True
        time.sleep(0.05)
    return not p.exists()


def post(url: str, body: bytes, content_type: str):
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": content_type},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def main() -> int:
    r = Result()
    bridge = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
    appjs = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    srvsrc = (ROOT / "webview_server.py").read_text(encoding="utf-8")

    print("\n[1] 前端：不再把普通物件塞進 FormData")
    r.check("bridge 用 instanceof Blob 判斷",
            "opts.file instanceof Blob" in bridge)
    r.check("bridge 有 JSON 路徑分支",
            '"Content-Type": "application/json"' in bridge and
            "JSON.stringify" in bridge)
    r.check("bridge 保留 multipart 分支",
            "const fd = new FormData()" in bridge and
            'fd.append("file", opts.file' in bridge)
    r.check("app.js 預檢與轉寫對稱（傳 path）",
            "path: byPath ? picked.path : null" in appjs and
            appjs.count("byPath = !picked.file && !!picked.path") >= 2)

    print("\n[2] 後端：/api/vad-preview 接受 JSON {path}")
    r.check("_vad_preview 有 JSON 分支",
            'if "application/json" in self.headers.get("Content-Type", ""):' in srvsrc)
    r.check("JSON 分支用 _read_json_body",
            srvsrc.count("_read_json_body()") >= 2)
    r.check("multipart 分支仍保留",
            "_receive_upload()" in srvsrc and
            "self._cleanup_upload(tmp_dir)" in srvsrc)

    print("\n[3] 真實 HTTP：兩條路徑都能到達 backend")
    import webview_server as ws
    # 開機清掃會動 %TEMP%，測試裡換成空實作，保持隔離。
    ws.sweep_stale_uploads = lambda: {"removed": 0, "failed": 0, "freed": 0}
    srv = ws.WebViewServer(host="127.0.0.1", port=0)
    calls = []

    def fake_preview(opts):
        calls.append(dict(opts))
        return {"ok": True, "path": opts.get("path"),
                "name": opts.get("name"), "groupCount": 42,
                "vadThreshold": opts.get("vadThreshold")}

    srv.backend.vad_preview = fake_preview
    try:
        port = srv.start()
    except Exception as e:
        r.check("啟動本機 server", False, str(e))
        srv.stop()
        print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
        return 1
    r.check("啟動本機 server", True, f"port={port}")

    url = f"http://127.0.0.1:{port}/api/vad-preview"
    try:
        # 3a. 點選檔：JSON，零拷貝
        status, body = post(
            url,
            json.dumps({"path": "C:/audio/lecture.m4a", "name": "lecture.m4a",
                        "vadThreshold": "0.45"}).encode("utf-8"),
            "application/json")
        r.check("JSON 請求 200", status == 200, str(status))
        r.check("backend 收到原始本機路徑",
                body.get("path") == "C:/audio/lecture.m4a", str(body.get("path")))
        r.check("backend 收到閥值",
                body.get("vadThreshold") == "0.45", str(body.get("vadThreshold")))
        r.check("沒有產生上傳副本",
                calls and "asr_web_" not in (calls[-1].get("path") or ""),
                str(calls[-1].get("path") if calls else ""))

        # 3b. 拖放：multipart，向後相容
        b = "----wbtest"
        payload = (
            f"--{b}\r\n"
            'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"RIFFfake" + f"\r\n--{b}--\r\n".encode()
        status2, body2 = post(url, payload, f"multipart/form-data; boundary={b}")
        r.check("multipart 請求 200", status2 == 200, f"{status2} {body2}")
        r.check("multipart 走暫存副本",
                len(calls) == 2 and "asr_web_" in (calls[-1].get("path") or ""),
                str(calls[-1].get("path") if len(calls) > 1 else ""))
        r.check("暫存副本已被清理",
                len(calls) == 2 and wait_gone(calls[-1]["path"]),
                str(calls[-1].get("path") if len(calls) > 1 else ""))
    finally:
        srv.stop()

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
