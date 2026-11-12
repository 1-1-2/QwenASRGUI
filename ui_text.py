"""H6/H7: structured, localisable UI text emitted by the backend.

Why this module exists
----------------------
The first i18n pass translated backend strings in the browser with a
per-character Traditional->Simplified table plus an English phrase dictionary.
That is wrong in two ways:

* Chinese variants differ in *wording* ("記憶體" / "内存", "預設" / "默认",
  "透過" / "通过"), not only in glyphs; a character table produces text that
  no Simplified-Chinese speaker would write.
* English fell back to the Chinese original for every string that was absent
  from the phrase dictionary, which is most long or parameterised sentences.

The backend now sends ``{"k": <key>, "v": {...}, "t": <Hant fallback>}`` for
every enumerable user-visible string. ``webview/js/i18n.js`` owns a hand
written three-locale dictionary keyed by ``k`` and renders ``v`` into it.
Free-form diagnostics (exception text, paths, user prompts, run logs) stay
plain strings and use the browser fallback path - they are deliberately not
translated, because they cannot be enumerated.

Contract
--------
* Keys are stable identifiers. Never reuse a key for different semantics.
* ``{name}`` placeholders must match exactly between ``TEMPLATES`` here and
  the ``M`` dictionary in ``webview/js/i18n.js``. ``tests/test_i18n_contract.py``
  enforces this, so a missing translation fails CI instead of leaking Chinese
  into the English UI.
* ``t`` is always the verbatim Hant rendering. It keeps the run log readable,
  keeps old API consumers working, and is the fallback when I18N is unavailable.
* ``plain()`` must be used anywhere the backend needs an actual ``str``
  (log entries, string concatenation, exception messages).

The source language of templates is Traditional Chinese (the project's
upstream language). Simplified and English text lives only in the frontend.
"""
from __future__ import annotations

from typing import Any

