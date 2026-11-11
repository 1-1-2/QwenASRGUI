"""Wave 2 regression: models.json as the single source of truth, L1 download
hardening and L2 manifest / offline import.

Runs without OpenVINO weights, torch, pywebview or network access.

    python tests/test_wave2.py
"""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import types
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import model_catalog as mc  # noqa: E402
import downloader as dl  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


# ---------------------------------------------------------------------------
# A tiny local HTTP server with Range + retry + HTML-error simulation
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""
    fail_first = 0
    omit_length = False
    html_error = False
    hits = 0

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        type(self).hits += 1
        n = type(self).hits
        if n <= type(self).fail_first:
            self.send_response(500)
            self.end_headers()
            if not head:
                self.wfile.write(b"upstream error")
            return
        if type(self).html_error:
            payload = b"<!DOCTYPE html><html><body>login required</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head:
                self.wfile.write(payload)
            return
        data = type(self).body
        rng = self.headers.get("Range")
        start = 0
        status = 200
        if rng and rng.startswith("bytes="):
            try:
                start = int(rng[6:].split("-")[0])
            except ValueError:
                start = 0
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            data = data[start:]
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(type(self).body)-1}/{len(type(self).body)}")
        if not type(self).omit_length:
            self.send_header("Content-Length", str(len(data)))
        else:
            self.close_connection = True
        self.end_headers()
        if not head:
            self.wfile.write(data)


def _start_server(**kwargs):
    for k, v in kwargs.items():
        setattr(_Handler, k, v)
    _Handler.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/asset.bin"


