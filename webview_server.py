"""webview_server.py — 桌面 WebView 的本機 HTTP 伺服器（純標準庫）

把 webview/ 靜態前端與 /api/* 業務端點以同一個 stdlib HTTP 伺服器提供，
並以 SSE（/api/events）把進度/狀態主動推給前端。設計取向與 Eel /
flaskwebgui 相同（本機 server + 瀏覽器渲染），但零第三方相依，方便
PyInstaller 打包（與既有 api_server.py 一致的做法）。

安全：只綁 127.0.0.1（loopback），外部連不到、且不觸發防火牆提示，
故桌面用途免金鑰。LAN 對外的 OpenAI 相容端點仍由 api_server.py 負責。

路由：
  GET  /                      → webview/index.html
  GET  /css/* /js/* …         → webview/ 靜態檔
  GET  /health                → {"status","model_ready"}
  GET  /api/status            → 後端狀態
  GET  /api/settings          → 設定；POST 改設定（json patch）
  GET  /api/devices           → 裝置 + 診斷
  GET  /api/endpoint          → LAN 端點狀態；POST {action:start|stop|regen,port?}
  POST /api/backend           → {index} 切換推理核心
  POST /api/transcribe        → multipart 上傳轉錄，回 {segments,srtPath}
  GET  /api/events            → SSE：data: {"event","payload"} 進度/狀態推送
"""
from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import app_paths
from webview_backend import WebBackend


def resolve_web_dir() -> Path:
    """Directory holding the front-end assets (index.html and friends).

    Source tree: the repository's ``webview/``. Frozen: the assets are shipped
    as ``webview_assets/`` inside the resource root (``_internal/`` in a onedir
    build, the _MEIPASS temp dir in onefile). The rename is deliberate - the
    pywebview package also owns a top-level ``webview/`` directory.
    """
    for name in ("webview_assets", "webview"):
        d = app_paths.resource_path(name)
        if (d / "index.html").is_file():
            return d
    if app_paths.is_frozen():
        return app_paths.resource_root() / "webview_assets"
    return Path(__file__).resolve().parent / "webview"


WEB_DIR = resolve_web_dir()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class _EventHub:
    """SSE 廣播：每個連線一個 Queue，publish() 推給全部訂閱者。

    为什么要保留「最近状态」并在新订阅者接入时重放：
    模型载入在线程里从 ``srv.start()`` 之后就开始跑，而 Edge 窗口要一会儿
    才连上 ``/api/events``。加载期间推的「載入 VAD 模型…」「編譯 ASR 模型…」
    在没有任何订阅者时**直接被丢掉**，用户看到的就是一个白屏卡十几秒，
    然后突然变「已就绪」——中间发生过什么完全不可见，只能怀疑是不是卡死。
    这里把最近一条 ``status`` / ``progress`` / ``warning`` 留下来，新连接
    先补发再走实时流，冷启动的那段就看得见了。
    """

    # 这些事件是「当前状态」而非「一次性动作」，重放有意义。
    # progress / download 之类的瞬时进度不重放（重放一条过期的 47% 只会误导）。
    _STICKY = ("status", "warning")

    def __init__(self):
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._sticky: dict[str, dict] = {}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.add(q)
            # 先补发最近状态（如果有），让晚连上的页面也知道现在处于什么状态
            for ev, payload in self._sticky.items():
                q.put({"event": ev, "payload": payload})
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, payload: dict):
        msg = {"event": event, "payload": payload}
        with self._lock:
            if event in self._STICKY:
                self._sticky[event] = payload      # 记住，供晚连上的页面补发
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass


