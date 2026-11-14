"""sysmem.py — 系統可用實體記憶體查詢（純標準庫，不依賴 psutil）。

為什麼需要：字級對齊（chatllm ForcedAligner）每個進程峰值工作集約 2.9 GB。
要不要開兩路、能不能與識別並行，只能靠「當下還有多少可用實體記憶體」判斷，
不能寫死一個數字——使用者的機器可能在跑瀏覽器、剪輯軟體。

Windows 用 GlobalMemoryStatusEx（ullAvailPhys 是「可用」而非「空閒」，
已扣除待回收的快取，正是我們要的口徑）；其他平台退化為 None，
呼叫端一律把 None 當成「不知道 → 走保守值」。
"""
from __future__ import annotations

import sys

# 一個 FA 進程的峰值工作集（GB），2026-11-12 真實音檔實測 2.90 GB。
FA_PROCESS_GB = 2.9
# 留給其他程式與系統快取的餘裕（GB）。低於這個值就開始換頁，速度斷崖。
SAFETY_GB = 1.5
# 與識別並行時的額外餘裕：ASR 常駐 + 解碼緩衝會吃掉一部分。
ASR_HEADROOM_GB = 2.6


def available_gb() -> float | None:
    """目前可用的實體記憶體（GB）。取不到回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return round(st.ullAvailPhys / (1024 ** 3), 2)
    except Exception:
        return None


def align_lanes(want: int = 2) -> tuple[int, float | None]:
    """補對齊可用幾路：1~2 路，按可用記憶體決定。

    每多一路需要一個 FA 進程（2.9 GB）+ 共用餘裕。取不到記憶體資訊時
    只給 1 路——寧可慢，不要在別人的機器上交換檔案。
    """
    avail = available_gb()
    if avail is None:
        return 1, None
    per_lane = FA_PROCESS_GB + 0.3
    fit = int((avail - SAFETY_GB) // per_lane)
    lanes = max(1, min(int(want), fit))
    return lanes, avail


def can_run_parallel_with_asr() -> tuple[bool, float | None]:
    """識別時能不能再起一路 FA。

    門檻 = FA 峰值 + ASR 餘裕；不夠就降級串行。
    """
    avail = available_gb()
    if avail is None:
        return False, None
    need = FA_PROCESS_GB + ASR_HEADROOM_GB
    return avail >= need, avail