# key -> Traditional Chinese template. {name} placeholders are rendered by
# both render() (fallback) and the frontend M dictionary.
TEMPLATES: dict[str, str] = {
    # ── 基礎模式用途卡 ────────────────────────────────────────────────
    "profile.zh.title": "中文（標準）",
    "profile.zh.desc": "一般用途首選：中文為主、斷句與標點最完整，長影片／會議／訪談都適用。",
    "profile.tea.title": "繁體中文 TEA（乾淨聲音）",
    "profile.tea.desc": "台灣國語特化：原生輸出繁體與台灣用語，中英夾雜也保得住。限乾淨人聲（訪談、質詢、講座）。",
    "profile.ja.title": "日本語",
    "profile.ja.desc": "日語／動漫特化：日文歌詞與台詞辨識明顯較佳，且輸出保留日文原生漢字（不會被誤轉成繁體字形）。",
    "profile.twmix.title": "國台語混合（高速）",
    "profile.twmix.desc": "Whisper Breeze-ASR-26：繁中／台語特化且速度極快，適合大量批次。模型不輸出標點，字幕改以長度切行（斷句較平）。",
    "profile.note.tea": "⚠️ TEA-ASR 為「台灣國語日常人聲」特化模型：乾淨人聲最準且原生輸出繁體，但背景配樂較重的段落或歌曲可能整段無輸出。音樂類素材請改用 Qwen3-ASR-1.7B（通用）或 Whisper Breeze。",
    "profile.note.cpuSlow": "此用途沒有純 CPU 的專用模型，將以 CrispASR 在處理器上執行，速度會明顯較慢；若可接受請直接下載，或改選「中文（標準）」。",
    "profile.note.chatllm": "⚠️ chatllm 核心在部分 AMD 內顯／APU 有已知相容問題，且核心二進位未隨安裝包提供（保留供既有使用者向下相容）；若遇當機或無輸出，建議改用「Qwen · CRISPASR/Vulkan」核心。",

    # ── 啟動自檢：通用狀態 ────────────────────────────────────────────
    "state.downloaded": "已下載",
    "state.notDownloaded": "未下載（啟用時自動下載）",
    "state.builtin": "已內建",
    "state.vadMissing": "缺 VAD onnx",
    "state.ready": "已就緒",
    "state.notProvided": "未提供（核心未隨安裝包附帶，僅供既有使用者）",
    "state.notDownloadedDiar": "未下載（約 32MB，啟用分離時下載）",
    "state.notDownloadedFa": "未下載（約 939MB，啟用對齊時下載）",

    # ── 啟動自檢：分組與項目 ──────────────────────────────────────────
    "health.group.ov": "Qwen · OpenVINO（CPU）",
    "health.group.crisp": "CRISPASR（{variant}：Whisper/Breeze + Qwen + TEA）",
    "health.group.chatllm": "Qwen · chatllm（Vulkan · 相容）",
    "health.ov.model": "ASR 模型（{size}）",
    "health.ov.vad": "語音分段 VAD（silero）",
    "health.ov.fa": "時間軸對齊 FA（ForcedAligner .bin）",
    "health.ov.diar": "說話者分離（外部 ONNX）",
    "health.crisp.core": "CrispASR {version} 核心（{variant}）",
    "health.crisp.coreMissing": "未下載或版本不符（約 {size}MB，啟用時下載）",
    "health.crisp.coreNotNeeded": "目前使用 OpenVINO（CPU），此核心不需要；改用 GPU 核心時會自動下載",
    "health.crisp.coreVariantMismatch": "版本相符但加速版本不同，啟用時會自動換成目前選擇的版本",
    "health.crisp.breeze": "Whisper/Breeze 模型（{quant}）",
    "health.crisp.qwen": "Qwen3-ASR-1.7B 模型（{quant}）",
    "health.crisp.qwenJa": "Qwen3-ASR-1.7B 日語動漫模型（{quant}）",
    "health.crisp.qwenJaMissing": "未下載（日語增強，啟用時下載）",
    "health.crisp.tea": "TEA-ASR-1.1 台灣國語模型（Q8）",
    "health.crisp.teaMissing": "未下載（繁中／台灣用語增強，啟用時下載）",
    "health.crisp.fa": "時間軸對齊 FA（aligner gguf {quant}）",
    "health.crisp.faMissing": "未下載（約 643MB，啟用時下載）",
    "health.crisp.diar": "說話者分離（外部 ONNX，與 OpenVINO 共用）",
    "health.shared.ffmpeg": "FFmpeg（影片抽音軌用）",
    "health.shared.ffmpegOk": "已偵測：{path}",
    "health.shared.ffmpegMissing": "未下載（轉錄影片時自動下載解壓）",
    "health.shared.diar": "說話者分離模型（OpenVINO/CRISPASR 共用）",
    "health.shared.diarMissing": "未下載（啟用分離時自動下載）",
    "health.chatllm.core": "chatllm 核心（libchatllm.dll）",
    "health.chatllm.model": "Qwen3-ASR 模型（.bin）",
    "health.chatllm.modelMissing": "未下載（約 2.3GB，啟用時自動下載）",
    "health.chatllm.fa": "時間軸對齊 FA（ForcedAligner .bin）",

    # ── 裝置與 GPU 診斷 ───────────────────────────────────────────────
    "device.inUse": "使用中",
    "device.vram": "{gb} GB 可用",
    "device.diag.noDetector": "GPU 偵測需要 CrispASR 核心；啟用 GPU 核心時會自動下載，之後即可在此列出可用的獨立顯示卡。目前僅 CPU 推理可用。",
    "device.diag.probeFailed": "GPU 偵測未完成（{source}）：{error}　已自動改用 CPU 推理。",
    "device.diag.noGpu": "未偵測到可用的獨立 GPU，僅 CPU 推理可用。",
    "device.diag.exception": "GPU 偵測例外：{error}",

    # ── 選擇核心／加速版本／模型的回饋訊息 ────────────────────────────
    "msg.accel.unknown": "未知的加速版本：{variant}",
    "msg.accel.ready": "已選用「{label}」（核心已就緒）。",
    "msg.accel.remembered": "已記住「{label}」。下次載入模型時會下載該版本核心（約 {size} MB），請重新啟動程式以套用。",
    "msg.backend.same": "「{label}」已是目前使用的核心。",
    "msg.backend.restart": "已記住「{label}」。請重新啟動程式以套用新核心。",
    "msg.backend.firstRun": "已記住「{label}」。請重新啟動程式以套用 —— 首次啟用該核心會在啟動時自動下載對應模型。",
    "msg.model.selectedCanLoad": "已選定「{core} · {model}」（{arch}）。點「下載並載入模型」即可開始（不需重啟）。",
    "msg.model.selectedRestart": "已記住「{core} · {model}」（{arch}）。切換核心需重新啟動程式以套用。",
    "msg.model.same": "「{core} · {model}」已是目前使用的模型。",
    "msg.model.withNote": "{message}\n{note}",
    "msg.lang.auto": "自動偵測",

    # ── 狀態列 ────────────────────────────────────────────────────────
    "status.appName": "聲音辨識小工具",
    "status.backendCpu": "CPU · OpenVINO INT8",

    # ── 進度／下載 ────────────────────────────────────────────────────
    "progress.download17b": "下載 1.7B 模型（約 4.3 GB）…",
    "progress.downloadChatllm": "下載 chatllm 模型（~2.3 GB）…",
    "progress.downloadCrisp": "下載 CrispASR 核心…",
    "progress.downloadGguf": "下載 {name}{note} {quant} GGUF…",
    "progress.downloadBreeze": "下載 Breeze-ASR-26 {quant} 模型…",
    "progress.downloadAligner": "下載時間軸對齊器…",
    "progress.downloadFfmpeg": "下載 ffmpeg（影片音軌提取）…",
    "progress.downloadDiar": "下載說話者分離模型（約 32 MB）…",
    "progress.downloadFa": "下載時間軸對齊模型（約 939 MB）…",
    "progress.downloadSource": "下載來源切換：{name}（{code}）{hint}",
    "progress.retry": "重試 {attempt}：{name} {hint}",
    "progress.manifest": "已輸出下載清單：{path}",
    "progress.importLocal": "匯入本地模型：{bundle} → {message}",
    "progress.gpuFallback": "{backend} 載入失敗，改用 CPU 核心…",
    "progress.enrichSkipped": "{label}模型未安裝，本次跳過，字幕不受影響",
    "progress.trialPrefix": "試跑前 {mins} 分鐘 · ",
    "progress.runInfo": "{trial}音檔 {mins} 分鐘 · 分段增量落盤 · 生效切分 {chunk} 秒 · 預估常駐約 {ram} GB",
    "progress.tunnelFailed": "建立失敗：{error}",
    "progress.tunnelStarting": "建立中…",
    "progress.accelProbeFailed": "硬體偵測失敗：{error}",
    "warn.sslInsecure": "SSL 憑證驗證已降級（{reason}）：下載的模型檔無法驗證來源，請在可信網路環境中使用。",

    # ── 硬體分級摘要（基礎模式頁首）───────────────────────────────────
    "hw.noGpu": "未偵測到顯示卡 —— 以純處理器（CPU）推理為準給建議。",
    "hw.probeFailed": "無法偵測顯示卡，以一般配備為準給建議。",
    "hw.discrete": "偵測到獨立顯示卡：{names} —— 可用最準的模型。",
    "hw.integrated": "偵測到內建顯示晶片：{names} —— 建議用較輕量的模型。",

    # ── 推理加速版本：選項卡與推薦理由（資料源自 downloader.py）────────
    "accel.reason.noGpu": "未偵測到顯示卡資訊，先以 Vulkan 版為預設——它在沒有獨立顯卡時會自動退回 CPU 推理，最不會出事。",
    "accel.reason.nvidia": "偵測到 NVIDIA 顯示卡（{names}）。預設仍用 Vulkan（僅 34 MB、實測速度接近）；若要壓榨 Qwen3-ASR 的極速，可改選 {cuda}。",
    "accel.reason.amdIntel": "偵測到 {vendor} 顯示卡（{names}）。CrispASR 的 Windows 版沒有 ROCm／SYCL，Vulkan 就是這張卡的最佳解。",
    "accel.reason.noAccel": "未偵測到可加速的顯示卡，使用 Vulkan 版（會自動退回 CPU 推理）。",
    "accel.note.needNvidia": "需要 NVIDIA 顯示卡",
    "accel.note.driverTooOld": "需要 {min} 以上的驅動（目前 {cur}）",
    "accel.note.otherCuda": "驅動版本建議改用另一個 CUDA 版本",
    "accel.note.legacyOnly": "只有在一般 CPU 版閃退時才需要",
    "accel.opt.vulkan.label": "Vulkan（通用 GPU）",
    "accel.opt.vulkan.desc": "NVIDIA / AMD / Intel 通吃，體積最小。已長期實測穩定，無獨立顯卡時亦可走內顯。",
    "accel.opt.cuda12.label": "CUDA 12（NVIDIA 專用）",
    "accel.opt.cuda12.desc": "NVIDIA 專用，自帶 CUDA 12 runtime。大型語音 LLM 後端（Qwen3-ASR）通常比 Vulkan 快，但下載量大得多。",
    "accel.opt.cuda13.label": "CUDA 13（NVIDIA 新驅動）",
    "accel.opt.cuda13.desc": "同 CUDA 12 但用較新的 runtime，體積小一些；需要 580 以上的 NVIDIA 驅動。",
    "accel.opt.cpu.label": "CPU（純處理器）",
    "accel.opt.cpu.desc": "完全不碰顯卡。速度最慢，但在顯卡驅動有問題時最不會出事。",
    "accel.opt.cpu-legacy.label": "CPU 相容版（無 AVX2 的老 CPU）",
    "accel.opt.cpu-legacy.desc": "給 2013 年前、不支援 AVX2 的老處理器；一般 CPU 版開不起來（閃退／非法指令）時才選這個。",

    # 下載中心：「執行期就緒」註記。這些字串原本只存在於前端 i18n.js，
    # 後端 render() 找不到模板就退化成 key 原文（t 欄位會顯示 "dl.runtimeCrisp"），
    # 只有在 I18N 不可用時才會露出來，所以一直沒被發現。此處補齊，兩邊一致。
    "dl.runtimeCrisp": "核心 v{version} 已安裝（只需其中一個加速版本）",
    "dl.runtimeFfmpegOk": "ffmpeg 就緒：{path}",
    "dl.runtimeFfmpegMissing": "找不到 ffmpeg，需要時會自動下載",
    "dl.runtimeVadOk": "VAD 已就緒（隨套件附帶，或已在模型目錄）",
    "dl.runtimeVadMissing": "找不到 silero VAD 模型，需重新安裝或手動放入 ov_models",
}


def render(key: str, variables: dict[str, Any] | None = None) -> str:
    """Render ``key`` in Traditional Chinese (the Hant fallback / log text)."""
    template = TEMPLATES.get(key)
    if template is None:                       # pragma: no cover - guarded by tests
        return key
    out = template
    for name, value in (variables or {}).items():
        out = out.replace("{" + name + "}", plain(value))
    return out


def msg(key: str, **variables: Any) -> dict:
    """Build a localisable message payload.

    ``variables`` may themselves be messages; ``plain()`` resolves them
    recursively so nested ``msg.model.withNote`` renders correctly in the
    fallback text and the run log.
    """
    return {"k": key, "v": variables, "t": render(key, variables)}


def join(*parts: Any, sep: str = "　") -> dict:
    """Concatenate several messages (e.g. a model note + a CPU warning)."""
    return {"parts": list(parts), "sep": sep,
            "t": sep.join(plain(p) for p in parts if plain(p))}


def plain(value: Any) -> str:
    """Resolve any message (structured or plain) to a readable Hant string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "t" in value:
            return str(value["t"])
        if "parts" in value:
            return str(value.get("sep") or "").join(
                plain(p) for p in value.get("parts", []) if plain(p))
        return str(value.get("k", ""))
    return str(value)
