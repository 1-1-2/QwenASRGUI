"""fa_aligner.py — chatllm 原生 ForcedAligner（後端無關，無需 PyTorch）

封裝「字級時間軸對齊」：以 subprocess 呼叫 chatllm `main.exe`，給定
「參考文字 + 音訊」即可輸出字級毫秒時間軸（format json）。

設計重點：
  • 對齊本身與 ASR 後端無關——OpenVINO（CPU）與 chatllm（Vulkan GPU）
    兩種引擎共用同一份邏輯，避免 _align_chunk 在兩處分歧。
  • CPU 用 `-ngl 0`，GPU 用 `-ngl {device_id}:all`。
  • 失敗、無輸出時一律回傳 []（呼叫端據此退回比例估算）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import app_paths
from typing import Callable

# chatllm 來源檔名沿用上游拼字（foced 非 forced），與 downloader 一致。
FA_BIN_NAME = "qwen3-focedaligner-0.6b.bin"

# Windows：隱藏子程序主控台視窗
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 子程序優先級。FA 與 ASR 併發時，把 FA 壓到低優先級讓系統自動調度，
# 比手工綁核更穩且更快（實測：綁核 FA 慢 19%，BELOW_NORMAL 只慢 6%）。
# 優先級不限制「用哪些核」，只影響搶 CPU 的順序——Windows 會把低優先級
# 線程排到空閒核上，效果等同於「自動找空閒核」，且換機器不用改配置。
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_IDLE_PRIORITY_CLASS = 0x00000040

# 可由呼叫端覆寫：0=正常，1=BELOW_NORMAL（默認），2=IDLE
FA_PRIORITY = 1


def _priority_flag() -> int:
    if sys.platform != "win32":
        return 0
    if FA_PRIORITY == 2:
        return _IDLE_PRIORITY_CLASS
    if FA_PRIORITY == 1:
        return _BELOW_NORMAL_PRIORITY_CLASS
    return 0


def _startup_info():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return si
    return None


def _base_dir() -> Path:
    """App 根目錄（凍結時為啟動器旁，否則為原始碼目錄）。見 app_paths.py。"""
    return app_paths.app_dir()


def find_chatllm_dir() -> Path | None:
    """定位含 main.exe 的 chatllm 目錄。

    與 app.py 的解析順序一致：<app>/chatllm → chatllmtest 備援。
    """
    base = _base_dir()
    for cand in (
        base / "chatllm",
        base / "chatllmtest" / "chatllm_win_x64" / "bin",
        base,
    ):
        if (cand / "main.exe").exists():
            return cand
    return None


def build_env() -> dict[str, str]:
    """回傳給 main.exe 的環境變數，保證它找得到 ffmpeg。

    為什麼需要：``main.exe`` 解碼音訊是**透過 PATH 呼叫 ffmpeg** 的子程序。
    系統 PATH 沒有 ffmpeg 時它不會報錯，而是印一行
    ``'ffmpeg' is not recognized``，然後**仍然輸出一份合法的 JSON**——
    固定 32 項、覆蓋 0~2.00s 的兜底對齊。呼叫端看到「有結果」就照單全收，
    字幕時間被壓扁到前兩秒，而且全程靜默。

    實測：45s chunk 在有/無 ffmpeg 下，覆蓋範圍分別是 13.12s 與 2.00s；
    端到端字幕空白率 96% vs 4%。也就是說「FFmpeg 沒裝」這個環境問題，
    表現出來是「對齊品質莫名變差」，極難歸因。

    因此把能找到的 ffmpeg 目錄**前置**到子程序的 PATH：系統 PATH 優先級
    維持不變（有的話照用），沒有時自動用 app 自帶的那份。找不到就原樣
    返回，讓上游照舊失敗——但至少不會因為「忘了加 PATH」而靜默降級。
    """
    env = os.environ.copy()
    try:
        from ffmpeg_utils import find_ffmpeg
        exe = find_ffmpeg()
    except Exception:
        exe = None
    if exe is None:
        return env

    ff_dir = str(Path(exe).parent)
    # Windows 的 PATH 鍵大小寫不固定（實測有 Path / PATH 兩種），逐一處理
    for key in ("PATH", "Path", "path"):
        if key in env:
            parts = [p for p in env[key].split(os.pathsep) if p]
            if ff_dir not in parts:
                env[key] = os.pathsep.join([ff_dir, *parts])
            break
    else:
        env["PATH"] = ff_dir

    # 讓下游也能直接取用（部分 ffmpeg 封裝會讀這個）
    env.setdefault("FFMPEG_PATH", str(exe))
    return env


class ChatLLMAligner:
    """chatllm 原生 ForcedAligner 的輕量包裝。

    用法
    ----
    fa = ChatLLMAligner(fa_dir=model_dir)        # CPU（-ngl 0）
    if fa.load(cb=status):                        # 驗證 .bin 可用
        items = fa.align_chunk(wav, ref_text)     # [(word, start_s, end_s), ...]
    """

    FA_BIN_NAME = FA_BIN_NAME

    def __init__(
        self,
        fa_dir: str | Path | None,
        chatllm_dir: str | Path | None = None,
        n_gpu_layers: int = 0,
        device_id: int = 0,
    ):
        self._fa_dir = Path(fa_dir) if fa_dir else None
        self._chatllm_dir = (
            Path(chatllm_dir) if chatllm_dir else find_chatllm_dir()
        )
        self._n_gpu_layers = n_gpu_layers
        self._device_id = device_id
        self._fa_bin: Path | None = None
        self.ready = False

    # ── 偵測 / 驗證 ────────────────────────────────────────────────────
    def fa_bin_path(self) -> Path | None:
        """FA .bin 應在的位置（fa_dir / FA_BIN_NAME）。"""
        if self._fa_dir is None:
            return None
        return self._fa_dir / self.FA_BIN_NAME

    def load(self, cb: Callable[[str], None] | None = None) -> bool:
        """驗證 chatllm FA .bin 可用。成功回傳 True 並設定 ready/_fa_bin。

        實際對齊在 align_chunk() 以 subprocess 完成；此處僅以 `--show`
        確認模型確為 ForcedAligner，避免日後拿到錯誤模型。
        """
        def _s(msg: str):
            if cb:
                cb(msg)

        self.ready = False
        self._fa_bin = None

        fa_path = self.fa_bin_path()
        if fa_path is None or not fa_path.exists():
            return False
        if self._chatllm_dir is None:
            _s("⚠ 找不到 chatllm（main.exe），時間軸對齊改用比例估算")
            return False

        try:
            _s("驗證時間軸對齊模型（chatllm ForcedAligner）…")
            exe = self._chatllm_dir / "main.exe"
            r = subprocess.run(
                [str(exe), "-m", str(fa_path), "-ngl", "0",
                 "--hide_banner", "--show"],
                capture_output=True, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                timeout=30, cwd=str(self._chatllm_dir),
                env=build_env(),
                creationflags=_CREATE_NO_WINDOW | _priority_flag(),
                startupinfo=_startup_info(),
            )
            out = r.stdout + r.stderr
            if "ForcedAligner" not in out:
                _s("⚠ FA 模型驗證失敗，改用比例估算")
                return False
            self._fa_bin = fa_path
            self.ready = True
            _s("時間軸對齊模型就緒（chatllm，無需 PyTorch）")
            return True
        except Exception as _e:
            _s(f"⚠ ForcedAligner 載入失敗（{_e}），改用比例估算")
            return False

    # ── 對齊 ──────────────────────────────────────────────────────────
    def align_chunk(
        self,
        wav_path: str,
        ref_text: str,
        language: str = "Chinese",
    ) -> list[tuple[str, float, float]]:
        """對齊「參考文字 + 音訊」，回傳字級 [(word, start_s, end_s), ...]。

        失敗或無輸出時回傳 []。
        """
        if not self._fa_bin or self._chatllm_dir is None:
            return []

        gpu_args = (["-ngl", f"{self._device_id}:all"]
                    if self._n_gpu_layers > 0 else ["-ngl", "0"])
        exe = self._chatllm_dir / "main.exe"
        prompt = f"{ref_text}{{{{audio:{wav_path}}}}}"
        cmd = [
            str(exe), "-m", str(self._fa_bin), *gpu_args, "--hide_banner",
            "--multimedia_file_tags", "{{", "}}",
            "-p", prompt,
            "--set", "format", "json",
            "--set", "language", language,
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                timeout=45, cwd=str(self._chatllm_dir),
                env=build_env(),
                creationflags=_CREATE_NO_WINDOW | _priority_flag(),
                startupinfo=_startup_info(),
            )
        except Exception:
            return []

        out = r.stdout + r.stderr
        # main.exe 末尾會附 timings 文字，需先切出 JSON 陣列 [...]
        i = out.find("[")
        j = out.rfind("]")
        if i < 0 or j <= i:
            return []
        try:
            data = json.loads(out[i:j + 1])
        except (ValueError, json.JSONDecodeError):
            return []

        items: list[tuple[str, float, float]] = []
        for d in data:
            try:
                w = str(d["text"])
                s = float(d["start"]) / 1000.0   # 毫秒 → 秒
                e = float(d["end"]) / 1000.0
            except (KeyError, TypeError, ValueError):
                continue
            if w.strip():
                items.append((w, s, e))
        return items
