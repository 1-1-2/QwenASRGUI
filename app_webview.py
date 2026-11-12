"""app_webview.py - desktop launcher (local HTTP server + browser window).

Window strategy (Wave 3 / G1)
-----------------------------
The default window is the system Microsoft Edge in ``--app`` + ``--inprivate``
mode. It needs no .NET assembly, no pythonnet, no WebView2 SDK and no
``Mark-of-the-Web`` workaround: the original onefile build existed only because
loose DLLs inherited MOTW and .NET refused to load them. Removing pythonnet
removes that whole failure mode and makes an onedir / layered layout possible.

The native pywebview/WebView2 window is still available as an **opt-in** path
for source installs via ``QWENASR_NATIVE_WINDOW=1``. The release EXE no longer
bundles pywebview/pythonnet, so enabling it there is expected to fail and fall
back to Edge --app automatically.

Startup order: local server (127.0.0.1 only) -> optional background model load
-> Edge --app window -> default browser as last resort.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

import app_paths
from proc_guard import setup_kill_on_close_job
from single_instance import SingleInstance
try:
    from webview_server import WebViewServer
    _SERVER_IMPORT_ERR = None
except ImportError as _exc:      # --selftest must work without GUI deps
    WebViewServer = None
    _SERVER_IMPORT_ERR = str(_exc)
    # 為何在這裡就喊出來：打包版一旦漏收模組（例如 webview_backend 依賴的
    # ui_text 沒被 PyInstaller 收集），匯入鏈就會斷。原本這個 ImportError 被
    # 靜默吞掉，一路走到 main() 才在 WebViewServer(...) 炸成
    #   TypeError: 'NoneType' object is not callable
    # —— 真正的凶手（少了哪個模組）完全看不到。故匯入期就寫進日誌。
    # 刻意走 stderr：--selftest 的 stdout 是契約 JSON，任何雜訊都不能摻進去。
    try:
        print(f"[app_webview] 無法匯入 webview_server：{_exc}", file=sys.stderr)
    except Exception:
        pass

APP_NAME = "聲音辨識小工具"
WIN_W, WIN_H = 1180, 820


# ════════════════════════════════════════════════════════
# stdio 兜底：windowed 打包下 sys.stdout / sys.stderr 是 None
# ════════════════════════════════════════════════════════
#   PyInstaller --windowed（無主控台 EXE）啟動時，Python 的標準串流會被設成
#   None —— 此時任何 print() 或 sys.stdout.flush() 都會：
#     AppError: sys.stdout is None  /  AttributeError: 'NoneType' object has no attribute 'flush'
#   開發模式從 cmd 執行時一定有真實的主控台，所以這個問題只在打包版才炸，
#   而且炸在最外層的 main() 收尾（單一實例、關窗退出都走那條路）。
#   修法：啟動最早期把 None 換成一個寫往日誌檔的替身，兼具兩個好處：
#     • 不再出現 NoneType 例外（診斷訊息也不會憑空消失）
#     • 使用者仍可從 %LOCALAPPDATA%\<APP_NAME>\launcher.log 追查啟動問題
#
#   「追查」的前提是能分清哪一行屬於哪一次啟動。單一 append 檔做不到：跑過
#   三次之後，三段日誌首尾相連，光看內容分不出哪段是閃退那次。所以每行都蓋
#   時間戳，並且每次啟動另外留一份帶時間戳的快照 —— 出問題只要把「最新那份
#   快照」整個發出來，不必在一坨歷史裡挑。
_LOG_SNAPSHOT_PREFIX = "launcher-"


class _LogFanout:
    """一次執行的日誌出口：穩定的 launcher.log ＋ 本次快照，每行蓋時間戳。

    為什麼保留兩個檔案而不是只留快照：
      • ``launcher.log`` 路徑固定 —— CI 冒煙測試與使用說明都靠它，改名等於
        把所有既有取證入口一起打斷；
      • 快照只含本次執行 —— 排查時要的就是「最近這一次」的完整上下文。
    兩者內容相同，只是顆粒度不同。

    時間戳在**寫入時**取，不是呼叫時取；`_at_bol` 記錄「是否在行首」，因此
    一次 print() 先寫內容、再寫換行的兩段 write 只會加一個前綴。traceback 那種
    一次寫一大段的情況，會逐行各蓋一個 —— 反而更容易看出是哪一行出事。
    """

    MAX_SNAPSHOTS = 12                    # 保留最近 12 次啟動，約幾百 KB

    def __init__(self, directory: Path, tag: str):
        self._lock = threading.Lock()
        self._files: list = []
        self._at_bol = True
        self.snapshot: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        main = directory / "launcher.log"
        try:
            # 上限約 1 MB：塞住磁碟比沒有日誌更糟。超標時輪換成 .1 而不是刪掉
            # —— 前一次崩潰往往正是要對照的那一次。
            if main.exists() and main.stat().st_size > 1_048_576:
                main.replace(directory / "launcher.log.1")
        except Exception:
            pass
        try:
            # 带 PID：同一秒內啟動兩次（例如 --selftest 子行程與主行程）時
            # 檔名仍唯一，不會兩個行程交錯寫同一份快照。
            self.snapshot = directory / f"{_LOG_SNAPSHOT_PREFIX}{tag}-p{os.getpid()}.log"
        except Exception:
            self.snapshot = None
        for p in (main, self.snapshot):
            if p is None:
                continue
            try:
                self._files.append(
                    open(p, "a", encoding="utf-8", errors="replace"))
            except Exception:
                pass
        self._prune(directory)

    def _prune(self, directory: Path):
        try:
            olds = sorted(directory.glob(f"{_LOG_SNAPSHOT_PREFIX}*.log"))
        except Exception:
            return
        for p in olds[:-self.MAX_SNAPSHOTS]:
            if p == self.snapshot:
                continue                  # 絕不刪掉正在寫的這一份
            try:
                p.unlink()
            except Exception:
                pass

    @staticmethod
    def _prefix() -> str:
        now = time.time()
        return "[%s.%03d] " % (time.strftime("%m-%d %H:%M:%S", time.localtime(now)),
                               int((now % 1) * 1000))

    def write(self, s: str) -> int:
        if not s:
            return 0
        with self._lock:
            parts = []
            for line in s.splitlines(keepends=True):
                if self._at_bol and line.strip():
                    parts.append(self._prefix())
                    self._at_bol = False
                parts.append(line)
                if line.endswith(("\n", "\r")):
                    self._at_bol = True
            out = "".join(parts)
            for f in self._files:
                try:
                    f.write(out)
                    f.flush()             # 閃退時緩衝區不落地 = 白記
                except Exception:
                    pass
        return len(s)

    def flush(self):
        with self._lock:
            for f in self._files:
                try:
                    f.flush()
                except Exception:
                    pass


class _NullSafeStream:
    """包住真實串流；真實串流為 None 或已關閉時靜默丟棄（可選寫入日誌檔）。"""

    def __init__(self, real, fanout: "_LogFanout | None" = None):
        self._real = real
        self._log = fanout

    # io.TextIOBase 的常用介面（print / flush 只會用到這幾個）
    def write(self, s):
        if self._real is not None:
            try:
                self._real.write(s)
            except Exception:
                pass
        if self._log is not None:
            try:
                self._log.write(s)
                self._log.flush()
            except Exception:
                pass
        return len(s) if s else 0

    def flush(self):
        for target in (self._real, self._log):
            if target is not None:
                try:
                    target.flush()
                except Exception:
                    pass

    def isatty(self):
        try:
            return bool(self._real is not None and self._real.isatty())
        except Exception:
            return False

    def fileno(self):                             # 供需要真實 fd 的第三方套件探測
        if self._real is None:
            raise OSError("no console stream")
        try:
            return self._real.fileno()
        except (AttributeError, OSError):
            # 探測方（如 isatty / colorama / tqdm）會把 OSError 當成「無 tty」處理，
            # 但 AttributeError 會直接往外炸 —— 統一轉成 OSError 更安全。
            raise OSError("no real file descriptor") from None

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8") or "utf-8"

    def __getattr__(self, name):                  # 其餘屬性一律委派給真實串流
        if self._real is None:
            raise AttributeError(name)
        return getattr(self._real, name)


def _log(msg: str):
    """启动期进度一律走 stderr（已由 hush_stdio 换成日志替身）。

    不用 stdout：--selftest 的 stdout 是契约 JSON，任何杂讯都会让它解析失败；
    而 CLI 模式又把 stdout 当机器可读输出。启动器的进度信息只属于人看。
    """
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


class _StartupClock:
    """启动链路分段计时 —— 让「开了 10 秒才出窗口」变成可以指名的那一行。

    为什么需要：使用者报「第二次启动前有 10s 的加载时间」，而我们能测到的
    服务端构造只有几十毫秒。剩下那 9.x 秒到底在 Edge、在单实例锁、还是在
    打完日志之后没人量的那一段，光看代码永远吵不出结论。把每一段都记进
    launcher.log，下次谁再问就有数可看，不必再猜。
    """

    def __init__(self):
        self._t0 = time.perf_counter()
        self._last = self._t0

    def mark(self, label: str) -> float:
        now = time.perf_counter()
        delta = (now - self._last) * 1000
        total = (now - self._t0) * 1000
        self._last = now
        _log(f"[启动计时] {label}: +{delta:.0f}ms（累计 {total:.0f}ms）")
        return total


def _log_dir() -> Path | None:
    """啟動器日誌目錄：%LOCALAPPDATA%\\<APP_NAME>（非 Windows 用家目錄）。"""
    try:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
        root = Path(base) if base else Path.home()
        return root / APP_NAME
    except Exception:
        return None


def hush_stdio():
    """把 None 的 stdout／stderr 換成安全替身。必須在任何 print 之前呼叫。"""
    if sys.stdout is not None and sys.stderr is not None:
        return                                    # 有真實主控台（開發模式）→ 不動
    fanout = None
    d = _log_dir()
    if d is not None:
        try:
            fanout = _LogFanout(d, time.strftime("%Y%m%d-%H%M%S"))
            # 每次執行的分界線：一眼能看出這段屬於哪一次、哪個進程。
            # 少了它，兩次執行的日誌首尾相接時仍然分不清。
            fanout.write(
                f"===== {APP_NAME} 啟動 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                f"  PID {os.getpid()} =====\n")
        except Exception:
            fanout = None
    if sys.stdout is None:
        sys.stdout = _NullSafeStream(None, fanout)
    if sys.stderr is None:
        # stderr 與 stdout 共用同一組出口（同一個 fanout → 只有一份檔案句柄，
        # 兩個串流互相穿插時順序才不會亂）
        sys.stderr = _NullSafeStream(None, fanout)


hush_stdio()


def _install_crash_log():
    """最後兜底：未捕捉例外寫進 launcher.log，並提醒使用者看哪個檔案。

    windowed EXE 沒有主控台，未捕捉例外原本**完全無聲**（使用者只看到視窗沒開）。
    有了這支 handler，任何啟動期錯誤都會留下可回報的文字；stderr 已被前一步
    換成日誌替身，所以 traceback 會落進 launcher.log 與本次執行快照（每行都
    帶時間戳），同時彈一個說明框指出位置。
    """
    import traceback

    def _hook(exc_type, exc, tb):
        try:
            # 带完整链条：只写最后一行会丢掉「真正的元凶」（例如
            #   webview_backend → ffmpeg_utils → customtkinter → tkinter 缺 tkinter
            # 的关键信息全在中间那些 from 链里）。
            sys.stderr.write("".join(traceback.format_exception(exc_type, exc, tb)))
            sys.stderr.flush()
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                d = _log_dir()
                where = str(d / "launcher.log") if d else "launcher.log"
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"程式啟動時發生錯誤：\n{exc_type.__name__}: {exc}\n\n"
                    f"詳細記錄：{where}\n"
                    f"（每次啟動的獨立記錄在同一個資料夾的 launcher-*.log，"
                    f"把最新的那份附上來即可）",
                    APP_NAME, 0x00000010)          # MB_ICONERROR
        except Exception:
            pass

    sys.excepthook = _hook
    # 背景執行緒的例外不會經過 sys.excepthook（3.8 起改走 threading.excepthook）
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


_install_crash_log()


# ════════════════════════════════════════════════════════
# 標題列配色：配合淺色頁面主題 → 白底黑字
# ════════════════════════════════════════════════════════
#   原生視窗（WebView2）與 Edge --app 視窗的標題列由 OS（DWM）繪製，預設會
#   跟隨系統的深／淺色設定 —— 系統若為深色，標題列就是深底白字，與本程式
#   固定的淺色頁面主題不搭。Windows 11（build 22000+）提供 DWM 屬性可直接
#   指定標題列配色，故在視窗顯示後呼叫 DwmSetWindowAttribute 強制：
#     • DWMWA_USE_IMMERSIVE_DARK_MODE(20)=0 → 關閉深色模式（淺色標題列）
#     • DWMWA_CAPTION_COLOR(35)=白          → 標題列底色（COLORREF 0x00BBGGRR）
#     • DWMWA_TEXT_COLOR(36)=黑             → 標題列文字／圖示色
#   兩種視窗（原生 / Edge fallback）的標題都是頁面 <title>「聲音辨識」，故可用
#   同一支以視窗標題尋找 HWND 的輔助函式套用。失敗（舊版 Windows / 非 win32）
#   一律靜默略過，不影響視窗開啟。
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_COLOR_WHITE = 0x00FFFFFF        # COLORREF：白底（淺色標題列底）
_COLOR_BLACK = 0x00000000        # COLORREF：黑字
_COLOR_DARK_BG = 0x0026201E      # COLORREF 0x00BBGGRR ← #1E2026（深色面板底，與 CSS 一致）

_initial_theme = "light"         # main() 啟動時由設定填入，供視窗裝飾 worker 取用初始深淺


def _os_dark() -> bool:
    """讀 Windows 個人化設定判斷系統是否為深色（AppsUseLightTheme==0）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return v == 0
    except Exception:
        return False