class WebViewServer:
    """本機前端伺服器。持有 WebBackend + EventHub。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.hub = _EventHub()
        self.backend = WebBackend(on_event=self.hub.publish)
        # B4: the batch queue lives on the server, not in the browser. It
        # publishes through the same hub so the UI just renders SSE state.
        from batch_queue import BatchQueue
        self.batch_queue = BatchQueue(self.backend, on_event=self.hub.publish)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._want_port = port
        self.port = port

    def start(self):
        if self._httpd:
            return
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):    # 靜音預設存取日誌
                pass

            # ── 回應輔助 ──────────────────────────────────────
            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code, msg):
                self._json({"error": {"message": msg}}, code)

            def _read_json_body(self) -> dict:
                n = int(self.headers.get("Content-Length", 0))
                if not n:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            # ── DNS rebinding / CSRF 防護 ─────────────────────
            #   惡意網站可能用 DNS rebinding 把網域指向 127.0.0.1，或跨站
            #   POST 到本機 /api（端點有副作用）。要求 Host/Origin 必須是
            #   本機回環位址 —— Edge --app 載入的就是 127.0.0.1:port，會通過。
            def _check_host(self) -> bool:
                allowed = {f"127.0.0.1:{server.port}", f"localhost:{server.port}"}
                if self.headers.get("Host", "") not in allowed:
                    self._err(403, "bad host")
                    return False
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{a}" for a in allowed}:
                    self._err(403, "bad origin")
                    return False
                return True

            # ── GET ───────────────────────────────────────────
            def do_GET(self):
                path = urlparse(self.path).path
                if (path.startswith("/api/") or path == "/health") and not self._check_host():
                    return
                if path == "/health":
                    b = server.backend
                    return self._json({"status": "ok",
                                       "model_ready": bool(getattr(b.engine, "ready", False))})
                if path == "/api/status":
                    return self._json(server.backend.get_status())
                if path == "/api/settings":
                    return self._json(server.backend.get_settings())
                if path == "/api/devices":
                    return self._json(server.backend.list_devices())
                if path == "/api/accel":          # 硬體偵測 + CrispASR 加速版本清單
                    return self._json(server.backend.get_accel())
                if path == "/api/model-options":
                    return self._json(server.backend.get_model_options())
                if path == "/api/basic-profiles":   # 基礎模式：用途 → 建議模型
                    return self._json(server.backend.get_basic_profiles())
                if path == "/api/languages":
                    return self._json(server.backend.get_languages())
                if path == "/api/endpoint":
                    return self._json(server.backend.get_endpoint())
                if path == "/api/health-check":
                    return self._json(server.backend.health_check())
                if path == "/api/tunnel":
                    return self._json(server.backend.get_tunnel())
                if path == "/api/qr":
                    return self._qr(urlparse(self.path).query)
                if path == "/api/batch":
                    return self._json(server.batch_queue.snapshot())
                if path == "/api/logs":
                    limit = None
                    try:
                        from urllib.parse import parse_qs
                        raw = parse_qs(urlparse(self.path).query).get("limit", [None])[0]
                        limit = int(raw) if raw else None
                    except (ValueError, TypeError):
                        pass
                    return self._json(server.backend.get_logs(limit))
                if path == "/api/model-catalog":
                    return self._json(server.backend.get_model_catalog())
                if path == "/api/events":
                    return self._sse()
                if path.startswith("/api/"):
                    return self._err(404, "unknown api")
                return self._static(path)

            # ── POST ──────────────────────────────────────────
            def do_POST(self):
                path = urlparse(self.path).path
                if not self._check_host():        # 所有 POST 皆為 /api，一律驗證
                    return
                try:
                    if path == "/api/settings":
                        return self._json(server.backend.set_settings(self._read_json_body()))
                    if path == "/api/backend":
                        return self._json(server.backend.set_backend(
                            self._read_json_body().get("index")))
                    if path == "/api/model":
                        body = self._read_json_body()
                        return self._json(server.backend.set_model(
                            body.get("core"), body.get("model")))
                    if path == "/api/accel":         # 選定加速版本（下次載入時下載）
                        return self._json(server.backend.set_accel(
                            self._read_json_body().get("variant")))
                    if path == "/api/load":           # 首次選定模型 → 就地下載並載入
                        return self._json(server.backend.request_load())
                    if path == "/api/endpoint":
                        body = self._read_json_body()
                        act = body.get("action")
                        if act == "start":
                            return self._json(server.backend.toggle_endpoint(True, body.get("port")))
                        if act == "stop":
                            return self._json(server.backend.toggle_endpoint(False))
                        if act == "regen":
                            return self._json(server.backend.regen_key())
                        return self._err(400, "unknown action")
                    if path == "/api/tunnel":
                        act = self._read_json_body().get("action")
                        return self._json(server.backend.toggle_tunnel(act == "start"))
                    if path == "/api/transcribe":
                        return self._transcribe()
                    if path == "/api/vad-preview":
                        return self._vad_preview()
                    if path == "/api/cancel":
                        return self._json({"ok": server.backend.cancel()})
                    if path == "/api/open-output":
                        return self._json({"ok": server.backend.open_output_dir()})
                    if path == "/api/check-update":
                        return self._json(server.backend.open_releases())
                    if path == "/api/download-manifest":
                        return self._json(server.backend.export_download_manifest(
                            self._read_json_body().get("bundles")))
                    if path == "/api/import-model":
                        body = self._read_json_body()
                        return self._json(server.backend.import_local_model(
                            body.get("dir", ""), body.get("bundle", "")))
                    if path == "/api/batch/add":
                        return self._batch_add()
                    if path == "/api/batch/start":
                        return self._json({"ok": True,
                                           "summary": server.batch_queue.start()})
                    if path == "/api/batch/cancel":
                        return self._json({"ok": True,
                                           "summary": server.batch_queue.cancel()})
                    if path == "/api/batch/remove":
                        body = self._read_json_body()
                        return self._json({"ok": server.batch_queue.remove(
                            str(body.get("id", "")))})
                    if path == "/api/batch/retry":
                        body = self._read_json_body()
                        return self._json({"ok": server.batch_queue.retry(
                            str(body.get("id", "")))})
                    if path == "/api/logs/clear":
                        return self._json(server.backend.clear_logs())
                    if path == "/api/refine-punct":
                        return self._json(server.backend.refine_punctuation(
                            self._read_json_body().get("path", "")))
                    if path == "/api/export-handout":
                        body = self._read_json_body()
                        return self._json(server.backend.export_handout(
                            body.get("path", ""), body.get("format", "md")))
                    if path == "/api/batch/clear":
                        return self._json({"ok": True, "removed":
                                           server.batch_queue.clear_finished()})
                    return self._err(404, "unknown api")
                except PermissionError as e:
                    # Windows Defender / 杀软扫描文件时会锁定它，导致 ffmpeg 或
                    # Python 的 open() 撞上 WinError 5。str(e) 只有「拒绝访问」，
                    # 这里补上用户能理解的上下文。
                    return self._err(500, (
                        f"文件访问被拒绝：{e}\n"
                        "这是杀毒软件（Windows Defender 等）正在扫描文件导致的。\n"
                        "请稍等几秒后重试，或将文件所在目录加入杀毒软件白名单。"
                    ))
                except Exception as e:
                    return self._err(500, str(e))

            # ── QR 圖（segno → PNG）───────────────────────────
            def _qr(self, query):
                from urllib.parse import parse_qs
                data = (parse_qs(query).get("d", [""])[0] or "").strip()
                png = server.backend.make_qr(data) if data else None
                if not png:
                    return self._err(404, "qr unavailable")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(png)

            # ── 靜態檔 ────────────────────────────────────────
            def _static(self, path):
                if path in ("", "/"):
                    path = "/index.html"
                target = (WEB_DIR / path.lstrip("/")).resolve()
                # 防目錄穿越：必須仍在 WEB_DIR 內
                if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                    return self._err(403, "forbidden")
                if not target.is_file():
                    return self._err(404, "not found")
                data = target.read_bytes()
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            # ── SSE 進度/狀態串流 ─────────────────────────────
            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = server.hub.subscribe()
                # 連上時先補一次目前狀態，避免錯過載入完成事件
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    init = {"event": "status",
                            "payload": {"modelReady": bool(getattr(server.backend.engine, "ready", False))}}
                    self.wfile.write(f"data: {json.dumps(init, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")     # 心跳，維持連線
                            self.wfile.flush()
                            continue
                        self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server.hub.unsubscribe(q)

            # ── multipart 上傳（轉錄與切分預檢共用）────────────
            def _receive_upload(self):
                """Write the upload straight to a temp file and free all copies.

                The upload is a full copy of a possibly multi-GB lecture.
                Dropping `body` / `parts` / `data` before the multi-hour run
                starts removes the three-full-copy baseline that used to stay
                resident for the entire transcription.
                """
                from api_server import _parse_multipart
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                    return None, None, self._err(400, "需以 multipart/form-data 上傳 file")
                boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                fields, files = _parse_multipart(body, boundary)
                if "file" not in files or not files["file"][1]:
                    del body, fields, files
                    return None, None, self._err(400, "缺少 file 或內容為空")
                filename, data = files["file"]

                tmp_dir = Path(tempfile.mkdtemp(prefix="asr_web_"))
                ext = Path(filename).suffix or ".wav"
                in_path = tmp_dir / ("upload" + ext)
                try:
                    in_path.write_bytes(data)
                except PermissionError as e:
                    # Windows Defender / 杀软扫描 %TEMP% 时会锁定新建的文件，
                    # write_bytes 撞上 WinError 5。这里必须说清楚是临时目录的
                    # 问题，否则前端只显示「[WinError 5] 拒绝访问」，用户以为
                    # 是原始文件的问题。
                    try:
                        import shutil as _sh
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    return None, None, self._err(500, (
                        f"无法写入临时文件（{tmp_dir}）：{e}\n"
                        "这是杀毒软件（Windows Defender 等）正在扫描 %TEMP% 导致的。\n"
                        "请稍等几秒后重试，或将 %TEMP% 目录加入杀毒软件白名单。"
                    ))
                except OSError as e:
                    try:
                        import shutil as _sh
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    return None, None, self._err(500, (
                        f"无法写入临时文件（{tmp_dir}）：{e}\n"
                        "请检查磁盘空间是否充足，或 %TEMP% 目录是否有写入权限。"
                    ))
                opts = {
                    "path": str(in_path),
                    "name": filename,
                    "language": (fields.get("language") or "").strip() or None,
                    "diarize": (fields.get("diarize", "") or "").lower() in ("1", "true", "on", "yes"),
                    "nSpeakers": fields.get("n_speakers", ""),
                    "align": (fields.get("align", "1") or "").lower() in ("1", "true", "on", "yes"),
                    "hint": fields.get("hint", ""),
                    "trial": (fields.get("trial", "") or "").lower() in ("1", "true", "on", "yes"),
                    "vadThreshold": fields.get("vad_threshold", ""),
                }
                del body, fields, files, data
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass
                return tmp_dir, opts, None

            def _cleanup_upload(self, tmp_dir):
                """刪掉本次上傳的臨時目錄；失敗要說出來，不能靜默。

                以前是 ``shutil.rmtree(tmp_dir, ignore_errors=True)``。Windows 上
                這個呼叫很容易失敗：ffmpeg / crispasr 子程序剛被 kill、句柄尚未
                真正釋放，rmtree 立刻撞上 WinError 5。ignore_errors 把它吞掉，
                結果是**每轉錄一次就在 %TEMP% 留一份完整音檔**（幾百 MB 到幾 GB
                的講座音檔），跑幾次就把系統碟塞滿，而且完全沒有痕跡。
                現在改成：重試幾次（給作業系統一點時間放手），仍失敗就記進
                run log —— 讓使用者知道去哪清，也讓我們知道它是真故障。
                """
                if not tmp_dir:
                    return
                import shutil
                import time as _t
                last = None
                for attempt in range(4):
                    try:
                        shutil.rmtree(tmp_dir)
                        return
                    except FileNotFoundError:
                        return                      # 已經不見了 = 目的達成
                    except OSError as exc:
                        last = exc
                        # WinError 5/32（被占用）都是暫態，退讓後再來一次
                        _t.sleep(0.15 * (attempt + 1))
                    except Exception as exc:        # 非預期錯誤：沒必要重試
                        last = exc
                        break
                try:
                    server.hub.publish("warning", {
                        "code": "temp_cleanup",
                        "message": (f"暫存檔清理失敗，可手動刪除：{tmp_dir}"
                                    f"（{type(last).__name__}: {last}）"),
                    })
                except Exception:
                    pass

            def _batch_add(self):
                """Upload one file into the persistent server-side queue.

                Reuses _receive_upload() so the multipart body is released
                immediately (Wave 1.5 memory fix); the file is then copied by
                BatchQueue.add() into longform_state/batch_uploads/ and this
                handler's temp dir is removed right away.
                """
                tmp_dir, opts, err = self._receive_upload()
                if err is not None:
                    return err
                try:
                    item = server.batch_queue.add(
                        Path(opts["path"]), opts["name"], opts)
                    return self._json({"ok": True, "item": item})
                except Exception as exc:
                    return self._err(500, str(exc))
                finally:
                    self._cleanup_upload(tmp_dir)

            def _transcribe(self):
                tmp_dir, opts, err = self._receive_upload()
                if err is not None:
                    return err
                try:
                    result = server.backend.transcribe(
                        opts, progress_cb=lambda pct, status:
                        server.hub.publish("progress", {"pct": pct, "status": status}))
                    return self._json(result)
                finally:
                    self._cleanup_upload(tmp_dir)

            def _vad_preview(self):
                tmp_dir, opts, err = self._receive_upload()
                if err is not None:
                    return err
                try:
                    result = server.backend.vad_preview(opts)
                    return self._json(result)
                finally:
                    self._cleanup_upload(tmp_dir)

        self._httpd = ThreadingHTTPServer((self.host, self._want_port), _Handler)
        self.port = self._httpd.server_address[1]      # 取得實際綁定的埠（port=0 時）
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


if __name__ == "__main__":
    # 獨立啟動（除錯用）：起 server，印出網址。只有「選擇的模型已下載」才自動
    # 載入（與 app_webview 一致：首次/未下載 → 等使用者於模型頁按下載，避免硬抓）。
    srv = WebViewServer(port=8765)
    srv.start()
    if srv.backend.selected_model_present():
        srv.backend.start_load()
    print(f"WebView server: {srv.url}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
