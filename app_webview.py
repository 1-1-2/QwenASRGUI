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
class _NullSafeStream:
    """包住真實串流；真實串流為 None 或已關閉時靜默丟棄（可選寫入日誌檔）。"""

    def __init__(self, real, log_path: Path | None = None):
        self._real = real
        self._log = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                # 啟動器日誌只保留單檔並上限約 1 MB：塞住磁碟比沒有日誌更糟
                if log_path.exists() and log_path.stat().st_size > 1_048_576:
                    log_path.unlink()
                self._log = open(log_path, "a", encoding="utf-8", errors="replace")
            except Exception:
                self._log = None

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
    log_path = None
    d = _log_dir()
    if d is not None:
        try:
            log_path = d / "launcher.log"
        except Exception:
            log_path = None
    if sys.stdout is None:
        sys.stdout = _NullSafeStream(None, log_path)
    if sys.stderr is None:
        # stderr 與 stdout 共用同一個日誌檔（append 模式，兩個 handle 各自寫入無妨）
        sys.stderr = _NullSafeStream(None, log_path)


hush_stdio()


def _install_crash_log():
    """最後兜底：未捕捉例外寫進 crash.log，並提醒使用者看哪個檔案。

    windowed EXE 沒有主控台，未捕捉例外原本**完全無聲**（使用者只看到視窗沒開）。
    有了這支 handler，任何啟動期錯誤都會留下可回報的檔案；stderr 已被前一步
    換成日誌替身，所以 traceback 就能落地。
    """
    import traceback

    def _hook(exc_type, exc, tb):
        try:
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
                    f"詳細記錄：{where}",
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
        print(f"[{APP_NAME}] 原生視窗失敗，改用 Edge：{e}")
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


def _profile_dir() -> str:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "edge-profile"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def _app_mode_args(url: str) -> list[str]:
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
        f"--user-data-dir={_profile_dir()}",
        f"--window-size={WIN_W},{WIN_H}",
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-background-networking", "--hide-crash-restore-bubble",
        "--disable-features=msImplicitSignin,msEdgeSyncEnabled,msEdgeWelcomePage,"
        "EdgeFollowEnabled,msEdgeSidebarV2,msReverseVideoTab,EdgeCollections",
    ]


def open_edge_app(url: str) -> subprocess.Popen | None:
    edge = _find_edge()
    if edge:
        args = [edge, *_app_mode_args(url)]
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(args, creationflags=flags)
            _decorate_window_async()   # Edge --app 視窗同樣套白底黑字標題列 + 圖示
            return proc
        except Exception:
            pass
    webbrowser.open(url)
    return None


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
        print(json.dumps(result, ensure_ascii=False))
        return 0

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
    print(json.dumps(result, ensure_ascii=False))
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
    owned, existing_url = guard.acquire()
    if not owned:
        if existing_url:
            print(f"[{APP_NAME}] 已有執行中的執行個體，導向現有視窗：{existing_url}")
            webbrowser.open(existing_url)
        else:
            print(f"[{APP_NAME}] 已有執行中的執行個體（未取得視窗網址），本次啟動結束。")
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
    srv.start()
    guard.publish(srv.url)
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
            print(f"[{APP_NAME}] 選擇的模型尚未下載 → 等使用者於模型頁確認後再下載。")
    except Exception:
        srv.backend.start_load()                     # 保底：判斷失敗仍嘗試載入
    url = srv.url
    print(f"[{APP_NAME}] {url}")

    # Default: Edge --app. No .NET / pythonnet / WebView2 SDK is touched, so a
    # MOTW-marked DLL can never break startup. The native window is opt-in for
    # source installs only (QWENASR_NATIVE_WINDOW=1); release builds do not
    # bundle pywebview and will fall through cleanly if it is requested.
    native_requested = os.environ.get("QWENASR_NATIVE_WINDOW", "") == "1"
    native_ok = False
    if native_requested and webview2_available():
        native_ok = run_native_window(url)
        if not native_ok:
            print(f"[{APP_NAME}] Native window unavailable, using Edge --app.")
    try:
        if not native_ok:
            proc = open_edge_app(url)
            if proc is not None:
                proc.wait()
            else:
                threading.Event().wait()
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