def _resolve_dark(theme: str) -> bool:
    """外觀偏好（light/dark/system）→ 是否深色。"""
    if theme == "dark":
        return True
    if theme == "light":
        return False
    return _os_dark()              # system → 跟隨 OS


def _set_titlebar(hwnd: int, dark: bool) -> bool:
    """對 HWND 套用標題列配色：dark→深底白字、light→白底黑字。回傳是否設成底色。"""
    if not hwnd or sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
    except Exception:
        return False

    def _set(attr: int, value: int) -> int:
        v = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(
            wintypes_hwnd(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))

    cap = _COLOR_DARK_BG if dark else _COLOR_WHITE
    txt = _COLOR_WHITE if dark else _COLOR_BLACK
    ok = False
    try:
        _set(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    except Exception:
        pass
    try:
        if _set(_DWMWA_CAPTION_COLOR, cap) == 0:        # S_OK
            ok = True
        _set(_DWMWA_TEXT_COLOR, txt)
    except Exception:
        pass
    return ok


def apply_titlebar_theme(theme: str):
    """供 webview_backend 回呼：依新外觀設定即時切換視窗標題列深淺（兩種視窗皆套）。"""
    dark = _resolve_dark(theme or "light")
    hwnd = _find_app_hwnd()
    if hwnd:
        _set_titlebar(hwnd, dark)


def wintypes_hwnd(hwnd: int):
    """把 Python int 包成 DWM API 接受的 HWND（c_void_p）。"""
    return ctypes.c_void_p(int(hwnd))


def _find_app_hwnd() -> int:
    """以視窗標題（APP_NAME）尋找頂層視窗 HWND；找不到回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(None, APP_NAME) or 0)
    except Exception:
        return 0


# ────────────────────────────────────────────────────────
# 視窗 / 工作列圖示（貓耳毛絨小耳機）
# ────────────────────────────────────────────────────────
#   為何需要程式化設定：PyInstaller 的 --icon 只內嵌進「打包後的 EXE」，
#   開發模式 `python app_webview.py` 的宿主行程是 python.exe → 工作列顯示
#   python 預設圖示。pywebview 6.x 也沒有可靠的 per-window 圖示 API。故改用
#   Win32 `WM_SETICON` 在視窗建立後把 icon.ico 套上去（標題列小圖 + 工作列大圖），
#   並設定獨立的 AppUserModelID，讓工作列不與 python.exe 共用同一顆按鈕／圖示。
_APP_USER_MODEL_ID = "dseditor.QwenASR.VoiceTool"
_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE, _LR_DEFAULTSIZE = 0x00000010, 0x00000040


def set_app_user_model_id():
    """讓 Windows 把本行程視為獨立 App（工作列圖示不跟 python.exe 綁在一起）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except Exception:
        pass


def _resolve_icon_path() -> Path | None:
    """Find icon.ico: assets/ in a source tree, resource root when frozen."""
    for c in (app_paths.resource_path("icon.ico"),
              app_paths.resource_path("assets", "icon.ico"),
              Path(__file__).resolve().parent / "assets" / "icon.ico"):
        if c.exists():
            return c
    return None


def _set_window_icon(hwnd: int, ico: Path) -> bool:
    """以 WM_SETICON 把 icon.ico 套到視窗（同時更新標題列小圖與工作列大圖）。"""
    if not hwnd or sys.platform != "win32":
        return False
    try:
        u = ctypes.windll.user32
        # 設定 restype/argtypes：HICON 是 64 位 handle，未指定會被 ctypes 截成 int
        # → handle 失效。LoadImageW 回 HANDLE、SendMessageW 收 HWND/WPARAM/LPARAM。
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        path = str(ico)
        big = u.LoadImageW(None, path, _IMAGE_ICON, 0, 0,
                           _LR_LOADFROMFILE | _LR_DEFAULTSIZE)        # 大圖（工作列）
        small = u.LoadImageW(None, path, _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)  # 小圖（標題列）
        h = wintypes_hwnd(hwnd)
        if big:
            u.SendMessageW(h, _WM_SETICON, _ICON_BIG, big)
        if small:
            u.SendMessageW(h, _WM_SETICON, _ICON_SMALL, small)
        return bool(big or small)
    except Exception:
        return False


def _decorate_window_async():
    """背景輪詢，待視窗建立後套用淺色標題列 + 程式圖示（原生 / Edge 皆適用）。

    視窗建立有延遲，且 DWM／WM_SETICON 都需 HWND 存在後才能套 —— 故短輪詢，
    兩者皆完成（或逾時）即停。獨立 daemon 緒執行，不阻塞主流程。
    """
    ico = _resolve_icon_path()
    dark = _resolve_dark(_initial_theme)

    def worker():
        themed = False
        iconed = ico is None          # 無 icon 檔則視為「已處理」（只做標題列）
        for _ in range(40):           # 最多 ~6 秒（40 × 0.15s）
            hwnd = _find_app_hwnd()
            if hwnd:
                if not themed:
                    themed = _set_titlebar(hwnd, dark)
                if not iconed:
                    iconed = _set_window_icon(hwnd, ico)
                if themed and iconed:
                    return
            time.sleep(0.15)
    threading.Thread(target=worker, name="window-decorate", daemon=True).start()


# ════════════════════════════════════════════════════════
# 前置偵測：系統是否具備 WebView2 Runtime（原生視窗的前提）
# ════════════════════════════════════════════════════════
#   為何需要：缺 WebView2 Runtime 時，pywebview 仍會「開出視窗」但 WebView2
#   控制項初始化失敗 —— edgechromium 只記 log 後 return，**不丟例外、不關窗**，
#   於是 webview.start() 既不返回也不報錯 → 我們的 Edge fallback 永遠不會觸發，
#   使用者只看到一個空白視窗（像整個程式壞掉）。故啟動前先用官方註冊表鍵判定，
#   不具備就直接走 Edge --app（純 Chromium，不需 WebView2/.NET）。
#   （缺 .NET 的情況則由 run_native_window 內的例外處理乾淨退回 Edge。）
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_available() -> bool:
    """偵測系統是否已安裝 WebView2 Runtime（Evergreen 或 per-user）。"""
    if sys.platform != "win32":
        return False
    import winreg
    for hive, path in (
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
        (winreg.HKEY_CURRENT_USER,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
    ):
        try:
            with winreg.OpenKey(hive, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


# ════════════════════════════════════════════════════════
# 主要：原生 WebView2 視窗（pywebview，只載入網址、無 js_api）
# ════════════════════════════════════════════════════════
def run_native_window(url: str) -> bool:
    """以原生 WebView2 視窗載入 url，阻塞至關窗回 True；不可用/失敗回 False。"""
    try:
        import webview
    except Exception:
        return False
    try:
        # 允許下載：原生 WebView2 預設 ALLOW_DOWNLOADS=False 會「靜默取消」<a download>，
        # 導致「字幕存檔」按鈕沒有任何反應、也不跳存檔視窗。開啟後 edgechromium 會彈
        # 原生 Save 視窗讓使用者選位置。
        try:
            webview.settings['ALLOW_DOWNLOADS'] = True
        except Exception:
            pass
        webview.create_window(
            APP_NAME, url=url,
            width=WIN_W, height=WIN_H, min_size=(960, 680),
        )
        _decorate_window_async()   # 視窗顯示後背景套用白底黑字標題列 + 程式圖示
        webview.start()            # 阻塞至視窗關閉（在主執行緒）
        return True
    except Exception as e:
        _log(f"[{APP_NAME}] 原生視窗失敗，改用 Edge：{e}")
        return False


# ════════════════════════════════════════════════════════
# Fallback：系統 Microsoft Edge --app 無痕視窗
# ════════════════════════════════════════════════════════
def _find_edge() -> str | None:
    for c in [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]:
        if c and Path(c).is_file():
            return str(c)
    return None


def _profile_dir(variant: int = 0) -> str:
    """Edge 的專用 profile 目錄。

    Wave 11：variant>0 是重試用的備用目錄。固定 profile 若殘留
    SingletonLock（上次 Edge 沒乾淨退出），新啟動的 msedge.exe 會把
    --app URL 轉發給那個殘留實例、自己立刻 exit(0) —— 使用者看到的就是
    「視窗閃一下就沒了」，而 launcher.log 記為「退出碼 0，存活 0.0s」。
    換一個全新的 profile 目錄即可避開鎖競爭。

    只使用 0/1/2 三個固定名字，不會無限增生目錄（使用者先前明確不希望
    「每次啟動都產生新 profile」）。
    """
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    name = "edge-profile" if variant == 0 else f"edge-profile-r{variant}"
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _lock_owner_alive(p: Path) -> bool:
    """SingletonLock 內容是 ``hostname-pid``；該 pid 還活著就不能刪。

    Wave 12／問題 4：先前無條件 unlink，若另一個 Edge 執行個體正在使用
    同一個 profile，刪掉它的 ProcessSingleton 鎖會造成共享衝突，使用者
    看到的就是其中一個視窗「拒絕訪問」。寧可留著鎖、交給備用 profile
    隔離，也不要動活執行個體的鎖。
    """
    import re
    try:
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    m = re.search(r"-(\d+)\s*$", txt)
    if not m:
        return False
    pid = int(m.group(1))
    try:
        if sys.platform == "win32":
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            k.CloseHandle(wintypes.HANDLE(h))
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _clear_stale_singleton(profile_dir: str):
    """清除上次 Edge 沒乾淨退出留下的 ProcessSingleton 鎖（Wave 11／問題 2）。

    Chromium 會在 --user-data-dir 裡放 SingletonLock / SingletonSocket /
    SingletonCookie。上次進程崩潰或被強制結束後這些檔可能還在，新啟動的
    msedge.exe 會判定「這個 profile 已有實例」，把 --app URL 轉發過去後
    自己立刻 exit(0) —— launcher.log 裡就是「退出碼 0，存活 0.0s」，
    使用者看到的是冷啟動閃一下就沒了。

    本機以 Chromium + 固定 --user-data-dir 可穩定重現：
      Failed to create <profile>/SingletonLock: File exists (17)
      Failed to create a ProcessSingleton for your profile directory...
      Aborting now to avoid profile corruption.

    只刪這三個已知檔名，不碰 profile 其他內容；若檔案真的被另一個 Edge
    行程佔住，unlink 會失敗，我們記一行日誌後交給 _run_edge_session()
    換備用 profile 重試，不強行破壞。
    """
    root = Path(profile_dir)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = root / name
        try:
            if p.is_symlink() or p.exists():
                if name == "SingletonLock" and p.is_file() and _lock_owner_alive(p):
                    _log(f"[{APP_NAME}] {name} 屬於仍在執行的 Edge，跳過不刪（避免拒絕訪問）")
                    continue
                p.unlink()
                _log(f"[{APP_NAME}] 已清除殘留的 {name}（{root.name}）")
        except OSError as e:
            _log(f"[{APP_NAME}] {name} 清除失敗（可能仍有行程佔用）：{e}")


def _app_mode_args(url: str, variant: int = 0) -> list[str]:
    """Edge --app 模式的完整旗標。抽成獨立函式，方便測試與日後調整。

    這裡每個旗標都對應一個「使用者看得到的問題」，不只是瘦身：
      --app=<url>            無網址列 / 書籤列（純應用視窗）
      --inprivate            不落瀏覽紀錄
      --user-data-dir=<專用> 與日常 Edge 完全隔離 → 不會撿到使用者的拡充功能
      --window-size          首開尺寸對齊 WIN_W × WIN_H
      --no-first-run / --no-default-browser-check
                             不跳「設為預設瀏覽器」「歡迎使用」
      --disable-features=…   關掉 Edge 自帶的側邊欄 / 帳號登入提示
      --hide-crash-restore-bubble
                             上次非正常關閉後不再彈「恢復頁面」氣泡
      （標題列固定是頁面 <title>「聲音辨識小工具」；Windows 11 上我們再把標題列
        配色改成與頁面一致的深/淺色，見 _set_titlebar。）
    """
    return [
        f"--app={url}", "--inprivate",
        f"--user-data-dir={_profile_dir(variant)}",
        f"--window-size={WIN_W},{WIN_H}",
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-background-networking", "--hide-crash-restore-bubble",
        "--disable-features=msImplicitSignin,msEdgeSyncEnabled,msEdgeWelcomePage,"
        "EdgeFollowEnabled,msEdgeSidebarV2,msReverseVideoTab,EdgeCollections",
    ]


def open_edge_app(url: str, variant: int = 0) -> subprocess.Popen | None:
    edge = _find_edge()
    if edge:
        # Wave 11：開窗前先清掉上次異常結束留下的 ProcessSingleton 鎖。
        # 這是冷啟動秒退（exit 0 / 0.0s）最常見的可重現原因；清不掉也沒關係，
        # _run_edge_session 會換備用 profile 再試。
        _clear_stale_singleton(_profile_dir(variant))
        args = [edge, *_app_mode_args(url, variant)]
        # CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        #   BREAKAWAY_FROM_JOB 讓 Edge 脫離 Python 的 Job Object —— 否則 Python
        #   退出時 Job handle 關閉，OS 會連帶殺掉 Edge，使用者看到的就是「視窗
        #   閃一下就沒了」（第一次啟動尤其常見，因為 Edge 冷啟動慢，使用者以為
        #   程式掛了就關掉，Python 隨後退出 → Edge 被殺）。
        #   脫離後 Python 可以獨立退出，Edge 繼續存活。
        flags = 0x08000000 | 0x01000000 if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(args, creationflags=flags)
            _log(f"[{APP_NAME}] Edge PID {proc.pid} 已啟動（脫離 Job Object，"
                 f"profile variant={variant}）")
            _decorate_window_async()   # Edge --app 視窗同樣套白底黑字標題列 + 圖示
            return proc
        except Exception:
            pass
    webbrowser.open(url)
    return None


# Wave 12／問題 4：判斷開窗成功不能只看「啟動行程是否還活著」。
# Edge --app 的啟動行程把 URL 交棒給瀏覽器主行程後，以 exit 0 在 0.0~0.3s
# 結束是**正常**的；舊邏輯把它當成「被轉發給殘留實例」而連開 3 次。
_EDGE_MAX_ATTEMPTS = 3
_EDGE_STARTUP_GRACE_SEC = 10.0    # 最長等前端連線
_EDGE_MIN_GRACE_SEC = 4.0         # 行程已結束時，至少再給頁面這麼久
_EDGE_PROBE_INTERVAL_SEC = 0.1
_EDGE_CLOSE_POLL_SEC = 0.3
_EDGE_CLOSE_IDLE_SEC = 6.0        # SSE 斷線後的寬限（EventSource 會自動重連）


def _wait_for_window_close(srv, idle_grace: float | None = None) -> None:
    """等到前端視窗真的關掉，才讓 main() 收網停 server。

    視窗開著時 /api/events 的 SSE 長連線一定還在；關掉後連線斷開，
    _Handler 的 finally 會 sse_leave()。另以 HWND 作快速輔助判定。
    兩者都消失且持續 idle_grace 秒才返回，避免 EventSource 重連的
    短暫空窗被誤判成關窗（那會提前關掉 server，視窗還在卻白屏）。
    """
    grace = _EDGE_CLOSE_IDLE_SEC if idle_grace is None else idle_grace
    idle = 0.0
    while True:
        alive = False
        try:
            alive = srv.window_alive()
        except Exception:
            pass
        if not alive:
            alive = bool(_find_app_hwnd())
        if alive:
            idle = 0.0
        else:
            idle += _EDGE_CLOSE_POLL_SEC
            if idle >= grace:
                return
        time.sleep(_EDGE_CLOSE_POLL_SEC)


def _run_edge_session(url: str, seen=None, wait_close=None,
                      grace: float | None = None) -> tuple[int | None, float, int]:
    """啟動 Edge 並等到視窗關閉；只有「完全沒開出頁面」才換 profile 重試。

    回傳 (最後一次退出碼, 觀察秒數, 總嘗試次數)。

    Wave 12／問題 4 的根因：舊程式用 ``proc.wait()`` 的 ``exit 0`` 加上
    「存活 < 2s」判定「秒退」，但 Edge --app 交棒後啟動行程本來就會秒退。
    結果一次啟動連開 3 個視窗、其中一個還因 profile 鎖競爭顯示「拒絕
    訪問」，launcher.log 卻記「連續 3 次秒退」——與使用者看到的完全相反。

    現在以「本機 server 收到前端請求 / 找到視窗 HWND」為成功訊號：
      • 看到訊號 → 開窗成功，不重試，改等視窗關閉；
      • 完全沒訊號且啟動行程已結束 → 才是真秒退，換備用 profile 重試。

    seen() / wait_close() 可注入，讓 Linux 沙箱測試不必真的開 Edge。
    """
    probe = seen or (lambda: bool(_find_app_hwnd()))
    max_wait = _EDGE_STARTUP_GRACE_SEC if grace is None else grace
    for attempt in range(_EDGE_MAX_ATTEMPTS):
        t_edge = time.perf_counter()
        proc = open_edge_app(url, variant=attempt)
        if proc is None:
            # 找不到 msedge.exe，已降級成系統瀏覽器；沒有行程可等，
            # 維持 HTTP server 存活（與舊行為一致）。
            _log(f"[{APP_NAME}] 找不到 msedge.exe，已用系統瀏覽器開啟，"
                 f"HTTP server 持續服務。")
            threading.Event().wait()
            return None, 0.0, 0
        ok = False
        while True:
            if probe():                      # 至少探測一次（grace=0 也要量）
                ok = True
                break
            elapsed = time.perf_counter() - t_edge
            if elapsed >= max_wait:
                break
            # 啟動行程已結束，且已給頁面足夠時間連上 → 不必耗滿 max_wait。
            if proc.poll() is not None and elapsed >= _EDGE_MIN_GRACE_SEC:
                break
            time.sleep(_EDGE_PROBE_INTERVAL_SEC)
        rc = proc.poll()
        alive = time.perf_counter() - t_edge
        if ok:
            _log(f"[{APP_NAME}] 頁面已連上本機服務（啟動行程 rc={rc}、"
                 f"{alive:.1f}s；交棒後結束屬正常）→ 開窗成功，不重試。")
            closer = wait_close or (lambda p=proc: p.wait())
            closer()
            _log(f"[{APP_NAME}] 視窗已關閉，準備結束 HTTP 服務。")
            return rc, alive, attempt + 1
        _log(f"[{APP_NAME}] 第 {attempt + 1}/{_EDGE_MAX_ATTEMPTS} 次："
             f"{alive:.1f}s 內未偵測到任何頁面連線（rc={rc}）")
        if attempt + 1 < _EDGE_MAX_ATTEMPTS:
            _log(f"[{APP_NAME}] ⚠ 判定為轉發給殘留實例／開窗失敗，"
                 f"換用備用 profile 自動重試…")
            time.sleep(0.35)
            continue
        _log(f"[{APP_NAME}] ⚠ Edge 連續 {_EDGE_MAX_ATTEMPTS} 次都沒開出頁面；"
             f"請把本份 launcher-*.log 回報給開發者。")
        return rc, alive, attempt + 1
    return None, 0.0, _EDGE_MAX_ATTEMPTS


def _cli_argv() -> list[str] | None:
    """有帶已知子命令（或 --help/--version）→ 回傳要交給 CLI 的 argv；否則 None。

    判斷刻意保守：只認 cli_mode.SUBCOMMANDS 裡的字，其餘一律當作「正常啟動
    GUI」。這樣就算使用者的捷徑或關聯開檔帶了奇怪參數，也不會意外變成無頭模式。
    """
    argv = sys.argv[1:]
    if not argv:
        return None
    from cli_mode import SUBCOMMANDS
    for a in argv:
        if a in SUBCOMMANDS:
            return argv
        if a in ("-h", "--help", "--version"):
            return argv
        if not a.startswith("-"):        # 第一個非旗標不是子命令 → 不是 CLI
            break
    return None


def _write_selftest_result(result: dict):
    """把 selftest 结果写成文件。

    为什么需要：--windowed 的 exe 没有控制台，stdout 被换成日志替身，外部
    脚本（tools/smoke_test.py、CI）只能「等日志里出现某一行」——既不可靠也
    容易误判。写一个结果文件，跑完就一定看得见。
    """
    d = _log_dir()
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "selftest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _selftest() -> int:
    """G6: cold-start benchmark for the onedir layout.

    Measures what can be measured without a desktop: server construction +
    bind, one HTTP round-trip to /health, and resource-root resolution. Window
    creation and Edge startup are intentionally excluded - those need a real
    Windows desktop. When the GUI dependency stack is unavailable (headless
    CI), the server part is reported as skipped instead of crashing, so this
    command stays usable as a smoke test everywhere.
    """
    import time as _t
    from version import __version__
    result = {
        "ok": True,
        "version": __version__,
        "layout": "onedir" if (app_paths.app_dir() / "_internal").is_dir() else "source",
        "frozen": app_paths.is_frozen(),
        "python": sys.version.split()[0],
        "server": "measured",
        "note": "Excludes OS process creation, window creation and Edge startup; "
                "measure those on the target Windows machine.",
    }
    t0 = _t.perf_counter()
    root = app_paths.resource_root()
    result["resourceResolveMs"] = round((_t.perf_counter() - t0) * 1000, 3)
    result["resourceRoot"] = str(root)

    if WebViewServer is None:
        result["server"] = "skipped"
        result["serverSkipReason"] = _SERVER_IMPORT_ERR or "GUI dependencies absent"
        # 打包版缺 GUI 依赖＝这个包是坏的（2026-11-11 的启动崩溃正是此形态）。
        # 源码环境下依赖是可选的，跳过就好；frozen 下必须报错，否则 CI 的
        # 冒烟测试会把「缺 ui_text.py 的包」判为通过。
        if app_paths.is_frozen():
            result["ok"] = False
        _log(json.dumps(result, ensure_ascii=False))
        _write_selftest_result(result)
        return 0 if result["ok"] else 1

    import urllib.request
    t1 = _t.perf_counter()
    srv = WebViewServer(host="127.0.0.1", port=0)
    t2 = _t.perf_counter()
    srv.start()
    t3 = _t.perf_counter()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/health",
                                   timeout=3) as resp:
            resp.read()
            result["healthRoundTripMs"] = round((_t.perf_counter() - t3) * 1000, 2)
    except Exception as exc:
        result["ok"] = False
        result["healthRoundTripMs"] = f"error: {type(exc).__name__}"
    result["constructMs"] = round((t2 - t1) * 1000, 2)
    result["serverStartMs"] = round((t3 - t2) * 1000, 2)
    srv.stop()
    _log(json.dumps(result, ensure_ascii=False))
    _write_selftest_result(result)
    return 0 if result["ok"] else 1


def main():
    # G6: headless cold-start benchmark; never opens a window or models.
    if "--selftest" in sys.argv[1:]:
        return _selftest()

    # ── 無頭 CLI 模式（給外部 Agent／腳本）──────────────────────────────
    #   帶子命令就完全不開視窗、不起 server，跑完即退並回傳退出碼。
    cli = _cli_argv()
    if cli is not None:
        setup_kill_on_close_job()        # 子程序（crispasr.exe 等）仍要連帶回收
        from cli_mode import main as cli_main
        sys.exit(cli_main(cli))

    # 先把自己綁進 Job Object：此後 start_load()/轉錄衍生的 crispasr.exe、
    # chatllm main.exe(FA) 等子程序都會自動納入同一 Job，關窗時連帶被殺。
    # handle 由 proc_guard 模組級變數持有（生命週期 == 行程），無須在此保管。
    setup_kill_on_close_job()

    # 設定獨立 AppUserModelID（要在開任何視窗前）→ 工作列圖示不跟 python.exe 共用。
    set_app_user_model_id()

    # H4: only one GUI instance may own the user-data directory. A second
    # launch hands the user to the existing window instead of racing on the
    # same settings / checkpoint / subtitle files.
    guard = SingleInstance()
    # 單實例鎖：取得失敗代表已有另一個實例 → 直接結束（不是崩潰，日誌要說清楚）
    clk = _StartupClock()
    owned, existing_url = guard.acquire()
    clk.mark("取得單實例鎖")
    if not owned:
        if existing_url:
            _log(f"[{APP_NAME}] 已有執行中的執行個體，導向現有視窗：{existing_url}")
            webbrowser.open(existing_url)
        else:
            _log(f"[{APP_NAME}] 已有執行中的執行個體（未取得視窗網址），本次啟動結束。")
        return 0

    # 匯入期兜底（見檔案上方 try/except）：缺模組時 WebViewServer 是 None，
    # 若直接往下走會在 WebViewServer(...) 炸成 TypeError 而看不到真正原因。
    # 這裡改成可讀的錯誤，並提示使用者去哪裡看日誌。
    if WebViewServer is None:
        raise RuntimeError(
            f"無法載入本程式核心模組（webview_server）：{_SERVER_IMPORT_ERR}\n"
            "安裝可能不完整，請重新解壓縮完整套件（勿只取出單一 exe）。"
        )

    srv = WebViewServer(host="127.0.0.1", port=0)   # 隨機空閒埠，只綁回環
    clk.mark("建立 WebViewServer")
    srv.start()
    guard.publish(srv.url)
    clk.mark("HTTP 服務開始監聽")
    # 視窗外觀：讀持久化外觀偏好決定初始標題列深淺；註冊回呼，讓 UI 切換主題時
    # 視窗標題列即時跟著深/淺。
    global _initial_theme
    try:
        _initial_theme = srv.backend.persisted_theme()
        srv.backend.set_theme_callback(apply_titlebar_theme)
    except Exception:
        pass
    # 自動載入策略：只有「目前選擇的模型已下載」時才自動載入（快速、無下載）。
    # 首次／模型未下載 → 不自動觸發下載，停在模型頁等使用者選好（可改 Whisper 等）
    # 並按「下載並載入模型」才開始下載。避免一開啟就硬抓預設模型。
    try:
        if srv.backend.selected_model_present():
            srv.backend.start_load()
        else:
            _log(f"[{APP_NAME}] 選擇的模型尚未下載 → 等使用者於模型頁確認後再下載。")
    except Exception:
        srv.backend.start_load()                     # 保底：判斷失敗仍嘗試載入
    clk.mark("模型載入已排入背景（不阻塞開窗）")
    url = srv.url
    _log(f"[{APP_NAME}] {url}")

    # Default: Edge --app. No .NET / pythonnet / WebView2 SDK is touched, so a
    # MOTW-marked DLL can never break startup. The native window is opt-in for
    # source installs only (QWENASR_NATIVE_WINDOW=1); release builds do not
    # bundle pywebview and will fall through cleanly if it is requested.
    native_requested = os.environ.get("QWENASR_NATIVE_WINDOW", "") == "1"
    native_ok = False
    if native_requested and webview2_available():
        native_ok = run_native_window(url)
        if not native_ok:
            _log(f"[{APP_NAME}] Native window unavailable, using Edge --app.")
    try:
        if not native_ok:
            # Wave 12：以「前端是否連上本機 server」判定開窗成功，
            # 不再拿啟動行程的 exit 0 / 存活秒數當成秒退證據。
            # 成功後還要等視窗真的關掉才收網，否則 server 會在視窗
            # 還開著時被 os._exit 關掉，頁面直接白屏。
            _run_edge_session(
                url,
                seen=lambda: srv.window_signal() or bool(_find_app_hwnd()),
                wait_close=lambda: _wait_for_window_close(srv),
            )
            clk.mark("Edge 視窗已關閉")
    except KeyboardInterrupt:
        pass
    finally:
        # 視窗已關 → 立即收網。先停 HTTP server（釋放埠），再硬退出行程。
        # 為什麼用 os._exit 而非正常 return：chatllm 走常駐 libchatllm.dll，
        # Vulkan context 刻意永不釋放（見 vulkan-dual-context-crash）；正常
        # 直譯器關閉會去清理這顆 DLL → 可能卡死成殭屍行程、持續佔顯存。
        # os._exit 跳過 atexit/GC/DLL 卸載，由 OS 直接回收行程與 GPU 資源；
        # 同一瞬間 Job handle 關閉 → 殘餘子程序一併被終止。
        try:
            guard.release()
            srv.stop()
        finally:
            # 收尾 flush 一律走「盡力而為」：windowed 版的 stdout 可能是替身或 None，
            # 一步失敗不能連帶讓 os._exit 被跳過 —— 那會留下殭屍行程與被佔用的埠。
            for _s in (sys.stdout, sys.stderr):
                try:
                    if _s is not None:
                        _s.flush()
                except Exception:
                    pass
            os._exit(0)


if __name__ == "__main__":
    main()
