"""Wave 12 regression: the four issues reported after Wave 11.

1. Model memory is released after a run (and lazily reloaded on the next one).
2. Download-centre paths no longer gain a duplicate ``ov_models`` layer.
3. Batch "Add files" uses native local paths; the batch page has drag & drop.
4. Edge startup success is judged by the page connecting, not by exit code.

Headless: no model weights, no GUI, no Windows desktop required.

    python tests/test_wave12.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as core                        # noqa: E402
import model_catalog as mc                # noqa: E402
import webview_backend as wb              # noqa: E402
from batch_queue import BatchQueue        # noqa: E402
from webview_server import WebViewServer  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _make_backend():
    """Bare WebBackend without __init__ side effects (downloads / settings)."""
    b = object.__new__(wb.WebBackend)
    eng = core.ASREngine()
    eng.ready = True
    eng.audio_enc = object()
    eng.vad_sess = object()
    eng._last_segments_rich = [{"text": "x"} for _ in range(50)]
    eng._last_vad_diag = "diag"
    b.engine = eng
    b._loaded = True
    b._load_err = None
    b._loading = False
    b._running = 0
    b._batch_running = False
    b._progress_scope = None
    b._active_backend = "openvino"
    b._lock = threading.Lock()
    b._settings_raw = lambda: {}
    events, loads = [], []

    def _emit(ev, pl):
        events.append((ev, pl))

    def _load():
        loads.append(1)
        fresh = core.ASREngine()
        fresh.ready = True
        b.engine = fresh
        b._loaded = True

    b._emit = _emit
    b._load_worker = _load
    b._transcribe_impl = lambda opts, cb=None: {"segments": [], "srtPath": ""}
    return b, events, loads


# ──────────────────────────────────────────────────────────────────────
# Issue 1: model memory lifecycle
# ──────────────────────────────────────────────────────────────────────
def test_issue1(r: Result):
    print("\n[1] 識別完成後釋放模型記憶體")

    b, events, loads = _make_backend()
    old = b.engine
    res = b.release_model()
    r.check("release_model 回傳 ok / released", bool(res.get("ok") and res.get("released")), str(res))
    r.check("舊引擎的重量級引用被切斷",
            old.audio_enc is None and old.vad_sess is None and
            old._last_segments_rich is None and old.ready is False)
    r.check("換成同介面空殼且標記未載入",
            b.engine is not old and not b.engine.ready and not b._loaded)
    r.check("釋放會廣播 status(modelReady=false, released)",
            any(ev == "status" and pl.get("modelReady") is False and pl.get("released")
                for ev, pl in events))
    r.check("釋放後 get_languages 仍可用",
            isinstance(b.get_languages().get("languages"), list))

    b2, ev2, loads2 = _make_backend()
    b2.transcribe({})
    r.check("單檔轉寫結束後自動釋放", not b2.engine.ready,
            f"ready={b2.engine.ready} loads={loads2}")
    r.check("自動釋放不需要重新載入", loads2 == [], str(loads2))

    b2.transcribe({})
    r.check("釋放後下一次轉寫會懶載入", loads2 == [1] and not b2.engine.ready,
            f"loads={loads2}")

    b3, ev3, loads3 = _make_backend()
    b3.set_progress_scope("batch")
    b3.transcribe({})
    r.check("批次進行中不逐檔釋放（避免每個檔重編譯）", b3.engine.ready)
    b3.release_model_if_enabled("batch-finished")
    r.check("整隊結束才釋放一次", not b3.engine.ready and b3._batch_running is False)

    b4, _, _ = _make_backend()
    b4._running = 1
    r.check("轉寫中拒絕釋放", b4.release_model().get("reason") == "busy")

    b5, _, _ = _make_backend()
    b5._settings_raw = lambda: {"auto_release_model": False}
    b5.transcribe({})
    r.check("設定關掉自動釋放後模型保留", b5.engine.ready)
    r.check("即使不卸載模型也清掉本次字級快取",
            b5.engine._last_segments_rich is None)

    b6, _, _ = _make_backend()
    b6._active_backend = "crispasr"
    r.check("GPU 後端不自動釋放（Vulkan context 常駐）",
            b6._auto_release_enabled() is False)
    b6.transcribe({})
    r.check("GPU 後端轉寫後模型仍在", b6.engine.ready)

    # 首次啟動（模型從未載入）時，vad_preview 的 _ensure_engine 絕不能
    # 拉起 _load_worker() → 否則點一下預檢就開始下載數百 MB。
    b7, _, loads7 = _make_backend()
    b7.engine.ready = False
    b7._released_once = False
    b7._ensure_engine()
    r.check("從未載入過 → 不觸發模型載入／下載", loads7 == [], str(loads7))
    b7._released_once = True
    b7._ensure_engine()
    r.check("只有曾經釋放過才會懶載入", loads7 == [1] and b7.engine.ready,
            str(loads7))

    # Real BatchQueue: release hook fires once for the whole queue, not per item.
    class CountingBackend:
        def __init__(self):
            self.calls = 0
            self.releases = 0

        def transcribe(self, opts, progress_cb=None):
            self.calls += 1
            return {"srtPath": ""}

        def set_progress_scope(self, scope):
            pass

        def release_model_if_enabled(self, reason):
            self.releases += 1

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        be = CountingBackend()
        q = BatchQueue(be, state_dir=tmp / "state")
        for i in range(3):
            f = tmp / f"a{i}.wav"
            f.write_bytes(b"RIFF-fake-wav")
            q.add_path(f)
        q.start()
        for _ in range(200):
            if not q.running and all(x["status"] == "done" for x in q.items):
                break
            import time
            time.sleep(0.02)
        r.check("真 BatchQueue 3 個檔只觸發 1 次整隊釋放",
                be.calls == 3 and be.releases == 1,
                f"calls={be.calls} releases={be.releases}")


# ──────────────────────────────────────────────────────────────────────
# Issue 2: download centre path
# ──────────────────────────────────────────────────────────────────────
def test_issue2(r: Result):
    print("\n[2] 下載中心檢測路徑不多一層 ov_models")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        md = root / "ov_models"
        doc = mc.load_catalog()
        st = mc.catalog_status(root, doc, md)
        bad = [b["target_dir"] for b in st["bundles"]
               if "ov_models/ov_models" in b["target_dir"].replace("\\", "/")]
        r.check("catalog_status 不再產生 ov_models/ov_models",
                not bad and st["model_dir"] == str(md), str(bad[:2]))

        by_id = {b["id"]: b for b in st["bundles"]}
        r.check("0.6B 模型落在 <app>/ov_models/qwen3_asr_int8",
                by_id["qwen3_asr_0p6b_ov_int8"]["target_dir"] == str(md / "qwen3_asr_int8"))
        r.check("VAD（target_dir=ov_models）落在 <app>/ov_models",
                by_id["silero_vad_v4"]["target_dir"] == str(md))
        r.check("ffmpeg 落在應用根（不是 ov_models 底下）",
                by_id["ffmpeg_windows"]["target_dir"] == str(root / "ffmpeg"))

        ids = [b["id"] for b in doc["bundles"][:3]]
        text = mc.build_manifest_text(root, ids, "", doc, md)
        r.check("下載清單與檢測用同一組路徑",
                "ov_models/ov_models" not in text and
                "ov_models/qwen3_asr_int8" in text)

        bundle = doc["bundles"][0]
        src = root / "src"
        src.mkdir()
        for f in bundle["files"]:
            (src / f["name"]).write_bytes(b"x" * 8)
        imp = mc.import_local_bundle(src, root, bundle, md)
        r.check("匯入落點與檢測落點一致",
                imp["status"]["target_dir"] == by_id[bundle["id"]]["target_dir"],
                imp["status"]["target_dir"])

        # Backend wiring: app_root and model_dir are passed separately.
        b = object.__new__(wb.WebBackend)
        b._settings_raw = lambda: {"model_dir": str(md)}
        b.get_settings = lambda: {"mirror": ""}
        with mock.patch.object(wb, "BASE_DIR", root):
            cat = b.get_model_catalog()
        r.check("WebBackend.get_model_catalog 走 (app_root, model_dir)",
                cat["model_root"] == str(root) and cat["model_dir"] == str(md) and
                not any("ov_models/ov_models" in x["target_dir"].replace("\\", "/")
                        for x in cat["bundles"]))


# ──────────────────────────────────────────────────────────────────────
# Issue 3: native picker + batch drag & drop
# ──────────────────────────────────────────────────────────────────────
def test_issue3(r: Result):
    print("\n[3] 批量頁加入檔案走本機路徑 + 拖放")
    html = (ROOT / "webview/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    bridge = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
    i18n = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
    server_src = (ROOT / "webview_server.py").read_text(encoding="utf-8")

    r.check("批量頁有拖放區 #batch-drop", 'id="batch-drop"' in html and
            'class="drop batch-drop"' in html)
    r.check("加入檔案改走 addBatchFilesViaPicker",
            "addBatchFilesViaPicker" in app_js and
            "#btn-batch-add\").addEventListener(\"click\", addBatchFilesViaPicker)" in app_js and
            "batchInput.click(); return;" in app_js)
    r.check("選到路徑後走 batchAddPath（零複製）",
            "await API.batchAddPath(p, opts)" in app_js)
    r.check("拖放會嘗試取出本機絕對路徑",
            "function extractLocalPaths(" in app_js and
            "text/uri-list" in app_js and "file://" in app_js)
    r.check("拖放拿不到路徑時仍可上傳（Wave 11 ownedPath 清理）",
            "await API.batchAdd(f, opts)" in app_js)
    r.check("bridge 有 pickFiles() 打 /api/pick-files",
            "async pickFiles()" in bridge and '"/api/pick-files"' in bridge)
    r.check("後端有原生選檔與 /api/pick-files 路由",
            "def pick_local_files(" in server_src and
            'path == "/api/pick-files"' in server_src and
            "GetOpenFileNameW" in server_src)
    r.check("i18n 三語補齊拖放文案",
            all(k in i18n for k in ("batch.dropBig", "batch.dropSub", "batch.dropAdded")))

    # Real HTTP round-trip with an injected picker (no Windows dialog in CI).
    srv = WebViewServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        srv.pick_files = lambda: [r"C:\audio\a.wav", r"C:\audio\b.mp3"]
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        conn.request("POST", "/api/pick-files", body=b"{}",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        r.check("/api/pick-files 回傳注入的本機路徑",
                resp.status == 200 and payload.get("count") == 2 and
                payload["paths"][0].endswith("a.wav"), str(payload)[:160])
        # keep-alive body was consumed -> next request must not become 501
        conn2 = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        conn2.request("POST", "/api/pick-files", body=b"{}",
                      headers={"Content-Type": "application/json"})
        p2 = conn2.getresponse(); p2.read()
        conn2.request("GET", "/api/batch")
        g = conn2.getresponse(); g.read()
        r.check("pick-files 後同連線 GET /api/batch 不會 501",
                p2.status == 200 and g.status == 200,
                f"POST={p2.status} GET={g.status}")
        conn2.close()
    finally:
        srv.stop()


# ──────────────────────────────────────────────────────────────────────
# Issue 4: Edge startup judged by frontend connection
# ──────────────────────────────────────────────────────────────────────
def _load_launcher():
    stubs = {
        "proc_guard": types.SimpleNamespace(setup_kill_on_close_job=lambda: None),
        "single_instance": types.SimpleNamespace(SingleInstance=lambda: None),
    }
    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)
    if "app_paths" not in sys.modules:
        sys.modules["app_paths"] = types.SimpleNamespace(
            app_dir=lambda: ROOT, resource_path=lambda *a: ROOT.joinpath(*a),
            resource_root=lambda: ROOT, user_path=lambda *a: ROOT.joinpath(*a),
            is_frozen=lambda: False)
    sys.modules.pop("app_webview", None)
    import app_webview
    return app_webview


def test_issue4(r: Result):
    print("\n[4] 開窗成功以「頁面連上本機服務」判定")
    launcher_src = (ROOT / "app_webview.py").read_text(encoding="utf-8")
    server_src = (ROOT / "webview_server.py").read_text(encoding="utf-8")

    srv = object.__new__(WebViewServer)
    srv.frontend_seen = threading.Event()
    srv.sse_clients = 0
    srv._client_lock = threading.Lock()
    r.check("初始沒有任何前端連線訊號", not srv.window_signal() and not srv.window_alive())
    srv.mark_frontend()
    r.check("靜態頁請求會點亮 frontend_seen", srv.window_signal())
    srv.sse_enter(); srv.sse_enter()
    r.check("SSE 連線計數累加", srv.sse_clients == 2 and srv.window_alive())
    srv.sse_leave()
    r.check("一條 SSE 斷線後仍視為視窗開著", srv.sse_clients == 1 and srv.window_alive())
    srv.sse_leave()
    r.check("全部斷線後 window_alive=false（frontend_seen 仍為粘滯證據）",
            not srv.window_alive() and srv.window_signal())

    r.check("do_GET 排除 /health 後標記前端",
            'if path != "/health":' in server_src and
            "server.mark_frontend()" in server_src)
    r.check("_sse 進出會維護計數",
            "server.sse_enter()" in server_src and "server.sse_leave()" in server_src)
    r.check("不再用 exit 0 + 存活秒數判定秒退",
            "_EDGE_EARLY_EXIT_SEC" not in launcher_src and
            "def _wait_for_window_close(" in launcher_src and
            "seen=lambda: srv.window_signal()" in launcher_src)
    r.check("清鎖前檢查擁有者 PID",
            "def _lock_owner_alive(" in launcher_src)

    aw = _load_launcher()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "SingletonLock"
        p.write_text(f"myhost-{os.getpid()}", encoding="utf-8")
        live = aw._lock_owner_alive(p)
        p.write_text("myhost-99999999", encoding="utf-8")
        dead = aw._lock_owner_alive(p)
        r.check("活執行個體的鎖不刪 / 陳舊鎖可刪", live and not dead,
                f"live={live} dead={dead}")

        # Stale locks are removed; a live owner is preserved.
        profile = Path(d) / "profile"
        profile.mkdir()
        (profile / "SingletonLock").write_text("myhost-99999999", encoding="utf-8")
        (profile / "SingletonCookie").write_text("x", encoding="utf-8")
        aw._clear_stale_singleton(str(profile))
        r.check("陳舊 SingletonLock / Cookie 被清掉",
                not (profile / "SingletonLock").exists() and
                not (profile / "SingletonCookie").exists())
        (profile / "SingletonLock").write_text(f"myhost-{os.getpid()}", encoding="utf-8")
        aw._clear_stale_singleton(str(profile))
        r.check("活執行個體的 SingletonLock 保留",
                (profile / "SingletonLock").exists())

    class FakeProc:
        def poll(self):
            return 0

        def wait(self):
            return 0

    closed = []
    orig_open, orig_sleep = aw.open_edge_app, aw.time.sleep
    aw.time.sleep = lambda _s: None
    aw.open_edge_app = lambda url, variant=0: FakeProc()
    try:
        rc, alive, attempts = aw._run_edge_session(
            "http://127.0.0.1:1/", seen=lambda: True,
            wait_close=lambda: closed.append(1), grace=0.0)
    finally:
        aw.open_edge_app, aw.time.sleep = orig_open, orig_sleep
    r.check("頁面連上 → 只啟動一次並等到視窗關閉",
            attempts == 1 and closed == [1], f"attempts={attempts} closed={closed}")


def main() -> int:
    r = Result()
    test_issue1(r)
    test_issue2(r)
    test_issue3(r)
    test_issue4(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
