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

# ── 首屏語言注入 ─────────────────────────────────────────────────────
# index.html 是繁體寫死的骨架；若等 app.js 讀完 settings 再切換，使用者會看到
# 「先繁中、再跳成設定的語言」。所以輸出 HTML 時就把設定語言寫進
# <html data-locale lang>，i18n.js 在 head 同步解析後即可直接套用。
_LOCALE_MAP = {
    "hant": "hant", "hans": "hans", "en": "en",
    "繁體中文": "hant", "简体中文": "hans", "簡體中文": "hans", "English": "en",
}
_LOCALE_LANG = {"hant": "zh-Hant", "hans": "zh-Hans", "en": "en"}


def _inject_locale(data: bytes, server) -> bytes:
    """把設定裡的介面語言寫進 index.html 的 <html> 標籤。"""
    try:
        raw = str(server.backend._settings_raw().get("ui_lang", "") or "")
    except Exception:
        raw = ""
    loc = _LOCALE_MAP.get(raw, "hant")
    if loc == "hant":
        return data                       # HTML 預設就是繁中，不用改
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    html = html.replace('data-locale="hant"', f'data-locale="{loc}"', 1)
    html = html.replace('lang="zh-Hant"', f'lang="{_LOCALE_LANG[loc]}"', 1)
    return html.encode("utf-8")


# ── %TEMP% 殘留清理 ──────────────────────────────────────────────────
# 上傳轉檔會把整份音檔複製到 %TEMP%\asr_web_xxxx\，正常路徑由
# _cleanup_upload() 在請求結束時刪掉。但「崩潰 / 被工作管理員結束 / rmtree 撞上
# 檔案佔用」三種情況都會留下完整副本，而且沒有痕跡 —— 實測一台機器累積
# 5 個目錄、1.0 GB。所以啟動時補一次掃描，設定頁也給一個手動按鈕。
# 只認得自己的前綴，絕不碰其他程式的暫存目錄。
TMP_OWNED_PREFIXES = ("asr_web_", "asr_api_")
TMP_SWEEP_AGE_SEC = 6 * 3600          # 只清「上一次執行」留下的，不動正在寫的


def sweep_stale_uploads(min_age: float = TMP_SWEEP_AGE_SEC,
                        root: Path | None = None) -> dict:
    """刪掉 %TEMP% 下本程式遺留的上傳副本。

    回傳 ``{"removed","freed","kept","failed"}``。**永不拋例外**：這是開機流程
    與設定頁按鈕共用的清理，不能因為某個目錄被鎖住就讓程式起不來。
    """
    import shutil
    import time as _t
    res = {"removed": 0, "freed": 0, "kept": 0, "failed": 0}
    base = Path(root) if root else Path(tempfile.gettempdir())
    now = _t.time()
    for prefix in TMP_OWNED_PREFIXES:
        try:
            cands = list(base.glob(prefix + "*"))
        except OSError:
            continue
        for d in cands:
            try:
                if not d.is_dir():
                    continue
                if now - d.stat().st_mtime < min_age:
                    res["kept"] += 1
                    continue
                size = 0
                for f in d.rglob("*"):
                    try:
                        if f.is_file():
                            size += f.stat().st_size
                    except OSError:
                        pass
                shutil.rmtree(d)
                res["removed"] += 1
                res["freed"] += size
            except FileNotFoundError:
                continue
            except OSError:
                res["failed"] += 1
    return res