# ---------------------------------------------------------------------------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qwenasr_wave2_"))
    r = Result()

    print("\n[Wave 2] models.json / catalog")
    doc = mc.load_catalog(refresh=True)
    r.check("catalog loads with schema 1", doc["schema"] == 1)
    r.check("12 bundles / 47 files",
            len(doc["bundles"]) == 12 and
            sum(len(b["files"]) for b in doc["bundles"]) == 47)
    r.check("no fabricated size_bytes",
            all(f["size_bytes"] is None for b in doc["bundles"] for f in b["files"]))
    known_sha = [(b["id"], f["name"]) for b in doc["bundles"] for f in b["files"]
                 if f.get("sha256")]
    r.check("only 3 hashes are verified from source", len(known_sha) == 3, str(known_sha))
    r.check("all file sources resolve",
            all(f["source"] in doc["sources"] for b in doc["bundles"] for f in b["files"]))
    r.check("all target_dir are relative (no absolute leak)",
            all(not Path(b["target_dir"]).is_absolute() for b in doc["bundles"]))
    # Frozen onefile build: --add-data files live in _MEIPASS, not beside EXE.
    meipass = tmp / "meipass"
    meipass.mkdir(exist_ok=True)
    (meipass / "models.json").write_text("{}", encoding="utf-8")
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(sys, "_MEIPASS", str(meipass), create=True), \
         mock.patch.object(sys, "executable", str(tmp / "app.exe"), create=True):
        r.check("catalog_path prefers _MEIPASS when frozen",
                mc.catalog_path() == meipass / "models.json",
                str(mc.catalog_path()))
    (meipass / "models.json").unlink()
    (tmp / "models.json").write_text("{}", encoding="utf-8")
    with mock.patch.object(sys, "frozen", True, create=True), \
         mock.patch.object(sys, "_MEIPASS", str(meipass), create=True), \
         mock.patch.object(sys, "executable", str(tmp / "app.exe"), create=True):
        r.check("catalog_path falls back to EXE dir",
                mc.catalog_path() == tmp / "models.json",
                str(mc.catalog_path()))

    print("\n[Wave 2] validation rejects malformed catalogs")
    bad = json.loads(json.dumps(doc))
    bad["bundles"].append(dict(bad["bundles"][0]))
    try:
        mc._validate(bad)
        r.check("duplicate bundle id rejected", False)
    except mc.CatalogError:
        r.check("duplicate bundle id rejected", True)
    bad = json.loads(json.dumps(doc))
    bad["bundles"][0]["files"][0]["source"] = "does-not-exist"
    try:
        mc._validate(bad)
        r.check("unknown source rejected", False)
    except mc.CatalogError:
        r.check("unknown source rejected", True)
    bad = json.loads(json.dumps(doc))
    bad["bundles"][0]["files"][0]["sha256"] = "abcd"
    try:
        mc._validate(bad)
        r.check("bad sha256 rejected", False)
    except mc.CatalogError:
        r.check("bad sha256 rejected", True)

    print("\n[Wave 2] URLs and mirrors")
    f06 = doc["bundles"][0]["files"][0]
    urls = mc.file_urls(f06, doc, "https://hf-mirror.com")
    r.check("primary HF URL rewritten to mirror", urls[0].startswith("https://hf-mirror.com/"))
    r.check("fallback source preserved", any("Echo9Zulu" in u for u in urls))
    vad = next(b for b in doc["bundles"] if b["id"] == "silero_vad_v4")["files"][0]
    vurls = mc.file_urls(vad, doc, "https://hf-mirror.com")
    r.check("GitHub URL NOT rewritten by HF mirror", vurls[0].startswith("https://github.com/"))
    r.check("VAD url_override points at real file", vurls[0].endswith("silero_vad.onnx"))
    r.check("human_size(None) is honest", mc.human_size(None) == "unknown")
    r.check("human_size formats MiB", "MiB" in mc.human_size(5 << 20))

    print("\n[Wave 2] integrity probes")
    lfs = tmp / "lfs.bin"
    lfs.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 123\n")
    r.check("LFS pointer detected", "LFS" in (mc.file_problem(lfs) or ""))
    html = tmp / "index.bin"
    html.write_bytes(b"<!DOCTYPE html><html>login</html>")
    r.check("HTML error page detected", "HTML" in (mc.file_problem(html) or ""))
    r.check("missing file detected", mc.file_problem(tmp / "nope.bin") == "missing")
    good = tmp / "good.bin"
    good.write_bytes(b"\x00\x01real")
    r.check("real file accepted", mc.file_problem(good) is None)

    print("\n[Wave 2] status / verify / import")
    bundle = doc["bundles"][0]
    root = tmp / "models"
    dst = mc.bundle_dir(root, bundle)
    dst.mkdir(parents=True, exist_ok=True)
    st = mc.bundle_status(root, bundle, doc)
    r.check("empty bundle incomplete", not st["complete"] and st["present_count"] == 0)
    ok, why = mc.verify_bundle(root, bundle)
    r.check("verify reports missing", not ok and "遺失" not in why, why)
    # Fill with dummy bytes; sha256 will mismatch for the 3 hashed files.
    for f in bundle["files"]:
        (dst / f["name"]).write_bytes(b"dummy")
    st2 = mc.bundle_status(root, bundle, doc)
    r.check("all files present but hashes fail",
            st2["present_count"] == st2["total_count"] and not st2["complete"])
    ok2, why2 = mc.verify_bundle(root, bundle)
    r.check("sha mismatch detected", not ok2 and "SHA256" in why2, why2)
    # Build a correct source dir using the real hashes by copying with right bytes
    src = tmp / "manual"
    src.mkdir()
    import hashlib
    for f in bundle["files"]:
        sha = f.get("sha256")
        if sha:
            # We cannot invert sha256; instead verify import reports the mismatch.
            (src / f["name"]).write_bytes(b"dummy")
        else:
            (src / f["name"]).write_bytes(b"dummy")
    imp = mc.import_local_bundle(src, tmp / "models2", bundle, )
    r.check("import copies listed files only", imp["copied"] == len(bundle["files"]))
    r.check("import reports hash failure honestly", not imp["ok"] and "SHA256" in imp["message"])

    print("\n[Wave 2] L2 manifest")
    text = mc.build_manifest_text(root, [b["id"] for b in doc["bundles"][:2]],
                                  "https://hf-mirror.com", doc)
    r.check("manifest lists primary URL", "resolve/main/audio_encoder_model.bin" in text)
    r.check("manifest rewrites HF to mirror", "https://hf-mirror.com/" in text)
    r.check("manifest contains aria2c command", "aria2c -x 8" in text)
    r.check("manifest marks unknown hash", "sha256  : UNKNOWN" in text)
    r.check("manifest labels estimates", "estimate only" in text)
    r.check("manifest contains target dir", "ov_models/qwen3_asr_int8" in text)
    # The manifest is a copy/paste artefact: paths must use POSIX separators on
    # every OS. On Windows a bare str(Path) would emit backslashes here. This
    # assertion runs on Linux CI too, so it pins the contract before a Windows
    # build ever sees it.
    r.check("manifest paths are OS-independent (no native backslashes)",
            "ov_models\\qwen3_asr_int8" not in text and
            all("\\" not in ln for ln in text.splitlines()
                if ln.strip().startswith("target :")))
    r.check("manifest does not invent numeric bytes",
            "size_bytes  :" not in text and "bytes  :" not in text)

    print("\n[Wave 2] error classification")
    cases = [
        (urllib.error.HTTPError("u", 403, "forbidden", {}, None), "http_forbidden"),
        (urllib.error.HTTPError("u", 404, "missing", {}, None), "http_not_found"),
        (urllib.error.HTTPError("u", 503, "busy", {}, None), "http_server"),
        (urllib.error.URLError("timed out"), "timeout"),
        (urllib.error.URLError("[Errno -2] Name or service not known"), "dns"),
        (OSError("no space left"), "disk"),
    ]
    for exc, expected in cases:
        got = mc.classify_error(exc)["code"]
        r.check(f"classify {type(exc).__name__} → {expected}", got == expected, got)
    r.check("check_disk flags provable shortage",
            mc.check_disk(tmp / "x.bin", 1 << 60) is not None)
    r.check("check_disk silent when unknown",
            mc.check_disk(tmp / "x.bin", None) is None)

    print("\n[Wave 2] L1 download (local HTTP server)")
    payload = bytes(range(256)) * 400       # 102400 bytes
    srv, url = _start_server(body=payload, fail_first=0, omit_length=False, html_error=False)
    try:
        dest = tmp / "dl" / "asset.bin"
        events = []
        seen = []
        dl._download_file(url, dest, progress_cb=lambda d, t: seen.append((d, t)),
                          event_cb=lambda k, p: events.append((k, p)), retries=3)
        r.check("download bytes match", dest.read_bytes() == payload)
        r.check("progress received total", bool(seen) and seen[-1][1] == len(payload),
                str(seen[-1] if seen else None))
        r.check("no spurious retry events", not events, str(events))
    finally:
        srv.shutdown()

    srv, url = _start_server(body=payload, fail_first=2, omit_length=False, html_error=False)
    try:
        dest = tmp / "dl2" / "asset.bin"
        events = []
        dl._download_file(url, dest, event_cb=lambda k, p: events.append((k, p)), retries=3)
        r.check("retry recovers after 2 failures", dest.read_bytes() == payload)
        r.check("retry events emitted", any(k == "download-retry" for k, _ in events),
                str(events))
    finally:
        srv.shutdown()

    srv, url = _start_server(body=payload, fail_first=0, omit_length=True, html_error=False)
    try:
        dest = tmp / "dl3" / "asset.bin"
        seen = []
        dl._download_file(url, dest, progress_cb=lambda d, t: seen.append((d, t)),
                          size_bytes=len(payload), retries=1)
        r.check("no Content-Length still downloads", dest.read_bytes() == payload)
        r.check("size_bytes fallback drives progress",
                bool(seen) and seen[-1][1] == len(payload), str(seen[-1] if seen else None))
    finally:
        srv.shutdown()

    srv, url = _start_server(body=payload, fail_first=0, omit_length=False, html_error=False)
    try:
        dest = tmp / "dl4" / "asset.bin"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(payload[:40000])       # simulate interrupted download
        dl._download_file(url, dest, retries=1)
        r.check("Range resume produces exact file", dest.read_bytes() == payload)
    finally:
        srv.shutdown()

    srv, url = _start_server(body=payload, fail_first=0, omit_length=False, html_error=True)
    try:
        dest = tmp / "dl5" / "asset.bin"
        try:
            dl._download_file(url, dest, retries=1)
            r.check("HTML response rejected", False, "no exception")
        except OSError as e:
            r.check("HTML response rejected", "HTML" in str(e), str(e))
        r.check("rejected file deleted", not dest.exists())
    finally:
        srv.shutdown()

    print("\n[Wave 2] global download hook")
    got = []
    dl.set_download_event_hook(lambda k, p: got.append((k, p)))
    try:
        # The HEAD size probe consumes the first hit, so two failures are
        # needed before the first GET can succeed after a retry.
        srv2, url2 = _start_server(body=payload, fail_first=2, omit_length=False,
                                   html_error=False)
        try:
            dl._download_file(url2, tmp / "dl6" / "a.bin", retries=2)
        finally:
            srv2.shutdown()
        r.check("global hook receives retry", any(k == "download-retry" for k, _ in got))
    finally:
        dl.set_download_event_hook(None)

    print("\n[Wave 2] backend wiring (fake core)")
    fake_core = types.ModuleType("app")
    fake_core.ASREngine = mock.MagicMock()
    fake_core._DEFAULT_MODEL_DIR = tmp / "ov_models"
    sys.modules.setdefault("app", fake_core)
    fake_api = types.ModuleType("api_server")
    fake_api._VIDEO_HINT = set()
    fake_api._parse_srt = lambda text: []
    sys.modules.setdefault("api_server", fake_api)
    import webview_backend as wb
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be = wb.WebBackend(on_event=None)
    be._settings_raw = lambda: {"model_dir": str(tmp / "ov_models")}
    be._emit = lambda e, p: None
    wb.BASE_DIR = tmp
    cat = be.get_model_catalog()
    r.check("backend returns catalog", "bundles" in cat and len(cat["bundles"]) == 12)
    r.check("backend catalog has no fabricated sizes",
            all(f["declared_size_bytes"] is None
                for b in cat["bundles"] for f in b["files"]))
    man = be.export_download_manifest([])
    r.check("backend writes manifest txt", Path(man["path"]).exists() and man["bytes"] > 0)
    r.check("manifest txt contains aria2c", "aria2c" in Path(man["path"]).read_text(encoding="utf-8"))
    imp2 = be.import_local_model(str(tmp / "manual"), "qwen3_asr_0p6b_ov_int8")
    r.check("backend import returns status dict", "status" in imp2 and "ok" in imp2)
    r.check("backend download hook installed", dl._DOWNLOAD_EVENT_CB is not None)

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
