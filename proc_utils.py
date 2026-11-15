"""proc_utils.py — 統一「不彈黑窗」的子程序啟動參數。

為什麼需要單獨一個模組
----------------------
Windows 上只要在 GUI 進程裡 spawn 一個 console 子程序（ffmpeg、main.exe、
crisp_engine…），系統就會分配一個控制台窗口，使用者看到的是黑窗一閃一閃。
`CREATE_NO_WINDOW` 阻止分配，`STARTUPINFO + SW_HIDE` 再兜一層 —— 兩個都要。

漏掉的代價不小：對齊管線每解一個窗口就起一次 ffmpeg，收尾 drain 時連續
跑十幾個窗口，畫面上就是「黑色命令行視窗反覆彈出閃爍」。這不是崩潰，
但看起來像程式失控。

專案裡原本每個模組各寫一份（`audio_io`、`chatllm_engine`、`crisp_engine`、
`fa_aligner` 都有），新程式碼隨手 `subprocess.run(...)` 就漏了 —— 
`webview_backend._decode_window` 與 `align_backfill._decode` 就是這樣漏的。
集中到這裡之後，新調用點只要用 `run_hidden()` 就不會再漏。

用法
----
    from proc_utils import run_hidden, hidden_kwargs

    run_hidden(cmd, capture_output=True, timeout=180)

    subprocess.Popen(cmd, **hidden_kwargs(), stdout=subprocess.PIPE)
"""
from __future__ import annotations

import subprocess
import sys

# CREATE_NO_WINDOW：不為子程序分配控制台（0x08000000，僅 Windows 有效）
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_STARTUP_INFO: "subprocess.STARTUPINFO | None" = None
if sys.platform == "win32":
    _STARTUP_INFO = subprocess.STARTUPINFO()
    _STARTUP_INFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUP_INFO.wShowWindow = 0          # SW_HIDE


def hidden_kwargs() -> dict:
    """回傳讓子程序不彈窗的 kwargs；非 Windows 回空 dict。

    只包含 `creationflags` / `startupinfo`，可安全 merge 進 Popen / run。
    """
    if sys.platform != "win32":
        return {}
    kw: dict = {"creationflags": CREATE_NO_WINDOW}
    if _STARTUP_INFO is not None:
        kw["startupinfo"] = _STARTUP_INFO
    return kw


def run_hidden(cmd, **kwargs):
    """`subprocess.run` 的包裝：自動加上不彈窗參數。

    呼叫方傳的 `creationflags` 會被 OR 上 CREATE_NO_WINDOW（例如要疊加
    優先級旗標），`startupinfo` 則只在沒傳時補 —— 兩者都不覆蓋呼叫方的意圖。
    """
    if sys.platform == "win32":
        kwargs["creationflags"] = (int(kwargs.get("creationflags") or 0)
                                   | CREATE_NO_WINDOW)
        if "startupinfo" not in kwargs and _STARTUP_INFO is not None:
            kwargs["startupinfo"] = _STARTUP_INFO
    return subprocess.run(cmd, **kwargs)