def prune_checkpoints(days: int, state_dir: Path | None = None) -> dict:
    """按「保留天數」清掉久未續跑的斷點記錄（longform_state）。

    與上一版的分工不同：**任何狀態都適用**。一個 paused 的任務放了 90 天
    沒人續跑，留著也只是佔空間；``days <= 0`` 表示使用者選了永久保留，
    直接不動。

    判斷依據是 checkpoint 的 ``updated_at``（沒有這一列時退回 jsonl 的 mtime）。
    只刪狀態（DB 列 + ``*.segments.jsonl``），**絕不動 ``subtitles/`` 的成品**。

    永不拋例外：這是開機流程的一部分，不能被一個壞掉的 DB 卡住。
    """
    import sqlite3
    import time as _t
    res = {"removed": 0, "freed": 0, "days": int(days or 0)}
    d = Path(state_dir) if state_dir else app_paths.user_path("longform_state")
    if not d.is_dir() or int(days or 0) <= 0:
        return res
    cutoff = _t.time() - int(days) * 86400
    db = d / "checkpoints.db"
    rows = []
    if db.is_file():
        try:
            con = sqlite3.connect(str(db), timeout=10)
            try:
                rows = list(con.execute(
                    "SELECT task_id, updated_at FROM progress"))
            finally:
                con.close()
        except sqlite3.Error:
            rows = []                 # 讀不到就只處理「沒有斷點列」的孤兒檔
    known = set()
    suffix = ".segments.jsonl"
    for tid, updated in rows:
        tid = str(tid); known.add(tid)
        try:
            if float(updated or 0) >= cutoff:
                continue
        except (TypeError, ValueError):
            continue
        freed = 0
        jf = d / f"{tid}{suffix}"
        try:
            if jf.is_file():
                freed = jf.stat().st_size
                jf.unlink()
        except OSError:
            continue                  # 檔案被佔用：整條記錄留著，下次再試
        try:
            con = sqlite3.connect(str(db), timeout=10)
            try:
                con.execute("DELETE FROM progress WHERE task_id=?", (tid,))
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass
        res["removed"] += 1
        res["freed"] += freed
    # 沒有斷點列的孤兒 jsonl（舊版殘留 / 清過 DB 但沒清檔）
    for f in d.glob("*" + suffix):
        tid = f.name[:-len(suffix)]
        if tid in known:
            continue
        try:
            if f.stat().st_mtime >= cutoff:
                continue
            size = f.stat().st_size
            f.unlink()
            res["removed"] += 1
            res["freed"] += size
        except OSError:
            continue
    return res


def pick_local_files() -> list[str]:
    """Open a native Windows multi-select dialog and return absolute paths.

    A browser ``<input type=file>`` can never expose a Windows absolute path,
    so the batch page uses this server-side picker to hand real paths to
    ``BatchQueue.add_path()`` — zero copy, no upload, nothing to clean up.

    Pure stdlib ctypes on purpose: the classic common dialog needs no
    pywin32 / tkinter / COM typelibs, all of which are easy to miss in a
    PyInstaller ``--windowed`` build. Returns ``[]`` on cancel or on any
    non-Windows platform (the front end then falls back to the file input).
    """
    import sys
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class _OFN(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", ctypes.c_ssize_t),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(32768)
    filt = ctypes.create_unicode_buffer(
        "Audio / Video\0*.mp3;*.wav;*.m4a;*.flac;*.aac;*.ogg;*.wma;"
        "*.mp4;*.mkv;*.mov;*.avi;*.webm\0All files\0*.*\0")
    ofn = _OFN()
    ofn.lStructSize = ctypes.sizeof(_OFN)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrFilter = ctypes.cast(filt, wintypes.LPCWSTR)
    ofn.lpstrTitle = "選擇要加入批次的音訊／影片檔案"
    ofn.nFilterIndex = 1
    # OFN_ALLOWMULTISELECT | OFN_EXPLORER | OFN_FILEMUSTEXIST |
    # OFN_HIDEREADONLY | OFN_NOCHANGEDIR
    ofn.Flags = 0x00000200 | 0x00080000 | 0x00001000 | 0x00000004 | 0x00000008
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        ofn.hwndOwner = user32.GetForegroundWindow()
    except Exception:
        pass
    try:
        ctypes.windll.ole32.OleInitialize(None)
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception:
        return []
    finally:
        try:
            ctypes.windll.ole32.OleUninitialize()
        except Exception:
            pass
    if not ok:
        return []
    try:
        raw = ctypes.string_at(ctypes.addressof(buf),
                               len(buf) * ctypes.sizeof(ctypes.c_wchar))
        parts = [p for p in raw.decode("utf-16-le").split("\x00") if p]
    except Exception:
        return []
    if not parts:
        return []
    if len(parts) == 1:                 # single selection: full path
        return [parts[0]]
    base = parts[0]                      # multi selection: dir + names
    return [str(Path(base) / name) for name in parts[1:]]


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
        # Wave 12／問題 4：開窗成功的可觀測訊號。Edge --app 的啟動行程常把
        # URL 交給瀏覽器主行程後就以 exit 0 秒退，單看 proc.wait() 會誤判
        # 「視窗沒開」而重複開窗。真正能證明視窗開了的是「前端來連本機
        # server」：靜態頁請求或 /api/events 的 SSE 長連線。
        self.frontend_seen = threading.Event()
        self.sse_clients = 0
        self._client_lock = threading.Lock()
        # Wave 12／問題 3：原生檔案選擇器做成可注入，測試可換成假實作，
        # 不需真的在 Linux 沙箱裡彈 Windows 對話框。
        self.pick_files = pick_local_files

    def mark_frontend(self):
        """前端送來任一非健康檢查請求 → 視窗／頁面確實開了。"""
        self.frontend_seen.set()

    def sse_enter(self):
        with self._client_lock:
            self.sse_clients += 1
        self.frontend_seen.set()

    def sse_leave(self):
        with self._client_lock:
            self.sse_clients = max(0, self.sse_clients - 1)

    def window_signal(self) -> bool:
        """這次啟動的頁面是否曾連上本機服務（粘滯訊號）。"""
        return self.frontend_seen.is_set() or self.sse_clients > 0

    def window_alive(self) -> bool:
        """視窗是否還在：SSE 長連線仍掛著。"""
        return self.sse_clients > 0

    def start(self):
        if self._httpd:
            return
        server = self

        # 開機先掃一次上次執行的暫存殘留（崩潰／被強制結束留下的整份音檔）。
        # 失敗只記錄，不影響啟動。
        try:
            swept = sweep_stale_uploads()
            if swept["removed"] or swept["failed"]:
                import ui_text as _u
                note = _u.render("set.tempDone", {
                    "n": swept["removed"],
                    "mb": f"{swept['freed'] / (1024 * 1024):.0f}"})
                if swept["failed"]:
                    note += f"（{swept['failed']} 個仍被占用）"
                self.backend.run_log.add(note, level="info", source="temp")
        except Exception:
            pass
        # 依「保留天數」清理久未續跑的斷點記錄（設定頁可調，0 = 永久保留）。
        try:
            pruned = prune_checkpoints(self.backend._ckpt_days(),
                                       state_dir=self.backend.state_dir())
            if pruned["removed"]:
                self.backend.run_log.add(
                    f"longform_state：清理 {pruned['removed']} 份斷點記錄、"
                    f"釋放 {pruned['freed'] / (1024 * 1024):.0f} MB"
                    f"（保留 {pruned['days']} 天）",
                    level="info", source="temp")
        except Exception:
            pass

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
                # Wave 11：非 multipart 的請求體在 do_POST 開頭已統一讀進
                # self._post_body。這裡只解析、不再讀 socket —— 否則
                # 「不讀 body 的路由」（例如 /api/batch/start）會把前端送的
                # `{}` 留在 keep-alive 連線裡，殘留位元組被當成下一個請求行，
                # 讓緊接著的 GET /api/batch 得到 501 Unsupported method。
                raw = getattr(self, "_post_body", None)
                if raw is None:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(n) if n else b""
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
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
                # Wave 12：前端真的載進來才會打這些請求。/health 排除，
                # 那是 CI／外部監控用的，不能拿來證明視窗開了。
                if path != "/health":
                    server.mark_frontend()
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
                if path == "/api/checkpoints":
                    # 唯讀：條數 / 占用 / 明細（設定頁「斷點記錄」）
                    return self._json(server.backend.checkpoints_info())
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
                # Wave 11（佇列狀態 bug 的真正根因）：HTTP/1.1 keep-alive 下，
                # 客戶端送的 `{}`（apiPost 一律帶 body）若不被讀走，會殘留在
                # 連線裡，被伺服器當成「下一個請求」的方法行 → 501。
                # 曾在真機復現：POST /api/batch/start 後緊接的 GET /api/batch
                # 得到 501，前端 getBatch() 降級成 items:[]，整個佇列被清空，
                # 只剩 worker 推回來的第一個 item（使用者看到「只剩一個」）。
                # 解法：非 multipart 的請求在此統一讀乾淨，之後所有分支都用
                # self._post_body；multipart 交給 _receive_upload() 自行讀。
                ctype = self.headers.get("Content-Type", "")
                if "multipart/form-data" in ctype:
                    self._post_body = None
                else:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                    try:
                        self._post_body = self.rfile.read(n) if n else b""
                    except OSError:
                        self._post_body = b""
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
                        # Wave 14：上傳路由已移除。批次頁只走本機路徑直引
                        # （/api/batch/add-path），不複製、不上傳、不產生副本。
                        return self._err(410, "批次加入已改為本機路徑直引，"
                                         "請改用點擊選檔或 /api/batch/add-path")
                    if path == "/api/batch/add-path":
                        # Wave 11：直接引用本機檔案，不複製、不上傳。
                        body = self._read_json_body()
                        raw = str(body.get("path", "")).strip()
                        try:
                            item = server.batch_queue.add_path(
                                raw, body.get("name"), body.get("opts") or {})
                            return self._json({"ok": True, "item": item})
                        except ValueError as e:
                            return self._err(400, str(e))
                    if path == "/api/catalog/verify":
                        body = self._read_json_body()
                        bid = (body or {}).get("id") if isinstance(body, dict) else None
                        if not bid:
                            return self._err(400, "missing id")
                        return self._json(server.backend.verify_catalog_bundle(bid))
                    if path == "/api/catalog/open":
                        body = self._read_json_body()
                        bid = (body or {}).get("id") if isinstance(body, dict) else None
                        if not bid:
                            return self._err(400, "missing id")
                        return self._json(server.backend.open_catalog_dir(bid))
                    if path == "/api/batch/start":
                        # 開始當下的音檔頁設定（語言／分離／詞級優化／提示詞／
                        # VAD／每段秒數）隨請求帶入，整條佇列鎖定同一組值。
                        # body 在 do_POST 開頭已統一讀進 _post_body，這裡只解析。
                        body = self._read_json_body()
                        opts = body.get("opts") if isinstance(body, dict) else None
                        return self._json({"ok": True,
                                           "summary": server.batch_queue.start(opts)})
                    if path == "/api/batch/cancel":
                        return self._json({"ok": True,
                                           "summary": server.batch_queue.cancel()})
                    if path == "/api/batch/align/mark":
                        # 只標記，不啟動：勾選的列改成「補做對齊」任務。
                        body = self._read_json_body() or {}
                        ids = [str(x) for x in (body.get("ids") or [])]
                        n = server.batch_queue.mark_align(ids)
                        return self._json({"ok": True, "marked": n,
                                           "snapshot": server.batch_queue.snapshot()})
                    if path == "/api/batch/align/unmark":
                        body = self._read_json_body() or {}
                        ids = body.get("ids")
                        n = server.batch_queue.unmark_align(
                            None if ids is None else [str(x) for x in ids])
                        return self._json({"ok": True, "unmarked": n,
                                           "snapshot": server.batch_queue.snapshot()})
                    if path == "/api/batch/rebuild/srt":
                        # 只重寫 SRT（用既有 rich.json），毫秒級；不跑對齊。
                        body = self._read_json_body() or {}
                        _id = str(body.get("id", ""))
                        it = next((x for x in list(server.batch_queue.items)
                                   if x.get("id") == _id), None)
                        if it is None:
                            return self._json({"ok": False,
                                               "error": "找不到這一列"})
                        try:
                            r = server.backend.rebuild_srt_only(
                                it.get("opts") or {"path": it.get("path"),
                                                   "name": it.get("name")})
                        except Exception as exc:
                            return self._json({"ok": False, "error": str(exc)})
                        return self._json(r)
                    if path == "/api/batch/align/probe":
                        # 兩段式：先回 L0/L2（毫秒級），L3 三窗試對齊丟背景，
                        # 逐項以 SSE "alignprobe" 把黃標刷成綠/紅。
                        import align_backfill
                        import threading as _threading
                        body = self._read_json_body() or {}
                        want = {str(x) for x in (body.get("ids") or [])}
                        out = []
                        for it in list(server.batch_queue.items):
                            if it.get("id") not in want:
                                continue
                            st = {"id": it["id"], "level": "blocked",
                                  "segments": 0, "reason": ""}
                            try:
                                audio, srt = server.backend.align_paths(
                                    it.get("opts") or {"path": it.get("path"),
                                                       "name": it.get("name")})
                            except Exception as exc:
                                st["reason"] = str(exc)
                                out.append(st)
                                continue
                            info = align_backfill.inspect(srt)
                            if not info.get("ok"):
                                st["reason"] = info.get("reason") or "缺產物"
                                out.append(st)
                                continue
                            st["segments"] = info["segments"]
                            dur = float(it.get("durationSec") or 0)
                            if not dur:
                                st["level"] = "unverified"
                                st["reason"] = "音檔長度尚未探測，僅確認產物存在"
                            else:
                                delta = abs(dur - info["duration"])
                                if delta <= max(10.0, info["duration"] * 0.001):
                                    st["level"] = "lenient"
                                    st["reason"] = f"長度一致（差 {delta:.1f}s），不保證同一檔案"
                                else:
                                    st["level"] = "blocked"
                                    st["reason"] = f"時長不符（差 {delta:.0f}s）"
                            out.append(st)

                            def _bg(_id=it["id"], _a=audio, _s=srt):
                                r = align_backfill.probe(_a, _s)
                                try:
                                    server.hub.publish("alignprobe",
                                                       {"id": _id, **r})
                                except Exception:
                                    pass
                            try:
                                _threading.Thread(target=_bg, daemon=True,
                                                  name=f"fa-probe-{it['id']}").start()
                            except Exception:
                                pass
                        return self._json({"ok": True, "items": out})
                    if path == "/api/batch/item/cancel":
                        body = self._read_json_body() or {}
                        return self._json({"ok": server.batch_queue.cancel_item(
                            str(body.get("id", "")),
                        ), "snapshot": server.batch_queue.snapshot()})
                    if path == "/api/batch/remove":
                        body = self._read_json_body()
                        return self._json({"ok": server.batch_queue.remove(
                            str(body.get("id", "")))})
                    if path == "/api/batch/retry":
                        body = self._read_json_body()
                        return self._json({"ok": server.batch_queue.retry(
                            str(body.get("id", "")))})
                    if path == "/api/batch/move":
                        # 佇列排序：index 為目標位置；promote=true 時移到最前面
                        # 的 pending 之前（已執行中的那列不動）。
                        body = self._read_json_body()
                        _id = str(body.get("id", ""))
                        if body.get("promote"):
                            ok = server.batch_queue.promote(_id)
                        else:
                            ok = server.batch_queue.move(
                                _id, int(body.get("index", 0) or 0))
                        return self._json({"ok": ok,
                                           "summary": server.batch_queue.snapshot()})
                    if path == "/api/logs/clear":
                        return self._json(server.backend.clear_logs())
                    if path == "/api/cleanup-temp":
                        # 設定頁「立即清理」：清 %TEMP% 下本程式遺留的副本。
                        # 只清 10 分鐘前的，避免刪掉此刻正在寫入的那一份。
                        return self._json(server.backend.cleanup_temp())
                    if path == "/api/checkpoints/clear":
                        # 只清斷點狀態，不動 subtitles/ 的成品
                        return self._json(server.backend.clear_checkpoints())
                    if path == "/api/transcribe-path":
                        # 單檔頁「點擊選檔」走原生對話框 → 直接引用本機路徑，
                        # 不複製、不上傳、不產生 %TEMP% 副本（與批次頁同一套）。
                        body = self._read_json_body() or {}
                        opts = {
                            "path": str(body.get("path", "")),
                            "name": body.get("name") or None,
                            "language": (body.get("language") or "").strip() or None,
                            "diarize": bool(body.get("diarize")),
                            "nSpeakers": body.get("nSpeakers") or "",
                            "align": bool(body.get("align", True)),
                            # FA 流水线开关（識別頁的「嘗試並行」）
                            "alignParallel": bool(body.get("alignParallel")),
                            "hint": body.get("hint") or "",
                            "trial": bool(body.get("trial")),
                            "vadThreshold": body.get("vadThreshold") or "",
                        }
                        try:
                            result = server.backend.transcribe(
                                opts, progress_cb=lambda pct, status:
                                server.hub.publish("progress",
                                                   {"pct": pct, "status": status}))
                            return self._json(result)
                        except PermissionError as e:
                            return self._err(403, f"沒有讀取權限：{e}")
                        except FileNotFoundError as e:
                            return self._err(404, f"找不到檔案：{e}")
                    if path == "/api/release-model":
                        # Wave 12／問題 1：手動釋放模型記憶體。
                        return self._json(server.backend.release_model())
                    if path == "/api/pick-files":
                        # Wave 12／問題 3：本機原生選檔 → 絕對路徑 → add_path。
                        import sys as _sys
                        try:
                            paths = server.pick_files() or []
                        except Exception as exc:
                            return self._err(500, str(exc))
                        return self._json({"ok": True, "paths": paths,
                                           "supported": _sys.platform == "win32",
                                           "count": len(paths)})
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
                    # WinError 5（拒绝访问）的成因很多，不能一概归咎于「杀软
                    # 扫描文件」—— 那会误导使用者去关掉杀软却毫无作用。
                    # 这里只描述事实、给出诊断线索。
                    return self._err(500, (
                        f"操作被拒绝：{e}\n"
                        "WinError 5（拒绝访问）可能来自：\n"
                        "  - 当前账号对目标目录/文件没有相应权限\n"
                        "  - Windows 的受控文件夹访问（Controlled Folder Access）拦截\n"
                        "  - 杀毒软件 / EDR 阻止了进程创建或文件读写\n"
                        "  - AppLocker / WDAC / Smart App Control 策略阻止\n"
                        "请把完整错误消息反馈给开发者，便于定位。"
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
                # 首屏語言：只改 HTML 入口，其餘靜態檔原樣輸出。
                if target.name == "index.html":
                    data = _inject_locale(data, server)
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
                server.sse_enter()      # Wave 12：SSE 連上 = 視窗開著
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
                    server.sse_leave()
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
                    # Windows：WinError 5 的成因很多（exe 所在目录无执行权限、
                    # AppLocker/WDAC 策略、受控文件夹访问、安全软件拦截子进程…）。
                    # **不能一概归咎于杀软扫描** —— 那会误导使用者去关掉杀软、
                    # 加白名单，结果什么都没变。这里只描述事实、给出诊断线索。
                    try:
                        import shutil as _sh
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    return None, None, self._err(500, (
                        f"无法写入临时文件：{e}\n"
                        f"临时目录：{tmp_dir}\n"
                        f"目标文件：{in_path}\n"
                        "WinError 5（拒绝访问）可能来自：\n"
                        "  - 当前账号对该目录没有「写入」权限\n"
                        "  - Windows 的受控文件夹访问（Controlled Folder Access）拦截\n"
                        "  - 杀毒软件 / EDR 阻止了本进程创建文件\n"
                        "  - 磁盘空间不足或文件系统错误\n"
                        "请把「临时目录」一并反馈给开发者，便于定位。"
                    ))
                except OSError as e:
                    try:
                        import shutil as _sh
                        _sh.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    return None, None, self._err(500, (
                        f"无法写入临时文件：{e}\n"
                        f"临时目录：{tmp_dir}\n"
                        f"目标文件：{in_path}\n"
                        "请检查磁盘空间是否充足、目录是否有写入权限。"
                    ))
                opts = {
                    "path": str(in_path),
                    "name": filename,
                    # 標記「這是上傳的副本」：它的路徑每次都不一樣（隨機臨時
                    # 目錄），不能拿來當任務身分，否則取消後重選同一支檔案
                    # 會變成另一個任務、斷點永遠接不上。後端改用內容身分。
                    "uploaded": True,
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
                # 與 /api/transcribe 一樣有兩種來源：
                #  · JSON {path}  → 原生對話框選檔，直接引用本機路徑（零拷貝）
                #  · multipart file → 拖放上傳的 File 物件
                # 從前這裡只認 multipart，點「選擇檔案」時前端把
                # {path, name} 這個普通物件塞進 FormData.append("file", …)，
                # 瀏覽器直接拋 "parameter 2 is not of type 'Blob'" —— 那是
                # 前端的錯，但要讓點選檔這條主路径可用，後端得先接住 JSON。
                if "application/json" in self.headers.get("Content-Type", ""):
                    body = self._read_json_body()
                    opts = {
                        "path": str(body.get("path", "") or ""),
                        "name": body.get("name") or None,
                        "vadThreshold": body.get("vadThreshold") or "",
                    }
                    return self._json(server.backend.vad_preview(opts))
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
