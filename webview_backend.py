"""webview_backend.py — WebView 共用後端邏輯（與視窗/傳輸無關）

把「狀態 / 設定 / 裝置 / 端點 / 轉錄」等業務邏輯集中於此，重用既有
ASREngine。不依賴 pywebview，也不依賴 HTTP —— 可被 webview_server
（桌面）直接呼叫，亦可被測試直接 import 驗證。

設計原則：
  • 不碰 GUI、不碰 socket；純函式式後端。
  • transcribe() 以 progress_cb(pct:int, status:str) 回報進度，由上層
    （HTTP 層）轉成 SSE 或其他通道推給前端。
  • 模型較重 → load() 設計成可在背景執行緒呼叫。
"""
from __future__ import annotations

import gc
import hashlib
import json
import threading
import time
import traceback
from pathlib import Path

import app as core          # 重用 ASREngine / 常數 / 診斷函式（__main__ guard 保護）
import app_paths
from run_log import RunLog
import ui_text as _t

# Application directory for user data (settings / models / subtitles / checkpoints).
# Deliberately not Path(__file__).parent: when frozen that points into the
# bundled resource root (_internal/ in onedir), not beside the launcher.
BASE_DIR = app_paths.app_dir()


def _vad_present(model_dir: Path, vad_sess=None) -> bool:
    """VAD 是否可用 —— 下載中心與系統自檢**唯一**的判斷來源。

    歷史上這兩邊各寫一套：自檢會依序找 [使用者模型目錄, 安裝目錄 ov_models,
    resource_path]，下載中心卻只認 models.json 的 target_dir（等於使用者模型
    目錄）。於是使用者一旦把 model_dir 指到別處（例如 D:\\models），VAD 明明隨
    套件附在安裝目錄裡、自檢顯示綠燈，下載中心卻說「未下載」—— 兩份 UI 互相
    矛盾。現在兩邊都呼叫這裡，結論不可能再分岔。

    查找順序刻意把「執行期實際會載入的那份」放前面：vad_sess 已經載入就直接算
    就緒；否則使用者模型目錄優先（可自行替換），最後才是隨套件附的資源根
    （onedir 是 _internal/，onefile 是解包臨時目錄，resource_path() 兩者都認）。
    """
    try:
        if vad_sess is not None:
            return True
        cands = [Path(model_dir) / "silero_vad_v4.onnx",
                 BASE_DIR / "ov_models" / "silero_vad_v4.onnx"]
        if hasattr(core, "resource_path"):
            cands.append(Path(core.resource_path("ov_models", "silero_vad_v4.onnx")))
        return any(p.exists() for p in cands)
    except Exception:
        return False

# ── 模型目錄：核心 → 模型 → (backend, settings patch)。──────────────────
#   webview 自有目錄（不直接用 app._resolve_backend），因為它多了一個
#   app.py 尚無的「Qwen on CRISPASR」項（同顆 crispasr.exe 跑 Qwen3-ASR GGUF，
#   見 crispasr-nemotron-eval）。settings 鍵與 app.py 相容：
#     openvino → cpu_model_size("0.6B"/"1.7B")
#     chatllm  → 無額外鍵（單一 .bin）
#     crispasr → crisp_model("breeze"/"qwen3") + crisp_quant(breeze 才用)
#   每個 entry：(核心標籤, 模型標籤, backend, settings_patch)
# chatllm（Vulkan）核心：因 AMD 內顯已知問題 + 打包時二進位常被防毒隔離，
# 核心二進位「不放進安裝包」（build_webview.bat 不再複製 chatllm/）。但引擎程式碼
# (_load_chatllm/chatllm_engine)保留，且為「向下相容」——曾使用 chatllm 的舊用戶
# （或自 CTk 桌面版升級、機器上已備有 chatllm/ 的人）仍能選用並確認其載入狀況：
#   • 模型清單：chatllm 項目只在「機器上實際有 chatllm/libchatllm.dll」或「目前
#     已記住 backend=chatllm」時才顯示（_chatllm_available）→ 新安裝的乾淨機器看不到，
#     不污染主推 CASR-Qwen 的 UI；舊用戶仍可見、可切。
#   • 系統自檢：同條件下 health_check 會列出 chatllm 核心（DLL／模型／FA）就緒狀況。
_CHATLLM_LABEL = "Qwen3-ASR-1.7B Q8（Vulkan · 相容）"
_CHATLLM_AMD_NOTE = _t.msg("profile.note.chatllm")
_MODEL_CATALOG = [
    ("Qwen", "Qwen3-ASR-0.6B",              "openvino", {"cpu_model_size": "0.6B"}),
    ("Qwen", "Qwen3-ASR-1.7B INT8",         "openvino", {"cpu_model_size": "1.7B"}),
    ("Qwen", "Qwen3-ASR-1.7B Q4 (CRISPASR)", "crispasr", {"crisp_model": "qwen3", "crisp_qwen_quant": "q4"}),
    ("Qwen", "Qwen3-ASR-1.7B Q8 (CRISPASR)", "crispasr", {"crisp_model": "qwen3", "crisp_qwen_quant": "q8"}),
    # 日語動漫特化（cstr/qwen3-asr-1.7b-ja-anime）：同架構、針對日語微調，日文歌詞/台詞辨識較佳。
    ("Qwen", "Qwen3-ASR-1.7B 日語動漫 Q4 (CRISPASR)", "crispasr", {"crisp_model": "qwen3-ja", "crisp_qwen_quant": "q4"}),
    ("Qwen", "Qwen3-ASR-1.7B 日語動漫 Q8 (CRISPASR)", "crispasr", {"crisp_model": "qwen3-ja", "crisp_qwen_quant": "q8"}),
    # 台灣國語特化（JacobLinCool/TEA-ASR-1.1，Qwen3-ASR-1.7B 微調）：繁中／台灣用語／
    # 中英夾雜。HF 上現成的 mradermacher GGUF 是 llama.cpp mmproj 拆檔版、CrispASR
    # 讀不了，故本專案自行以 CrispASR 官方轉換器重轉為單檔融合式 GGUF（見 downloader）。
    ("Qwen", "TEA-ASR-1.1 台灣國語 Q8 (CRISPASR)", "crispasr", {"crisp_model": "tea", "crisp_qwen_quant": "q8"}),
    # chatllm（相容項）：永遠在目錄中（供 set_model 查得），但 get_model_options
    # 會依 _chatllm_available 決定是否實際呈現給前端。
    ("Qwen", _CHATLLM_LABEL,                "chatllm",  {}),
    ("Whisper (Breeze)", "Breeze Q4 (輕量)", "crispasr", {"crisp_model": "breeze", "crisp_quant": "q4"}),
    ("Whisper (Breeze)", "Breeze Q5 (標準)", "crispasr", {"crisp_model": "breeze", "crisp_quant": "q5"}),
    ("Whisper (Breeze)", "Breeze Q8 (精確)", "crispasr", {"crisp_model": "breeze", "crisp_quant": "q8"}),
]
_BREEZE_QUANT_LABEL = {"q4": "Breeze Q4 (輕量)", "q5": "Breeze Q5 (標準)", "q8": "Breeze Q8 (精確)"}
_QWEN_CASR_QUANT_LABEL = {"q4": "Qwen3-ASR-1.7B Q4 (CRISPASR)",
                          "q8": "Qwen3-ASR-1.7B Q8 (CRISPASR)"}
_QWEN_JA_CASR_QUANT_LABEL = {"q4": "Qwen3-ASR-1.7B 日語動漫 Q4 (CRISPASR)",
                             "q8": "Qwen3-ASR-1.7B 日語動漫 Q8 (CRISPASR)"}
_TEA_CASR_QUANT_LABEL = {"q8": "TEA-ASR-1.1 台灣國語 Q8 (CRISPASR)"}
# TEA-ASR 是「凍結音訊編碼器 ＋ 解碼器小 LoRA、不到 10 小時公開語料」的輕量微調
# （見模型卡）。實測（crispasr / q8_0 與未量化 f16 結果一致，故非量化所致）：
#   • 乾淨人聲：辨識最佳，且**原生輸出繁體**（免簡繁轉換）
#   • 配樂較重的段落／歌曲：該段可能整段無輸出（模型直接吐 EOS）
# 這是模型的域外行為，不是包裝層可修的，故如實標示讓使用者自行選材。
_TEA_DOMAIN_NOTE = _t.msg("profile.note.tea")
# 模型標籤 → 提醒文字（目前只有 TEA 有；chatllm 的提醒仍依 backend 給）
_MODEL_NOTES = {_TEA_CASR_QUANT_LABEL["q8"]: _TEA_DOMAIN_NOTE}

# ══════════════════════════════════════════════════════════════════════
# 基礎模式：用「用途」而非「模型名」讓使用者選
# ══════════════════════════════════════════════════════════════════════
#   模型頁分兩種模式（預設基礎）：
#     基礎(basic)   → 選用途 → 依偵測到的硬體給出建議模型 → 按同一顆載入鈕
#     進階(advanced)→ 維持原本的「核心 + 模型下拉」全手動介面
#   兩者共用同一個 set_model / request_load 管道，故不會有狀態不一致。
#
# 硬體分級（tier）決定同一用途要給哪個量化：
#   gpu  = 有獨立顯卡（NVIDIA／AMD 獨顯）→ 給最準的 Q8
#   igpu = 只有內顯（Intel／AMD APU）    → 給較輕的 Q4／Q5
#   cpu  = 完全沒偵測到顯示卡            → 中文用途改推 OpenVINO 0.6B（純 CPU 主場）
_BASIC_PROFILES = [
    {
        "key":   "zh",
        "title": _t.msg("profile.zh.title"),
        "desc":  _t.msg("profile.zh.desc"),
        "picks": {
            "gpu":  ("Qwen", _QWEN_CASR_QUANT_LABEL["q8"]),
            "igpu": ("Qwen", _QWEN_CASR_QUANT_LABEL["q4"]),
            "cpu":  ("Qwen", "Qwen3-ASR-0.6B"),
        },
    },
    {
        "key":   "tea",
        "title": _t.msg("profile.tea.title"),
        "desc":  _t.msg("profile.tea.desc"),
        "note":  _TEA_DOMAIN_NOTE,
        "picks": {
            "gpu":  ("Qwen", _TEA_CASR_QUANT_LABEL["q8"]),
            "igpu": ("Qwen", _TEA_CASR_QUANT_LABEL["q8"]),
            "cpu":  ("Qwen", _TEA_CASR_QUANT_LABEL["q8"]),
        },
    },
    {
        "key":   "ja",
        "title": _t.msg("profile.ja.title"),
        "desc":  _t.msg("profile.ja.desc"),
        "picks": {
            "gpu":  ("Qwen", _QWEN_JA_CASR_QUANT_LABEL["q8"]),
            "igpu": ("Qwen", _QWEN_JA_CASR_QUANT_LABEL["q4"]),
            "cpu":  ("Qwen", _QWEN_JA_CASR_QUANT_LABEL["q4"]),
        },
    },
    {
        "key":   "twmix",
        "title": _t.msg("profile.twmix.title"),
        "desc":  _t.msg("profile.twmix.desc"),
        "picks": {
            "gpu":  ("Whisper (Breeze)", _BREEZE_QUANT_LABEL["q8"]),
            "igpu": ("Whisper (Breeze)", _BREEZE_QUANT_LABEL["q5"]),
            "cpu":  ("Whisper (Breeze)", _BREEZE_QUANT_LABEL["q5"]),
        },
    },
]
# 沒有顯示卡時，非中文用途只剩 GGUF 模型可用 → 據實提醒會比較慢
_BASIC_CPU_SLOW_NOTE = _t.msg("profile.note.cpuSlow")
# crispasr-Qwen 系列的 crisp_model 值（皆走 --backend qwen3-1.7b、同置 ov_models/、
# 共用 crisp_qwen_quant，差別只在權重與下載來源）。"breeze" 不在此列。
_QWEN_CASR_MODELS = ("qwen3", "qwen3-ja", "tea")
# 各 crispasr-Qwen 模型在下載訊息中的中文註記
_QWEN_CASR_NOTE = {"qwen3-ja": "（日語動漫）", "tea": "（台灣國語）"}

# Qwen GGUF 量化的**唯一**缺省值。首啟種子（_seed_defaults）與所有讀取側
# （_load_crispasr / selected_model_present / health / set_model …）都必須引用
# 這一個常數。曾經兩邊各寫一份字面量：種子寫 q4、讀取缺省寫 q8 —— 於是
# 「首啟下載 q4 → 載入時找 q8 → 再下一遍」，而模型還沒到位時自動載入路径
# 會在啟動當下拉起一次數百 MB 的下載（使用者看到的就是「首次開啟閃退，
# 但 settings.json 已建立成功」）。用常數把兩邊釘在一起，改一處即全改。
_QWEN_QUANT_DEFAULT = "q4"


def _qwen_casr_dl(crisp_model: str):
    """依 crisp_model 回傳對應的 (檔名, 存在檢查, 下載) 三個下載器函式。

    qwen3     → 標準 Qwen3-ASR-1.7B（cstr/qwen3-asr-1.7b-GGUF）
    qwen3-ja  → 日語動漫特化（cstr/qwen3-asr-1.7b-ja-anime-GGUF）
    tea       → 台灣國語特化（TEA-ASR-1.1，本專案自轉的融合式 GGUF）
    讓所有「crispasr-Qwen」路徑共用一處分派，避免各模型分支散落各方法。
    """
    from downloader import (qwen3_asr_gguf_filename, quick_check_qwen3_asr_gguf,
                            download_qwen3_asr_gguf, qwen3_asr_ja_gguf_filename,
                            quick_check_qwen3_asr_ja_gguf, download_qwen3_asr_ja_gguf,
                            tea_asr_gguf_filename, quick_check_tea_asr_gguf,
                            download_tea_asr_gguf)
    if crisp_model == "qwen3-ja":
        return (qwen3_asr_ja_gguf_filename, quick_check_qwen3_asr_ja_gguf,
                download_qwen3_asr_ja_gguf)
    if crisp_model == "tea":
        return (tea_asr_gguf_filename, quick_check_tea_asr_gguf,
                download_tea_asr_gguf)
    return (qwen3_asr_gguf_filename, quick_check_qwen3_asr_gguf,
            download_qwen3_asr_gguf)

# 說話者分離是「與後端無關的外部 ONNX」(diarize.py / DiarizationEngine)：
# OpenVINO 與 CRISPASR(whisper/qwen) 皆支援——前者 process_file 內建 use_diar 分支，
# 後者由 crisp_engine._apply_diarization 依時間指派。diar_engine 由 _ensure_diarization 掛上。
_DIARIZE_BACKENDS = {"openvino", "chatllm", "crispasr"}

# 辨識語言：crispasr/chatllm 共用的常用語系清單（OpenVINO 改用 processor 的）
_COMMON_LANGS = [
    "Chinese", "English", "Japanese", "Korean", "Cantonese", "French", "German",
    "Spanish", "Portuguese", "Russian", "Arabic", "Thai", "Vietnamese",
    "Indonesian", "Malay",
]


def parse_srt_to_segments(srt_path) -> list[dict]:
    """把 process_file 產出的 SRT 解析成前端要的 segments。

    說話者分離時，SRT 文字以「說話者 N：…」開頭 → 拆出 speaker 與 text。
    """
    try:
        from api_server import _parse_srt
    except Exception:
        return []
    if not srt_path or not Path(srt_path).exists():
        return []
    raw = _parse_srt(Path(srt_path).read_text(encoding="utf-8"))
    out = []
    for s in raw:
        text, speaker = s["text"], None
        if "：" in text[:8]:
            head, _, rest = text.partition("：")
            if head.startswith("說話者"):
                digits = "".join(c for c in head if c.isdigit())
                speaker = int(digits) if digits else None
                text = rest
        out.append({"start": s["start"], "end": s["end"], "speaker": speaker, "text": text})
    return out


def _rich_to_segments(rich) -> list[dict]:
    """引擎側通道 _last_segments_rich → 前端 segments（含字級 words，卡拉OK用）。

    引擎存的 speaker 是原始標籤（"說話者N" 或 None）；此處正規化成整數，
    與 parse_srt_to_segments 的前端契約一致。words 原樣帶出（[]=無對齊）。
    """
    out = []
    for seg in (rich or []):
        spk = seg.get("speaker")
        speaker = None
        if isinstance(spk, int):
            speaker = spk
        elif isinstance(spk, str):
            digits = "".join(c for c in spk if c.isdigit())
            speaker = int(digits) if digits else None
        out.append({
            "start": seg["start"], "end": seg["end"],
            "speaker": speaker, "text": seg["text"],
            "words": seg.get("words") or [],
        })
    return out


class WebBackend:
    """桌面 WebView 的後端。HTTP 層（webview_server）持有一個實例。"""

    def __init__(self, on_event=None):
        """on_event(event:str, payload:dict)：狀態/進度推送回呼（可選）。"""
        # G5: record first-run BEFORE _seed_defaults() creates settings.json.
        try:
            self._first_run = not Path(getattr(
                core, "SETTINGS_FILE", BASE_DIR / "settings.json")).exists()
        except Exception:
            self._first_run = False
        self.engine = core.ASREngine()
        self._loaded = False
        self._load_err = None
        self._loading = False
        self._cancel = False
        self._cancel_event = threading.Event()   # 长音频 Runner 使用的可等待取消信号
        self._server = None              # api_server.TranscribeServer（LAN 端點）
        self._tunnel = None              # cf_tunnel.CloudflareTunnel（對外臨時網址，延遲建立）
        self._on_event = on_event
        # Wave 11／問題 8：標記目前轉寫任務的歸屬（None=單檔、batch=批次）。
        # 批次 worker 會在呼叫 transcribe() 前設成 "batch"，讓 SSE 的
        # progress / progress-detail 帶 scope，前端才知道要畫在「批次」頁，
        # 而不是把批次進度誤顯示在「音檔轉字幕」頁。
        self._progress_scope = None
        # Wave 12／問題 1：模型記憶體生命週期。
        #   _running       正在執行中的 transcribe() 數（釋放必須等它歸零）
        #   _batch_running BatchQueue 整隊進行中（其間不在每個 item 後重載模型）
        self._running = 0
        self._batch_running = False
        # Wave 14：任務結束（正常/取消/例外）後若要釋放，先登記在這裡，
        # 等真正閒下來再放，避免取消時 worker 還在 transcribe() 裡就硬放。
        self._release_pending = False
        self._released_once = False       # 曾載入過又被釋放 → 才允許懶載入
        # H1: structured runtime log shared by the WebView and LAN endpoint.
        self.run_log = RunLog(on_event=on_event)
        self._theme_cb = None            # 主題變更回呼（app_webview 用來同步視窗標題列深淺）
        self._lock = threading.Lock()
        self._seed_defaults()            # 首次啟動（無 backend）→ 種子預設模型
        self._adopt_existing_quant()     # 既有設定缺量化 → 沿用磁碟上已有的那份
        self._apply_runtime_prefs()      # 啟動即套用持久化偏好（VAD/簡繁/鏡像/格式）
        self._install_download_hook()    # classified download events → SSE / run log

    # ── Model catalog, download manifest and offline import ───────────────
    def _install_download_hook(self):
        """Route downloader.py source switches / retries to the WebView UI."""
        import downloader as _dl

        def _hook(kind: str, payload: dict):
            self._emit(kind, payload)
            hint = payload.get("hint") or ""
            name = payload.get("file") or ""
            if kind == "download-source-switch":
                self._st(_t.msg("progress.downloadSource", name=name,
                                code=payload.get('code', ''), hint=hint))
            elif kind == "download-retry":
                self._st(_t.msg("progress.retry", attempt=payload.get('attempt', 1),
                                name=name, hint=hint))

        try:
            _dl.set_download_event_hook(_hook)
            # J5: surface a TLS verification downgrade as a persistent
            # warning (run log + UI banner), not a silent fallback.
            _dl.set_ssl_insecure_hook(
                lambda reason: self._emit("warning", {
                    "message": _t.msg("warn.sslInsecure", reason=str(reason)),
                    "code": "ssl_insecure"}))
        except Exception:
            pass

    def _catalog_paths(self) -> tuple[Path, Path]:
        """下載中心需要的**兩個**根目錄：(應用根, 模型目錄)。

        models.json 的 ``target_dir`` 一律寫成「相對應用根」：
          * ``ov_models/qwen3_asr_int8``、``ov_models``、``ov_models/diarization``
          * ``ffmpeg``、``crispasr``

        而 ``settings.model_dir``（預設 ``<app>/ov_models``）本身就是那個
        ``ov_models`` 資料夾。Wave 12 之前把後者直接當成 ``model_root`` 傳給
        model_catalog，於是拼出 ``<app>/ov_models/ov_models/qwen3_asr_int8``
        —— 多一層 ``ov_models``，下載中心永遠判定「檔案缺失」。

        現在兩者分開傳：model_catalog 會把 ``ov_models/...`` 的開頭那段
        換成 ``model_dir``，其餘 bundle 仍相對 ``app_root``。
        """
        s = self._settings_raw()
        default = getattr(core, "_DEFAULT_MODEL_DIR", BASE_DIR / "ov_models")
        return BASE_DIR, Path(s.get("model_dir", str(default)))

    def get_model_catalog(self) -> dict:
        """Return bundle/file status from models.json (never from guesses)."""
        import model_catalog as mc
        app_root, model_dir = self._catalog_paths()
        doc = mc.load_catalog()
        st = mc.catalog_status(app_root, doc, model_dir)
        st["mirror"] = self.get_settings().get("mirror", "")
        self._decorate_catalog_runtime(st)
        return st

    # ── 下載中心 × 系統自檢：同一份判斷（Wave 14／需求 11）──────────────
    #   下載中心原本只看「models.json 列的每個檔案在不在」。但 crispasr_core
    #   這個 bundle 列了 5 個加速版本 zip，使用者只會裝其中一個 → 永遠「缺 4 個」，
    #   於是系統自檢顯示綠燈（crispasr.exe 在、版本吻合）、下載中心卻說沒裝好。
    #   ffmpeg 同理：清單要求 ffmpeg.zip，實際只需要 ffmpeg.exe。
    #   這裡把「執行期真正在乎的判斷」附在 bundle 上，前端一律優先採用它。
    def _decorate_catalog_runtime(self, st: dict):
        try:
            from downloader import quick_check_crispasr, _CRISPASR_VERSION
            crisp_ok = quick_check_crispasr(self._crispasr_dir(), self._crisp_variant())
        except Exception:
            crisp_ok = False
            _CRISPASR_VERSION = ""
        try:
            _ff = self._resolve_ffmpeg()
            ff_ok = bool(_ff)
            ff_path = str(_ff) if _ff else ""
        except Exception:
            ff_ok, ff_path = False, ""
        # VAD 與自檢共用 _vad_present（見該函式註解：兩邊各寫一套曾導致
        # 「自檢綠燈、下載中心說未下載」）。
        try:
            vad_ok = _vad_present(self._model_dir(),
                                  getattr(self.engine, "vad_sess", None))
        except Exception:
            vad_ok = False
        for b in st.get("bundles", []) or []:
            bid = b.get("id")
            if bid == "crispasr_core":
                b["runtimeReady"] = bool(crisp_ok)
                b["runtimeNote"] = _t.msg("dl.runtimeCrisp", version=_CRISPASR_VERSION)
                # 只要「已安裝的那個加速版本」在，就算完整，別再數另外 4 個 zip。
                if crisp_ok:
                    b["complete"] = True
                    b["present_count"] = b.get("total_count")
            elif bid == "silero_vad_v4":
                b["runtimeReady"] = bool(vad_ok)
                b["runtimeNote"] = (_t.msg("dl.runtimeVadOk") if vad_ok
                                    else _t.msg("dl.runtimeVadMissing"))
                if vad_ok:
                    b["complete"] = True
                    b["present_count"] = b.get("total_count")
            elif bid == "ffmpeg_windows":
                b["runtimeReady"] = ff_ok
                b["runtimePath"] = ff_path
                b["runtimeNote"] = (_t.msg("dl.runtimeFfmpegOk", path=ff_path) if ff_ok
                                    else _t.msg("dl.runtimeFfmpegMissing"))
                if ff_ok:
                    b["complete"] = True
                    b["present_count"] = b.get("total_count")
        # 重算彙總，讓「已就緒 N/M」跟自檢結論一致。
        ids = [x.get("id") for x in st.get("bundles", []) or []]
        st["complete"] = [x["id"] for x in st.get("bundles", []) or [] if x.get("complete")]
        st["incomplete"] = [i for i in ids if i not in set(st["complete"])]

    def export_download_manifest(self, bundle_ids=None) -> dict:
        """Write a .txt download list with URLs, hashes and aria2c commands."""
        import model_catalog as mc
        app_root, model_dir = self._catalog_paths()
        ids = list(bundle_ids or [])
        doc = mc.load_catalog()
        if ids:
            for bid in ids:
                mc.get_bundle(bid, doc)      # validates ids early
        mirror = self.get_settings().get("mirror", "")
        text = mc.build_manifest_text(app_root, ids, mirror, doc, model_dir)
        out_dir = Path(getattr(core, "OUTPUT_DIR", BASE_DIR / "subtitles"))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = BASE_DIR
        path = out_dir / "模型下載清單.txt"
        path.write_text(text, encoding="utf-8")
        self._st(_t.msg("progress.manifest", path=path))
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size,
                "bundle_count": len(ids) if ids else len(doc["bundles"])}

    def import_local_model(self, src_dir: str, bundle_id: str) -> dict:
        """Validate manually downloaded files and copy them into place."""
        import model_catalog as mc
        app_root, model_dir = self._catalog_paths()
        doc = mc.load_catalog()
        bundle = mc.get_bundle(bundle_id, doc)
        result = mc.import_local_bundle(Path(src_dir), app_root, bundle, model_dir)
        self._st(_t.msg("progress.importLocal", bundle=bundle_id,
                                message=result['message']))
        return result

    # ── 首次啟動預設：1.7B Qwen on CRISPASR Q4（多數機器跑得動）─────────────
    def _seed_defaults(self):
        """settings.json 尚無 backend 時，種下預設模型選擇。

        多數狀況下「Qwen3-ASR-1.7B Q4（CRISPASR/Vulkan）」可順利運行（GPU Vulkan，
        繁中佳）。寫入 settings 後，後續 _persisted_backend/_load_worker 等皆讀到它。
        既有使用者（已有 backend）不受影響。
        """
        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            cur = {}
        if cur.get("backend"):
            return                       # 既有設定 → 不覆寫
        cur.setdefault("backend", "crispasr")
        cur.setdefault("crisp_model", "qwen3")
        cur.setdefault("crisp_qwen_quant", _QWEN_QUANT_DEFAULT)
        try:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _adopt_existing_quant(self):
        """settings 沒寫量化時，沿用「磁碟上已經有的那一份」，不要害人白下載。

        背景：`_seed_defaults()` 只在 settings 完全沒有 backend 時動手。從舊版
        升上來的使用者大多**已經有 backend**，於是 `crisp_qwen_quant` 這個鍵
        根本不存在，讀取時一律落到缺省值。

        這就出事了：缺省值原本是 `q8`，改成 `q4` 之後，一個早就下好 q8 的使用者
        會突然被判定成「模型不就緒」，然後在啟動當下開始下載另一個 q4 —— 明明
        手邊那份能用，卻要為一個純內部的預設值改動再等十幾分鐘。

        所以在這裡補一次「就地面化」：沒有顯式設定時，先看哪個量化檔真的存在，
        存在就把它寫進 settings（一次，之後就是顯式值）。都沒有才交給缺省值。
        代價是啟動多兩次 `Path.exists()`，可以忽略。
        """
        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            return
        if not cur or cur.get("crisp_qwen_quant"):
            return                       # 已顯式指定 → 尊重使用者
        if cur.get("backend") != "crispasr":
            return                       # 不是 crispasr 就與量化無關
        if cur.get("crisp_model", "breeze") not in _QWEN_CASR_MODELS:
            return                       # breeze 系列用 crisp_quant，另有一套
        try:
            from downloader import quick_check_qwen3_asr_gguf
        except Exception:
            return
        model_dir = Path(cur.get("model_dir") or
                         str(getattr(core, "_DEFAULT_MODEL_DIR", BASE_DIR / "ov_models")))
        for q in ("q8", _QWEN_QUANT_DEFAULT):   # q8 先試：舊缺省，多數老使用者是它
            try:
                if quick_check_qwen3_asr_gguf(model_dir, q):
                    cur["crisp_qwen_quant"] = q
                    f.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
                    return
            except Exception:
                continue

    # ── 事件推送 ────────────────────────────────────────────
    def _emit(self, event, payload):
        # Wave 14／需求 3：進度事件一律附帶本次任務的 taskId。前端據此判斷
        # 「這是同一個檔案的續跑」，才不會在斷點續傳時把已完成的段數清零；
        # 同時保留 pct/status，向後相容所有既有呼叫方與測試。
        if event in ("progress", "progress-detail") and isinstance(payload, dict) \
                and payload.get("taskId") is None:
            _tid = getattr(self, "_current_task_id", None)
            if _tid:
                payload["taskId"] = _tid
        return self._emit_inner(event, payload)

    def set_progress_scope(self, scope: str | None):
        """設定進度事件歸屬（BatchQueue 在跑某個 item 時設 "batch"）。

        Wave 12：同時記住「批次進行中」，讓 _end_run() 不在每個 item 之後
        就把模型卸掉又重載（那會讓 8 個檔案各付一次數十秒的編譯時間）。
        整隊結束由 BatchQueue 呼叫 release_model_if_enabled() 統一釋放。
        注意 BatchQueue 每個 item 的 finally 都會設回 None，所以這裡**不**
        在 scope=None 時清旗標。
        """
        self._progress_scope = scope
        if scope == "batch":
            self._batch_running = True

    def _emit_inner(self, event: str, payload: dict):
        # Wave 11：只對進度類事件加 scope，其餘事件契約完全不變。
        # 單檔任務 scope=None → payload 一個字都不動，舊前端也相容。
        scope = getattr(self, "_progress_scope", None)
        if scope and event in ("progress", "progress-detail") and isinstance(payload, dict):
            payload = dict(payload)
            payload["scope"] = scope
        # Mirror user-visible backend messages into the run log. "log" itself
        # is excluded to avoid an infinite publish loop.
        if event in ("status", "capabilities", "download", "warning") and isinstance(payload, dict):
            msg = payload.get("message") or payload.get("status")
            if msg:
                level = "warn" if event in ("warning",) else "info"
                self.run_log.add(_t.plain(msg), level=level, source=event)
        if self._on_event:
            try:
                self._on_event(event, payload)
            except Exception:
                pass

    def get_logs(self, limit: int | None = None) -> dict:
        """Return the recent runtime log (H1)."""
        return self.run_log.snapshot(limit)

    def clear_logs(self) -> dict:
        """Empty the runtime log buffer without touching disk."""
        self.run_log.clear()
        return {"ok": True}

    # ── 模型記憶體釋放（Wave 12／問題 1）────────────────────────────
    #   轉寫完成後，OpenVINO 編譯模型、ONNX VAD/diar session、ForcedAligner
    #   與本次的字級結果都還掛在 self.engine 上；longform 跑完幾 GB 會一直
    #   佔著，直到關掉程式才還給 OS。這裡提供「斷開引用 + GC + 換空殼」的
    #   卸載路徑，並在下次轉寫前自動重新載入（_ensure_engine）。
    _ENGINE_HEAVY_ATTRS = (
        "vad_sess", "audio_enc", "embedder", "dec_req", "processor",
        "cc", "diar_engine", "aligner", "use_aligner", "_fa", "_fa_bin",
        "_runner", "_model_dir", "_last_segments_rich", "_last_vad_diag",
        "kv_cache", "past_key_values",
    )

    def _auto_release_enabled(self) -> bool:
        """自動釋放開關；GPU 後端不自動釋放（Vulkan context 刻意常駐）。

        chatllm / crispasr 的執行期核心讓 Vulkan context 常駐（見
        vulkan-dual-context-crash），在每次轉寫後主動 GC 掉 DLL runner 有
        風險，因此只提供手動釋放，不做自動卸載。
        """
        try:
            if self._settings_raw().get("auto_release_model", True) is False:
                return False
        except Exception:
            pass
        return getattr(self, "_active_backend", "openvino") not in ("chatllm", "crispasr")

    @staticmethod
    def _drop_engine_refs(eng) -> int:
        """切斷引擎對重量級物件的引用；回傳實際清掉的屬性數。"""
        n = 0
        for name in WebBackend._ENGINE_HEAVY_ATTRS:
            try:
                if getattr(eng, name, None) is not None:
                    setattr(eng, name, None)
                    n += 1
            except Exception:
                pass
        try:
            eng.ready = False
        except Exception:
            pass
        return n

    def release_model(self, reason: str = "manual") -> dict:
        """釋放模型記憶體 → 回到「尚未載入」；下次轉寫會自動重新載入。

        不把 self.engine 設成 None：get_status / get_languages / health_check /
        toggle_endpoint 一律用 getattr(self.engine, ...) 讀，留一個同介面的
        空殼才不會在釋放後炸 AttributeError。
        """
        with self._lock:
            if self._running > 0 or self._loading:
                return {"ok": False, "released": False, "reason": "busy"}
            dropped = self._drop_engine_refs(self.engine)
            self.engine = core.ASREngine()
            self._loaded = False
            self._load_err = None
            self._released_once = True
        gc.collect()
        self._emit("status", {
            "modelReady": False, "released": True,
            "message": "模型記憶體已釋放；下次轉檔會自動重新載入。",
        })
        return {"ok": True, "released": True, "dropped": dropped,
                "reason": reason}

    def release_model_if_enabled(self, reason: str = "batch-finished") -> dict:
        """BatchQueue 整隊結束／取消時呼叫（FakeBackend 用 getattr 相容）。

        Wave 14／需求 17、18：取消佇列時 worker 執行緒可能還卡在 transcribe()
        裡（_running 仍是 1），舊版這裡直接 return skipped，於是「取消後記憶體
        沒放、重新開始時才先放再佔」。現在改成：先登記「本輪結束後要釋放」，
        等 _end_run() 真的把 _running 歸零時再釋放；若當下就已經閒置，立刻放。
        """
        self._batch_running = False
        self._release_pending = True
        if not self._auto_release_enabled():
            self._release_pending = False
            return {"ok": False, "released": False, "reason": "skipped"}
        if self._running > 0:
            # 還在跑：交給 _end_run() 收尾，這裡不阻塞呼叫端（cancel 是 UI 同步路徑）。
            return {"ok": True, "released": False, "reason": "deferred"}
        self._release_pending = False
        return self.release_model(reason=reason)

    def _ensure_engine(self):
        """模型「曾載入過又被釋放」時同步重新載入。

        刻意只處理釋放後的復活：首次啟動、模型還沒下載時**絕不**在這裡
        觸發 _load_worker()（那會在使用者只是點了「切分預檢」時拉起數百 MB
        下載）。那種情況直接返回，交給呼叫端給出原本「請先下載並載入模型」
        的可操作提示。
        """
        if getattr(self.engine, "ready", False):
            return
        # 背景載入正在跑 → 等它結束，避免並發載入兩份引擎。
        deadline = time.monotonic() + 180
        while self._loading and time.monotonic() < deadline:
            time.sleep(0.1)
        if getattr(self.engine, "ready", False):
            return
        if not getattr(self, "_released_once", False):
            return
        self._loading = True
        try:
            self._load_worker()
        finally:
            self._loading = False
        if not getattr(self.engine, "ready", False):
            raise RuntimeError(self._load_err or "模型載入失敗，請至模型頁重新載入。")

    def _begin_run(self):
        self._ensure_engine()
        with self._lock:
            # 新的任務開始 → 之前登記的「待釋放」作廢（本輪結束自然會再判斷）。
            self._release_pending = False
            self._running += 1

    def _end_run(self):
        with self._lock:
            self._running = max(0, self._running - 1)
            busy = self._running > 0
            pending = bool(getattr(self, "_release_pending", False))
        # 不論是否卸載模型，都要丟掉本次的字級結果／VAD 診斷快取。
        # 這份 rich 結果在數小時音檔上可達數十 MB，是「轉完記憶體沒回落」
        # 的另一個來源。
        self._drop_run_cache()
        gc.collect()
        if busy or not self._auto_release_enabled():
            return
        # Wave 14／需求 17、18：舊版額外要求 _batch_running 為 False 才釋放，
        # 但取消佇列後這個旗標會殘留 True，導致取消後模型一直掛在記憶體裡，
        # 直到下一次開始才被放掉（先放再佔）。
        # 但也不能一併無視 _batch_running：那會讓「整隊進行中」每個 item 結束
        # 就卸載一次模型，下一個檔案又要重新編譯數十秒。
        # 折衷且精確：**只有登記了待釋放（取消／整隊結束）才無視 _batch_running**。
        if self._batch_running and not pending:
            return
        self._batch_running = False
        self._release_pending = False
        try:
            self.release_model(reason="auto" if not pending else "batch-canceled")
        except Exception:
            pass

    def release_on_idle(self, reason: str = "idle") -> dict:
        """沒有任何任務在跑時立刻釋放模型；有任務則登記待釋放。

        由 UI 的「停止」與視窗關閉路徑呼叫，補上「取消後仍佔記憶體」的缺口。
        """
        if self._running > 0 or self._loading:
            self._release_pending = True
            return {"ok": True, "released": False, "reason": "deferred"}
        if not self._auto_release_enabled():
            return {"ok": False, "released": False, "reason": "disabled"}
        self._release_pending = False
        return self.release_model(reason=reason)

    def _drop_run_cache(self):
        for name in ("_last_segments_rich", "_last_vad_diag"):
            try:
                setattr(self.engine, name, None)
            except Exception:
                pass

    # ── 模型載入（背景執行緒呼叫）──────────────────────────
    def start_load(self):
        if self._loading or self._loaded:
            return
        self._loading = True
        threading.Thread(target=self._load_worker, name="model-loader", daemon=True).start()

    def _load_worker(self):
        # 依 settings.backend 全新載入對應引擎。因「切換=重啟」，每次啟動只載入
        # 一個核心、且為全新行程，天然避開 chatllm Vulkan 雙 context 切換當機。
        backend = self._persisted_backend()           # openvino / chatllm / crispasr
        try:
            if backend == "chatllm":
                self._load_chatllm()
            elif backend == "crispasr":
                self._load_crispasr()
            else:
                backend = "openvino"
                self._load_openvino()
            self._loaded = True
            self._active_backend = backend
            self._remember_active(backend)
            self._emit("status", {"modelReady": True})
        except Exception as e:
            traceback.print_exc()
            head = str(e).splitlines()[0][:110] if str(e) else type(e).__name__
            # GPU 核心載入失敗 → 退回 CPU(OpenVINO)，但明確告知(非靜默回退)
            if backend != "openvino":
                self._emit("progress", {"pct": 0, "status":
                    _t.msg("progress.gpuFallback", backend=backend)})
                try:
                    self._load_openvino()
                    self._loaded = True
                    self._active_backend = "openvino"
                    self._remember_active("openvino")
                    self._load_err = f"{backend} 核心載入失敗，已退回 CPU(OpenVINO)：{head}"
                    self._emit("status", {"modelReady": True, "error": self._load_err})
                    return
                except Exception:
                    traceback.print_exc()
            self._load_err = str(e)
            self._emit("status", {"modelReady": False, "error": str(e)})
        finally:
            self._loading = False

    # ── 各核心載入（重用既有引擎類別與 downloader）─────────────
    def _settings_raw(self) -> dict:
        try:
            f = getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json")
            return json.loads(Path(f).read_text(encoding="utf-8")) if Path(f).exists() else {}
        except Exception:
            return {}

    def _vk_device_id(self, s: dict) -> int:
        import re
        m = re.search(r"GPU:(\d+)", s.get("device", "") or "")
        return int(m.group(1)) if m else 0

    # 每段最長秒數：使用者設定 → 套到當前引擎，但永遠夾到「模型天花板」。
    # 天花板＝引擎類別的 max_chunk_secs（0.6B/chatllm 30、1.7B 10），由音訊
    # 編碼器匯出長度寫死，超過會被靜默截斷掉字，故只允許往短調，絕不超過。
    # crispasr（無 max_chunk_secs 類別屬性）走自身視窗，不受此設定影響。
    _CHUNK_FLOOR = 5
    # Only the first N subtitles are sent to the WebView. Rendering every card
    # of a 3-5 h lecture (thousands of DOM nodes) crashed the WebView2
    # renderer with "Out of memory" right after a successful run.
    _MAX_PREVIEW_SEGMENTS = 200
    def _apply_chunk_secs(self) -> None:
        eng = getattr(self, "engine", None)
        ceiling = getattr(type(eng), "max_chunk_secs", None) if eng is not None else None
        if not isinstance(ceiling, int):
            return                                  # crispasr 等：不以 max_chunk_secs 分段
        try:
            want = int(self._settings_raw().get("chunk_secs", 0) or 0)
        except Exception:
            want = 0
        eff = ceiling if want <= 0 else max(self._CHUNK_FLOOR, min(want, ceiling))
        try:
            eng.max_chunk_secs = eff                # 實例屬性遮蓋類別預設，僅影響本實例
        except Exception:
            pass

    def _dl_progress(self, *a):
        """下載進度回呼（容忍 progress_cb(frac) 或 progress_cb(frac, msg)）。"""
        frac = a[0] if a else 0
        msg = a[1] if len(a) > 1 else "下載中…"
        try:
            self._emit("progress", {"pct": int(float(frac) * 100), "status": str(msg)})
        except Exception:
            pass

    def _st(self, m):
        self._emit("progress", {"pct": 0, "status": m})

    def _load_openvino(self):
        s = self._settings_raw()
        model_dir = Path(s.get("model_dir", str(core._DEFAULT_MODEL_DIR)))
        use_17b = "1.7B" in s.get("cpu_model_size", "0.6B")
        cpu_threads = int(s.get("cpu_threads", 0) or 0)
        if use_17b:
            from downloader import quick_check_1p7b, download_1p7b
            if not quick_check_1p7b(model_dir):
                self._st(_t.msg("progress.download17b"))
                download_1p7b(model_dir, progress_cb=self._dl_progress)
        eng = core.ASREngine1p7B() if use_17b else core.ASREngine()
        eng.load(device="CPU", model_dir=model_dir, cb=self._st, cpu_threads=cpu_threads)
        self.engine = eng

    def _load_chatllm(self):
        from chatllm_engine import ChatLLMASREngine
        s = self._settings_raw()
        chatllm_dir = Path(s.get("chatllm_dir", str(getattr(core, "_CHATLLM_DIR", BASE_DIR / "chatllm"))))
        default_bin = getattr(core, "_BIN_PATH", BASE_DIR / "ov_models" / "qwen3-asr-1.7b.bin")
        model_path = Path(s.get("model_path") or s.get("gguf_path") or str(default_bin))
        if not model_path.exists():
            for c in (Path(s.get("model_dir", "")) / "qwen3-asr-1.7b.bin" if s.get("model_dir") else None,
                      default_bin):
                if c and Path(c).exists():
                    model_path = Path(c)
                    break
        if not model_path.exists():
            self._download_chatllm_bin(model_path)
        eng = ChatLLMASREngine()
        eng.load(model_path=model_path, chatllm_dir=chatllm_dir, n_gpu_layers=99,
                 device_id=self._vk_device_id(s), cb=self._st)
        self.engine = eng

    def _download_chatllm_bin(self, model_path: Path):
        import urllib.request
        from downloader import _ssl_ctx
        self._st(_t.msg("progress.downloadChatllm"))
        url = "https://huggingface.co/dseditor/Collection/resolve/main/qwen3-asr-1.7b.bin"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; QwenASR)"})
        with urllib.request.urlopen(req, context=_ssl_ctx()) as resp, \
                open(str(model_path) + ".tmp", "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            while True:
                block = resp.read(65536)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total > 0:
                    self._dl_progress(done / total, f"模型 {done/1048576:.0f}/{total/1048576:.0f} MB")
        import os as _os
        _os.replace(str(model_path) + ".tmp", str(model_path))

    def _load_crispasr(self):
        from crisp_engine import CrispWhisperEngine
        from downloader import (quick_check_crispasr, download_crispasr_core,
                                quick_check_breeze, download_breeze, breeze_filename,
                                quick_check_aligner_gguf, download_aligner_gguf,
                                aligner_gguf_filename, crispasr_gpu_backend)
        s = self._settings_raw()
        crispasr_dir = self._crispasr_dir()
        crisp_model = s.get("crisp_model", "breeze")     # breeze / qwen3 / qwen3-ja / tea
        quant = s.get("crisp_quant", "q5")
        fa_enabled = bool(s.get("crisp_fa", True))
        fa_quant = s.get("crisp_fa_quant", "q5")
        variant = self._crisp_variant()                  # 加速版本（vulkan/cuda…）

        # crispasr.exe 核心一律需要。帶 variant 檢查 → 版本升級或使用者改選
        # 加速版本時都會判定為「不就緒」而重新下載對應的 zip。
        if not quick_check_crispasr(crispasr_dir, variant):
            self._st(_t.msg("progress.downloadCrisp"))
            download_crispasr_core(crispasr_dir, progress_cb=self._dl_progress,
                                   variant=variant)

        if crisp_model in _QWEN_CASR_MODELS:
            # Qwen3-ASR GGUF（與 OV 模型同置 ov_models/；crisp_engine 依檔名含 "1.7b"
            # 自動推斷 --backend qwen3-1.7b）。標準版與日語動漫版共用此路徑，僅來源/
            # 檔名不同（由 _qwen_casr_dl 分派）。Q4/Q8 兩量化，缺檔自動下載（cstr repo）。
            fname_fn, check_fn, download_fn = _qwen_casr_dl(crisp_model)
            qquant = s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT)
            model_dir = Path(s.get("model_dir", str(getattr(core, "_DEFAULT_MODEL_DIR",
                                                            BASE_DIR / "ov_models"))))
            model_path = model_dir / fname_fn(qquant)
            if not check_fn(model_dir, qquant):
                _note = _QWEN_CASR_NOTE.get(crisp_model, "")
                _name = "TEA-ASR-1.1" if crisp_model == "tea" else "Qwen3-ASR-1.7B"
                self._st(_t.msg("progress.downloadGguf", name=_name, note=_note,
                                quant=qquant.upper()))
                download_fn(model_dir, qquant, progress_cb=self._dl_progress)
        else:
            model_path = crispasr_dir / breeze_filename(quant)
            if not quick_check_breeze(crispasr_dir, quant):
                self._st(_t.msg("progress.downloadBreeze", quant=quant.upper()))
                download_breeze(crispasr_dir, quant, progress_cb=self._dl_progress)
        aligner_path = None
        if fa_enabled:
            if quick_check_aligner_gguf(crispasr_dir, fa_quant):
                aligner_path = crispasr_dir / aligner_gguf_filename(fa_quant)
            else:
                try:
                    self._st(_t.msg("progress.downloadAligner"))
                    download_aligner_gguf(crispasr_dir, fa_quant, progress_cb=self._dl_progress)
                    aligner_path = crispasr_dir / aligner_gguf_filename(fa_quant)
                except Exception:
                    aligner_path = None       # 退回 Whisper 自帶時間軸
        eng = CrispWhisperEngine()
        eng.load(model_path=model_path, crispasr_dir=crispasr_dir,
                 device_id=self._vk_device_id(s), cb=self._st, aligner_path=aligner_path,
                 gpu_backend=crispasr_gpu_backend(variant))
        self.engine = eng

    # ── 狀態 ────────────────────────────────────────────────
    def get_status(self) -> dict:
        active = getattr(self, "_active_backend", "openvino")
        return {
            "modelReady": bool(getattr(self.engine, "ready", False)),
            "loading": self._loading,
            "error": self._load_err,
            "backend": self._backend_label(active, _t.render("status.backendCpu")),
            "backendKey": self._persisted_backend(),       # 已記住的核心選擇
            "activeBackend": active,                        # 實際載入中的核心
            "device": "GPU" if active in ("chatllm", "crispasr") else "CPU",
            "version": self._app_version(),
            "appName": _t.render("status.appName"),
            # 是否已有「任一組」模型下載完成（資訊用）
            "hasAnyModel": self._has_any_model(),
            # 目前選擇的模型是否已就緒 → 前端決定開始頁（就緒=語音轉文字／否=模型頁等下載）
            "selectedReady": self.selected_model_present(),
            # J5: let a reloaded page re-show the TLS downgrade banner.
            "sslInsecure": self._ssl_insecure(),
            "firstRun": bool(getattr(self, "_first_run", False)),
            # Wave 12：前端「轉檔後自動釋放」開關的目前值。
            "autoReleaseModel": self._auto_release_enabled(),
        }

    @staticmethod
    def _ssl_insecure() -> bool:
        try:
            import downloader
            return bool(downloader.ssl_insecure_active())
        except Exception:
            return False

    def selected_model_present(self) -> bool:
        """目前持久化選擇的『那一個』模型檔是否已下載。

        決定兩件事：① 啟動時是否自動載入（present→直接載；缺→不自動下載，等
        使用者在模型頁選好、按「下載並載入」才觸發，讓老手能先改選 Whisper 等）；
        ② 前端開始頁（present→語音轉文字；缺→模型頁）。
        """
        try:
            from downloader import (quick_check, quick_check_1p7b, quick_check_breeze,
                                    quick_check_crispasr)
            s = self._settings_raw()
            be = s.get("backend", "openvino")
            model_dir = self._model_dir()
            if be == "openvino":
                return (quick_check_1p7b(model_dir)
                        if "1.7B" in s.get("cpu_model_size", "0.6B") else quick_check(model_dir))
            if be == "crispasr":
                cd = self._crispasr_dir()
                if not quick_check_crispasr(cd):
                    return False                  # 核心 exe 缺（理論上隨包附帶）→ 當作未就緒
                cm = s.get("crisp_model", "breeze")
                if cm in _QWEN_CASR_MODELS:
                    _, check_fn, _ = _qwen_casr_dl(cm)
                    return check_fn(model_dir, s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT))
                return quick_check_breeze(cd, s.get("crisp_quant", "q5"))
            if be == "chatllm":
                return ((self._chatllm_dir() / "qwen3-asr-1.7b.bin").exists()
                        or (model_dir / "qwen3-asr-1.7b.bin").exists())
        except Exception:
            traceback.print_exc()
        return False

    def _has_any_model(self) -> bool:
        """機器上是否已有任一可用模型（任一核心任一量化）。供首啟頁面決策。"""
        try:
            from downloader import (quick_check, quick_check_1p7b, quick_check_breeze,
                                    quick_check_qwen3_asr_gguf, quick_check_qwen3_asr_ja_gguf,
                                    quick_check_tea_asr_gguf)
            model_dir = self._model_dir()
            crispasr_dir = self._crispasr_dir()
            if quick_check(model_dir) or quick_check_1p7b(model_dir):
                return True
            for q in ("q4", "q5", "q8"):
                if quick_check_breeze(crispasr_dir, q):
                    return True
            for q in ("q4", "q8"):
                if quick_check_qwen3_asr_gguf(model_dir, q) or quick_check_qwen3_asr_ja_gguf(model_dir, q):
                    return True
            if quick_check_tea_asr_gguf(model_dir, "q8"):
                return True
            # chatllm .bin（向下相容）
            if (self._chatllm_dir() / "qwen3-asr-1.7b.bin").exists():
                return True
            if (model_dir / "qwen3-asr-1.7b.bin").exists():
                return True
        except Exception:
            traceback.print_exc()
        return False

    def _persisted_backend(self) -> str:
        try:
            f = getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json")
            if Path(f).exists():
                return json.loads(Path(f).read_text(encoding="utf-8")).get("backend", "openvino")
        except Exception:
            pass
        return "openvino"

    def _app_version(self) -> str:
        try:
            import version
            # WebView 版優先用獨立版本字串（webview 0.1）；缺則退回語意化版本。
            return getattr(version, "WEBVIEW_VERSION", None) or version.__version__
        except Exception:
            return ""

    # ── 轉錄 ────────────────────────────────────────────────
    # opts: {path, language, diarize, nSpeakers, align, hint}
    # progress_cb(pct:int, status:str)
    def transcribe(self, opts: dict, progress_cb=None) -> dict:
        """轉寫入口：懶載入 → 執行 → 收尾釋放（Wave 12／問題 1）。

        原本 transcribe() 跑完就把模型、字級結果全留在 self.engine 上，
        記憶體一路佔到關程式。改成 try/finally 後，無論成功、取消或例外，
        _end_run() 都會清掉本次增量快取，並在單檔任務結束時釋放模型；
        下一次轉寫由 _begin_run() 自動重新載入，功能不受影響。
        """
        self._begin_run()
        try:
            return self._transcribe_impl(opts, progress_cb)
        finally:
            self._end_run()

    def _transcribe_impl(self, opts: dict, progress_cb=None) -> dict:
        if not getattr(self.engine, "ready", False):
            raise RuntimeError("模型尚未載入完成，請稍候再試。")
        path = opts.get("path")
        if not path or not Path(path).exists():
            raise RuntimeError("找不到音訊檔。")
        self._cancel = False
        self._cancel_event.clear()   # 每次转写前复位，避免上次的取消影响本轮

        def _cb(i, total, msg):
            if progress_cb:
                pct = min(99, int((i / max(total, 1)) * 100))
                progress_cb(pct, msg)

        # 影片 → 先抽音軌（與 api_server 一致）
        audio_path = Path(path)
        tmp_extra = None
        try:
            ext = audio_path.suffix.lower()
            from api_server import _VIDEO_HINT
            if ext in _VIDEO_HINT:
                from ffmpeg_utils import extract_audio_to_wav
                ff = self._ensure_ffmpeg()
                if not ff:
                    raise RuntimeError("上傳為影片但找不到 ffmpeg，且自動下載失敗，無法抽音軌。")
                wav = audio_path.with_suffix(".extracted.wav")
                extract_audio_to_wav(audio_path, wav, ff)
                audio_path, tmp_extra = wav, wav

            # Single pipeline for every input, short or long. The legacy
            # process_file() path loaded the whole waveform and wrote the SRT
            # only at the end; keeping two frameworks meant two sets of limits
            # and a length-dependent memory spike. All entries (GUI / batch /
            # CLI / API) now share the streaming runner.
            from longform_runner import probe_duration, TRIAL_MINUTES
            _ff = str(self._resolve_ffmpeg() or "ffmpeg")
            self._emit("progress", {"pct": 0, "status": "正在探测音频时长…"})
            _duration = probe_duration(audio_path, _ff)
            trial = bool(opts.get("trial"))
            limit_sec = TRIAL_MINUTES * 60 if trial else None

            _st = self._settings_raw()
            _ceiling = getattr(type(self.engine), "max_chunk_secs", None)
            if not isinstance(_ceiling, int):
                _ceiling = 30
            try:
                _want = int(_st.get("chunk_secs", 0) or 0)
            except Exception:
                _want = 0
            _effective = _ceiling if _want <= 0 else max(5, min(_want, _ceiling))
            _is_1p7 = "1p7" in getattr(type(self.engine), "_OV_SUBDIR", "").lower() \
                or "1p7" in type(self.engine).__name__.lower()
            _ram_mb = 6000 if _is_1p7 else 2000

            # Word timing and speaker labels are optional enrichments, not
            # discarded features. Report both availability (is the model
            # installed?) and whether this run actually applies them. A
            # missing model degrades to plain subtitles, never to a failure.
            _align_ok = bool(getattr(self.engine, "_fa_bin", None)
                             or getattr(self.engine, "use_aligner", False))
            _diar_ok = bool(getattr(getattr(self.engine, "diar_engine", None),
                                    "ready", False))
            _want_align = bool(opts.get("align"))
            _want_diar = bool(opts.get("diarize"))
            _align_active = _want_align and _align_ok
            _diar_active = _want_diar and _diar_ok
            self._emit("capabilities", {
                "streaming": True,
                "alignAvailable": _align_ok,
                "diarAvailable": _diar_ok,
                "alignActive": _align_active,
                "diarActive": _diar_active,
                "effectiveChunkSecs": _effective,
                "modelCeilingSecs": _ceiling,
                "estimatedRamMB": _ram_mb,
                "trial": trial,
            })
            for _wanted, _ok, _label in ((_want_align, _align_ok, "词级时间轴"),
                                          (_want_diar, _diar_ok, "说话人分离")):
                if _wanted and not _ok:
                    self._emit("status", {"message":
                        _t.msg("progress.enrichSkipped", label=_label)})
            mins = (_duration or 0) / 60
            self._emit("progress", {"pct": 0, "status":
                _t.msg("progress.runInfo",
                       trial=(_t.render("progress.trialPrefix", {"mins": TRIAL_MINUTES})
                              if trial else ""),
                       mins=f"{mins:.0f}", chunk=_effective,
                       ram=f"{_ram_mb / 1024:.1f}")})

            # 語言：空字串 / 自動偵測 / auto → None（讓引擎自動偵測）
            _lang = (opts.get("language") or "").strip()
            if _lang in ("", "自動偵測", "auto"):
                _lang = None

            # 輸出落點：subtitles/ 下、用「原始上傳檔名」命名（可在「開啟輸出資料夾」找到）。
            # 上傳檔名為使用者可控（含 LAN/外網端點）→ 必須只取基名，杜絕路徑穿越。
            srt_dir = Path(getattr(core, "SRT_DIR", BASE_DIR / "subtitles"))
            srt_dir.mkdir(parents=True, exist_ok=True)
            raw_name = opts.get("name") or Path(path).name
            out_name = Path(str(raw_name).replace("\\", "/")).name   # 去除任何目錄成分
            if not out_name or out_name.startswith("."):
                out_name = "transcript"
            out_ref = srt_dir / out_name
            # 防呆：確認解析後仍在 subtitles/ 內
            try:
                if srt_dir.resolve() not in out_ref.resolve().parents:
                    out_ref = srt_dir / "transcript"
            except Exception:
                out_ref = srt_dir / "transcript"

            with self._lock:
                self._apply_chunk_secs()      # 依設定夾定本次 max_chunk_secs（夾到模型上限）
                # 清除上一輪字級殘留，避免本輪若提前 return 時讀到舊資料
                try:
                    self.engine._last_segments_rich = None
                except Exception:
                    pass

                # Single streaming path: a single worker. ASREngine.transcribe()
                # holds a mutex, so extra threads only queue, while extra
                # processes each load their own IR and multiply memory peaks.
                from longform_runner import LongformRunner
                from postprocess import StreamEnhancer, load_rich

                def _splitter(text, t0, t1):
                    # Reuse the original line splitting / timestamp logic.
                    return core._assign_ts(core._split_to_lines(text), t0, t1)

                task_id = hashlib.sha1(
                    str(Path(path).resolve()).encode("utf-8")).hexdigest()[:12]
                out_srt = out_ref.with_suffix(".srt")
                if trial:
                    # A trial run must never pollute the full run's checkpoint
                    # or overwrite its SRT.
                    task_id += "-trial"
                    out_srt = out_ref.with_name(out_ref.stem + ".trial.srt")
                # Wave 14／需求 3：讓後續 progress / progress-detail 事件都帶上
                # taskId，前端才能分辨「同一檔案的續跑」與「換了檔案」。
                self._current_task_id = task_id

                # Fingerprint everything that changes the transcript. A
                # mismatch forces a clean restart instead of resuming into a
                # half-old / half-new SRT (different model, script, chunk...).
                _hint = (opts.get("hint") or "").strip()
                _fp_parts = [
                    f"backend={getattr(self, '_active_backend', '')}",
                    f"size={_st.get('cpu_model_size', '')}",
                    f"chunk={getattr(self.engine, 'max_chunk_secs', '')}",
                    f"vad={_st.get('vad_threshold', 0.5)}",
                    f"lang={_lang or 'auto'}",
                    f"script={_st.get('output_simplified', False)}"
                    f"/{_st.get('vocab_convert', True)}",
                    f"hint={hashlib.sha1(_hint.encode('utf-8')).hexdigest()[:8]}",
                    f"trial={trial}",
                ]
                try:
                    _src = Path(audio_path).stat()
                    _fp_parts.append(f"src={_src.st_size}/{int(_src.st_mtime)}")
                except OSError:
                    pass
                fingerprint = hashlib.sha1(
                    "|".join(_fp_parts).encode("utf-8")).hexdigest()[:16]

                rich_path = out_srt.with_suffix(".rich.json")
                # A stale enrichment file must never leak into a new run.
                try:
                    rich_path.unlink(missing_ok=True)
                except OSError:
                    pass
                _nsp = opts.get("nSpeakers")
                try:
                    _nsp = int(_nsp) if _nsp and str(_nsp) not in ("auto", "自動") else None
                except (TypeError, ValueError):
                    _nsp = None
                enhancer = StreamEnhancer(
                    engine=self.engine,
                    rich_path=rich_path,
                    language=_lang,
                    enable_align=_align_active,
                    enable_diar=_diar_active,
                    n_speakers=_nsp,
                    status_cb=lambda m: self._emit("status", {"message": m}),
                    existing_jsonl=BASE_DIR / "longform_state" /
                                   f"{task_id}.segments.jsonl",
                )

                runner = LongformRunner(
                    engine=self.engine,
                    vad_sess=getattr(self.engine, "vad_sess", None),
                    out_srt=out_srt,
                    work_dir=BASE_DIR / "longform_state",
                    ffmpeg=_ff,
                    progress_cb=progress_cb,   # same (pct,status) contract
                    # Wave 4: structured progress + throttled live subtitles.
                    detail_cb=lambda d: self._emit("progress-detail", d),
                    segment_cb=lambda s: self._emit("segments", s),
                    cancel=self._cancel_event,
                    language=_lang,
                    context=_hint or None,
                    line_splitter=_splitter,
                    task_id=task_id,
                    fingerprint=fingerprint,
                    max_chunk_secs=int(getattr(self.engine, "max_chunk_secs", 30)),
                    vad_threshold=float(_st.get("vad_threshold", 0.5) or 0.5),
                    enhancer=enhancer,
                )
                result = runner.run(
                    audio_path, total_sec=_duration,
                    resume=not trial and bool(opts.get("longform_resume", True)),
                    limit_sec=limit_sec)
                if result["status"] == "paused":
                    # 取消／中斷也要產出 rich.json。
                    #   它是由 runner 的 JSONL **全量重建**的派生檔，不是增量資料源：
                    #   中斷時 JSONL 已含所有完成段，所以這裡得到的是「目前為止」
                    #   的完整結果；續跑結束後會再用全量 JSONL 重寫一次並覆蓋，
                    #   因此不會出現「前半段沒有、後半段才有」的斷層。
                    #   舊版在此直接 raise，等於中斷後使用者連字級時間軸／說話者
                    #   都拿不到，只能等整份跑完。
                    try:
                        enhancer.finalize(jsonl_path=runner.jsonl)
                    except Exception:
                        pass
                    raise RuntimeError(
                        "已取消；已完成字幕已保存，再次启动同一文件可从断点续跑。")
                srt = runner.out_srt
                runner_result = result
                # Enrichment is strictly best-effort: finalize() never raises
                # and the SRT has already been written line by line.
                try:
                    enhancer.finalize(jsonl_path=runner.jsonl)
                except Exception:
                    pass
        finally:
            if tmp_extra:
                try:
                    Path(tmp_extra).unlink(missing_ok=True)
                except Exception:
                    pass

        if not srt:
            diag = getattr(self.engine, "_last_vad_diag", None)
            raise RuntimeError(diag or "未產生字幕（未偵測到人聲）。")

        # UI 永遠以記憶體中的 segments 渲染波形/字幕卡（需時間軸），故引擎固定產 SRT。
        # 但「輸出格式」設定決定使用者實際取得的存檔：選純文字 → 另寫 .txt、移除 .srt。
        # 優先用引擎的字級結構（含 words，驅動卡拉OK）；無則退回解析 SRT（行級）。
        # Prefer the streaming enrichment file (words + speakers). Fall back
        # to the engine channel, then to plain SRT parsing. Every step is
        # independent of the SRT, which is already safely on disk.
        rich = load_rich(Path(str(srt)).with_suffix(".rich.json"))
        if not rich:
            rich = getattr(self.engine, "_last_segments_rich", None)
        segments = _rich_to_segments(rich) if rich else parse_srt_to_segments(srt)
        saved = str(srt)
        out_fmt = (self._settings_raw().get("output_format", "srt") or "srt").lower()
        if out_fmt == "txt":
            try:
                from subtitle_lines import write_transcript
                lines = [(seg["start"], seg["end"], seg["text"],
                          (f'說話者{seg["speaker"]}' if seg.get("speaker") else None))
                         for seg in segments]
                txt = write_transcript(Path(srt), lines, out_format="txt")
                try:
                    Path(srt).unlink(missing_ok=True)
                except Exception:
                    pass
                saved = str(txt)
            except Exception:
                traceback.print_exc()          # 退回 SRT，不讓轉錄整體失敗

        if progress_cb:
            progress_cb(100, "完成")

        total_segments = len(segments)
        preview = segments[:self._MAX_PREVIEW_SEGMENTS]
        result = runner_result or {}
        return {
            "segments": preview,
            "totalSegments": total_segments,
            "previewLimit": self._MAX_PREVIEW_SEGMENTS,
            "previewTruncated": total_segments > len(preview),
            "srtPath": saved,
            "trial": trial,
            "truncated": bool(result.get("truncated")),
            # Wave 14／需求 3：告知呼叫方本次是從第幾段續跑（1 = 全新）。
            # 目前已寫入的段數 = resumedFrom - 1，佇列據此保留「已處理段數」。
            "resumedFrom": int(result.get("resumeFrom") or 1),
            "resumed": bool(result.get("resumed")),
            "duration": result.get("duration") or (_duration or 0),
            "peaks": result.get("peaks") or [],
        }

    def vad_preview(self, opts: dict, progress_cb=None) -> dict:
        """Dry-run segmentation on the first PREVIEW_MINUTES of the selected file.

        No ASR, no model weights needed beyond the VAD session. The result is a
        statistics dict the recognition page renders as red/yellow/green.
        """
        from longform_runner import vad_preview as _vp, PREVIEW_MINUTES

        path = (opts or {}).get("path")
        if not path or not Path(path).exists():
            raise RuntimeError("請先選擇音檔，再進行切分預檢。")
        # Wave 12：模型可能已在上一輪轉寫後釋放；VAD 預檢需要 vad_sess，
        # 這裡先同步復活引擎，不要再丟一句「模型尚未載入」給使用者。
        self._ensure_engine()
        # 模型未載入時 vad_sess 為 None，LongformRunner 會拋 ValueError ——
        # 對使用者完全無用（看不出要幹嘛）。這裡攔下，給出可操作的提示。
        if getattr(self.engine, "vad_sess", None) is None:
            raise RuntimeError(
                "模型尚未載入，無法進行切分預檢。\n"
                "請先到「模型」頁下載並載入模型，完成後再試。")
        _ff = str(self._resolve_ffmpeg() or "ffmpeg")
        _st = self._settings_raw()
        ceiling = getattr(type(self.engine), "max_chunk_secs", None)
        if not isinstance(ceiling, int):
            ceiling = 30
        try:
            want = int(_st.get("chunk_secs", 0) or 0)
        except Exception:
            want = 0
        effective = ceiling if want <= 0 else max(self._CHUNK_FLOOR,
                                                  min(want, ceiling))
        _raw_thr = (opts or {}).get("vadThreshold")
        if _raw_thr in (None, ""):
            _raw_thr = _st.get("vad_threshold", 0.5)
        try:
            threshold = float(_raw_thr or 0.5)
        except Exception:
            threshold = 0.5
        threshold = max(0.1, min(0.95, threshold))

        self._emit("progress", {"pct": 0,
                    "status": f"切分預檢：只分析前 {PREVIEW_MINUTES} 分鐘（不跑辨識）"})
        cb = progress_cb or (lambda pct, status: self._emit(
            "progress", {"pct": pct, "status": status}))
        try:
            out = _vp(
                audio_path=Path(path),
                vad_sess=getattr(self.engine, "vad_sess", None),
                ffmpeg=_ff,
                threshold=threshold,
                max_chunk_secs=effective,
                minutes=int((opts or {}).get("minutes", PREVIEW_MINUTES) or
                            PREVIEW_MINUTES),
                progress_cb=cb,
                cancel=self._cancel_event,
            )
        except PermissionError as e:
            # Windows 上 WinError 5（拒绝访问）的成因很多：exe 所在目录无执行
            # 权限、AppLocker/WDAC 策略、受控文件夹访问、安全软件拦截子进程…
            # 不能一概归咎于「杀软扫描文件」，那会误导使用者去关杀软却毫无作用。
            # 这里只描述事实 + 给诊断线索。
            raise RuntimeError(
                f"无法读取音频文件：{e}\n"
                f"文件路径：{path}\n"
                f"使用的 ffmpeg：{_ff}\n"
                "WinError 5（拒绝访问）可能来自：\n"
                "  - ffmpeg 这个可执行文件所在目录没有「执行」权限\n"
                "  - Windows 的受控文件夹访问（Controlled Folder Access）拦截\n"
                "  - 杀毒软件 / EDR 阻止了 ffmpeg 子进程的创建\n"
                "  - AppLocker / WDAC / Smart App Control 策略阻止\n"
                "请把「文件路径」与「使用的 ffmpeg」一并反馈给开发者，便于定位。"
            ) from e
        out["effectiveChunkSecs"] = effective
        out["modelCeilingSecs"] = ceiling
        self._emit("vad-preview", out)
        return out

    def _resolve_ffmpeg(self):
        """解析 ffmpeg：優先採用設定中手動指定的 ffmpeg_path，否則自動偵測。

        與 setting.py 的偵測順序一致（使用者手動指定 → 系統 PATH / App 目錄）。
        """
        from ffmpeg_utils import find_ffmpeg
        p = (self._settings_raw().get("ffmpeg_path", "") or "").strip()
        if p and Path(p).exists():
            return Path(p)
        return find_ffmpeg()

    def _ensure_ffmpeg(self):
        """回傳可用的 ffmpeg；找不到時自 HF 下載精簡 zip 解壓後再找。

        ffmpeg 不隨安裝包附帶 → 首次需要（轉錄影片）時自動下載到 <app>/ffmpeg/。
        下載進度經 self._dl_progress → SSE 推給前端。失敗回 None（由上層報錯）。
        """
        ff = self._resolve_ffmpeg()
        if ff:
            return ff
        try:
            from ffmpeg_utils import get_default_ffmpeg_dest
            from downloader import quick_check_ffmpeg, download_ffmpeg
            dest = get_default_ffmpeg_dest()
            if not quick_check_ffmpeg(dest):
                self._st(_t.msg("progress.downloadFfmpeg"))
                download_ffmpeg(dest, progress_cb=self._dl_progress)
            return self._resolve_ffmpeg()
        except Exception:
            traceback.print_exc()
            return None

    def _model_dir(self) -> Path:
        s = self._settings_raw()
        return Path(s.get("model_dir", str(getattr(core, "_DEFAULT_MODEL_DIR",
                                                   BASE_DIR / "ov_models"))))

    # ── 按需下載：說話者分離 / FA（移植自 app.py 的 CTk 下載協調層）──────────
    def _ensure_diarization(self) -> bool:
        """確保說話者分離 ONNX 模型存在並把 diar_engine 掛到當前引擎。

        diar 是與後端無關的外部 ONNX，OpenVINO/CRISPASR 共用。缺模型則自動
        下載（約 32 MB），完成後建立 DiarizationEngine 掛上 self.engine.diar_engine。
        回傳是否就緒。
        """
        from downloader import quick_check_diarization, download_diarization
        from diarize import DiarizationEngine
        model_dir = self._model_dir()
        diar_dir = model_dir / "diarization"
        if not quick_check_diarization(model_dir):
            self._st(_t.msg("progress.downloadDiar"))
            download_diarization(diar_dir, progress_cb=self._dl_progress)
        eng = DiarizationEngine(diar_dir)
        if getattr(eng, "ready", False):
            try:
                self.engine.diar_engine = eng
            except Exception:
                pass
            return True
        return False

    def _ensure_fa(self) -> bool:
        """確保「時間軸對齊」模型存在（移植自 app.py _check_aligner_model）。

        CRISPASR：FA aligner gguf 已於 _load_crispasr 處理，這裡略過。
        OpenVINO/chatllm：需 chatllm ForcedAligner .bin（約 939 MB），缺則下載並
        重新載入引擎內的對齊器（eng._load_aligner）。回傳是否就緒。
        """
        backend = getattr(self, "_active_backend", "openvino")
        if backend == "crispasr":
            return bool(getattr(self.engine, "_fa_bin", None))
        try:
            from downloader import quick_check_aligner, download_aligner
            model_dir = self._model_dir()
            if not quick_check_aligner(model_dir):
                self._st(_t.msg("progress.downloadFa"))
                download_aligner(model_dir, progress_cb=self._dl_progress)
            if hasattr(self.engine, "_load_aligner"):
                self.engine._load_aligner(cb=self._st)
            return bool(getattr(self.engine, "_fa_bin", None)
                        or getattr(self.engine, "use_aligner", False))
        except Exception:
            traceback.print_exc()
            return False

    def cancel(self):
        self._cancel = True
        self._cancel_event.set()   # 同步通知长音频流式 Runner，使其保存断点后退出
        return True

    def open_output_dir(self) -> bool:
        import os
        try:
            d = getattr(core, "SRT_DIR", BASE_DIR / "subtitles")
            Path(d).mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
            return True
        except Exception:
            return False

    def open_releases(self) -> dict:
        """「檢查更新」→ 以系統瀏覽器開啟 GitHub Releases 頁。"""
        import webbrowser
        try:
            import version
            url = getattr(version, "GITHUB_RELEASES_PAGE",
                          "https://github.com/1-1-2/QwenASRGUI/releases/latest")
        except Exception:
            url = "https://github.com/1-1-2/QwenASRGUI/releases/latest"
        try:
            webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}

    # ── 裝置 ────────────────────────────────────────────────
    #   偵測以 **CrispASR 為主**（crispasr.exe --diagnostics 自帶 GPU 列舉，不需
    #   chatllm）。chatllm 的 main.exe --show_devices 未隨安裝包提供 → 改為後備：
    #   僅在 crispasr 不在、但機器上有 chatllm main.exe 時才用（向下相容）。
    def _crispasr_dir(self) -> Path:
        # 用 core.BASE_DIR（frozen 时 = sys.executable 旁；別用 webview_backend 的
        # __file__，冻结后它指向随包资源根：onefile 是 _MEIPASS 暂存夹，onedir 是
        # _internal/）→ 才找得到随包附带／运行时下载到 exe 旁的 crispasr/ 核心。
        s = self._settings_raw()
        app_dir = getattr(core, "BASE_DIR", BASE_DIR)
        return Path(s.get("crispasr_dir", str(app_dir / "crispasr")))

    # ── 推理加速版本（CrispASR 的 Vulkan / CUDA / CPU 三種 build）────────
    #   偵測→推薦→下載對應 zip。注意這裡的硬體偵測**不能**用既有的
    #   probe_crispasr_devices()（那要先有 crispasr.exe），而走 downloader 的
    #   detect_hardware()（WMI + nvidia-smi），才能在下載核心前就決定抓哪一包。
    def _crisp_variant(self) -> str:
        """目前選定的加速版本；settings 沒有就依硬體推薦一個並記住。"""
        from downloader import (crispasr_variants, recommend_crispasr_variant)
        s = self._settings_raw()
        v = s.get("crisp_variant")
        if v in crispasr_variants():
            return v
        try:
            v = recommend_crispasr_variant()["recommended"]
        except Exception:
            v = "vulkan"
        self._persist_setting("crisp_variant", v)
        return v

    def get_accel(self) -> dict:
        """回傳硬體偵測結果 + 加速版本選單（供前端「推理加速版本」區塊渲染）。"""
        from downloader import (recommend_crispasr_variant, installed_crispasr_info,
                                _CRISPASR_VERSION)
        try:
            info = recommend_crispasr_variant()
        except Exception as e:
            traceback.print_exc()
            return {"ok": False,
                    "error": _t.render("progress.accelProbeFailed", {"error": e}),
                    "options": []}
        installed = installed_crispasr_info(self._crispasr_dir())
        selected  = self._crisp_variant()
        # H6/H7: downloader publishes structured keys next to the legacy
        # Chinese strings. Translate option cards and the recommendation here
        # so the WebView receives localisable payloads; CTk keeps using the
        # plain strings straight from downloader.
        options = []
        for op in info.get("options", []):
            op = dict(op)
            ii = op.pop("i18n", None)
            if ii:
                op["label"] = _t.msg(ii["label"])
                op["desc"] = _t.msg(ii["desc"])
                if ii.get("note"):
                    op["note"] = _t.msg(ii["note"], **ii.get("vars", {}))
            options.append(op)
        ri = info.get("i18n_reason")
        reason = _t.msg(ri["key"], **ri["vars"]) if ri else info["reason"]
        return {
            "ok": True,
            "selected": selected,
            "recommended": info["recommended"],
            "reason": reason,
            "options": options,
            "hardware": info["hardware"],
            "installed": installed,          # {"version","variant"}；None＝尚未安裝
            "latest": _CRISPASR_VERSION,
            # 已裝但版本／版本別不符 → 前端提示「載入時會自動重新下載」
            "needsDownload": (installed.get("version") != _CRISPASR_VERSION
                              or installed.get("variant") != selected),
        }

    def set_accel(self, variant: str) -> dict:
        """記住使用者選的加速版本。實際下載發生在下次載入模型時。"""
        from downloader import crispasr_variants, installed_crispasr_info, _CRISPASR_VERSION
        variants = crispasr_variants()
        if variant not in variants:
            return {"ok": False,
                    "message": _t.msg("msg.accel.unknown", variant=variant)}
        self._persist_setting("crisp_variant", variant)
        meta = variants[variant]
        installed = installed_crispasr_info(self._crispasr_dir())
        fresh = (installed.get("version") == _CRISPASR_VERSION
                 and installed.get("variant") == variant)
        if fresh:
            return {"ok": True, "variant": variant, "restartRequired": False,
                    "message": _t.msg("msg.accel.ready",
                                      label=_t.msg(f"accel.opt.{variant}.label"))}
        return {"ok": True, "variant": variant, "restartRequired": True,
                "message": _t.msg("msg.accel.remembered",
                                  label=_t.msg(f"accel.opt.{variant}.label"),
                                  size=meta['size_mb'])}

    def list_devices(self) -> dict:
        import platform
        devices = [{"kind": "cpu", "name": platform.processor() or "CPU",
                    "note": _t.msg("device.inUse")}]
        diag = {"level": None, "text": ""}

        vk, source = self._probe_gpu_devices()
        try:
            for d in (vk.get("devices") if vk else []) or []:
                gb = d.get("vram_free", 0) / (1024 ** 3)
                devices.append({"kind": "gpu", "name": d.get("name", "GPU"),
                                "note": _t.msg("device.vram", gb=f"{gb:.1f}") if gb else ""})
            if source is None:
                # 沒有任何可用的偵測器（crispasr 與 chatllm 皆未就位）
                diag = {"level": "info",
                        "text": _t.msg("device.diag.noDetector")}
            elif vk and vk.get("error"):
                diag = {"level": "warn",
                        "text": _t.msg("device.diag.probeFailed", source=source,
                                                      error=str(vk['error']))}
            elif not (vk and vk.get("devices")):
                diag = {"level": "info",
                        "text": _t.msg("device.diag.noGpu")}
        except Exception as e:
            diag = {"level": "warn",
                    "text": _t.msg("device.diag.exception", error=str(e))}
        return {"devices": devices, "diag": diag}

    def _probe_gpu_devices(self):
        """回傳 (探測結果 dict, 來源標籤)；找不到任何偵測器時回 (None, None)。

        優先 CrispASR（crispasr.exe --diagnostics）；其次後備 chatllm（main.exe
        --show_devices，僅供仍備有 chatllm 的既有使用者）。
        """
        # ① CrispASR 為主
        try:
            from crisp_engine import probe_crispasr_devices, _find_exe
            cd = self._crispasr_dir()
            if _find_exe(cd):                       # 直接或子資料夾找到 crispasr.exe
                return probe_crispasr_devices(cd), "CrispASR"
        except Exception:
            traceback.print_exc()
        # ② chatllm 後備（向下相容）
        try:
            chatllm_dir = self._chatllm_dir()
            if (chatllm_dir / "main.exe").exists():
                return core.probe_vulkan_devices(str(chatllm_dir)), "chatllm"
        except Exception:
            traceback.print_exc()
        return None, None

    # ── 核心切換：持久化選擇 + 請使用者重啟（不就地熱重載）─────
    #   理由：① chatllm DLL 在核心切換時 Vulkan context 未釋放會整機當機
    #   (見記憶 vulkan-dual-context-crash)；② 統一所有核心的切換行為，最安全。
    #   chatllm 保留原桌面實作以向下相容；未來全面改 casr，但切換一律需重啟。
    _BACKENDS = {0: "openvino", 1: "chatllm", 2: "crispasr"}
    _BACKEND_LABELS = {
        "openvino": "CPU · OpenVINO INT8",
        "chatllm":  "GPU · chatllm Vulkan",
        # CRISPASR 的加速版本可切（Vulkan/CUDA/CPU）→ 實際標籤由 _backend_label()
        # 依 crisp_variant 補上；這裡只留字典的預設值供舊呼叫端使用。
        "crispasr": "GPU · CRISPASR",
    }

    @staticmethod
    def _note_for(backend: str, model_label: str = "") -> dict | str:
        """該（核心, 模型）要顯示的提醒文字；沒有則空字串。

        chatllm 依 backend 給（AMD 內顯問題），TEA 等依模型標籤給（模型域限制）。
        """
        if backend == "chatllm":
            return _CHATLLM_AMD_NOTE
        return _MODEL_NOTES.get(model_label, "")

    def _backend_label(self, backend: str, fallback: str = "") -> str:
        """核心顯示名。CRISPASR 會補上目前選定的加速版本（Vulkan／CUDA／CPU）。"""
        label = self._BACKEND_LABELS.get(backend)
        if label is None:
            return fallback or backend
        if backend == "crispasr":
            try:
                from downloader import crispasr_variants
                v = self._crisp_variant()
                meta = crispasr_variants().get(v)
                if meta:
                    kind = "CPU" if meta["gpu_backend"] is None else "GPU"
                    return f"{kind} · CRISPASR（{meta.get('short') or meta['label']}）"
            except Exception:
                pass
        return label

    def set_backend(self, idx) -> dict:
        backend = self._BACKENDS.get(int(idx) if str(idx).isdigit() else 0, "openvino")
        label = self._backend_label(backend)
        self._persist_backend(backend)
        active = getattr(self, "_active_backend", "openvino")
        if backend == active:
            return {"ok": True, "backend": backend, "restartRequired": False,
                    "message": _t.msg("msg.backend.same", label=label)}
        if backend == "openvino":
            return {"ok": True, "backend": backend, "restartRequired": True,
                    "message": _t.msg("msg.backend.restart", label=label)}
        # GPU 核心：重啟後會全新載入（首次啟用會自動下載對應模型）
        return {"ok": True, "backend": backend, "restartRequired": True,
                "message": _t.msg("msg.backend.firstRun", label=label)}

    def _persist_backend(self, backend: str):
        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            cur = {}
        cur["backend"] = backend
        try:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── 模型下拉（核心 + 模型）＋ 辨識語言 ─────────────────────────────
    #   持久化後走「切換=重啟」(同 set_backend 的理由：避免就地切核心當機)。
    def _remember_active(self, backend: str):
        """記住「實際載入」的選擇識別碼，供 set_model 判斷是否需重啟。"""
        self._active_backend = backend
        self._active_identity = self._identity_for(backend, self._settings_raw())

    def _identity_for(self, backend: str, s: dict):
        """把 (backend + 相關 settings) 化為可比較的識別碼（決定要不要重啟）。"""
        if backend == "openvino":
            return ("openvino", s.get("cpu_model_size", "0.6B"))
        if backend == "crispasr":
            cm = s.get("crisp_model", "breeze")
            quant = (s.get("crisp_quant", "q5") if cm == "breeze"
                     else s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT))   # qwen3 量化也納入識別
            # 加速版本也納入識別：換 Vulkan↔CUDA 等同換核心，需重啟才生效
            return ("crispasr", cm, quant, self._crisp_variant())
        return ("chatllm",)

    def _current_selection(self):
        """settings → (核心標籤, 模型標籤)，供下拉預選。"""
        s = self._settings_raw()
        be = s.get("backend", "openvino")
        if be == "crispasr":
            cm = s.get("crisp_model", "breeze")
            if cm == "tea":
                return ("Qwen", _TEA_CASR_QUANT_LABEL["q8"])
            if cm == "qwen3-ja":
                qq = s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT)
                return ("Qwen", _QWEN_JA_CASR_QUANT_LABEL.get(qq, _QWEN_JA_CASR_QUANT_LABEL["q8"]))
            if cm == "qwen3":
                qq = s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT)
                return ("Qwen", _QWEN_CASR_QUANT_LABEL.get(qq, _QWEN_CASR_QUANT_LABEL["q8"]))
            q = s.get("crisp_quant", "q5")
            return ("Whisper (Breeze)", _BREEZE_QUANT_LABEL.get(q, _BREEZE_QUANT_LABEL["q5"]))
        if be == "chatllm":
            return ("Qwen", _CHATLLM_LABEL)
        sz = s.get("cpu_model_size", "0.6B")
        return ("Qwen", "Qwen3-ASR-1.7B INT8" if "1.7B" in sz else "Qwen3-ASR-0.6B")

    def _chatllm_dir(self) -> Path:
        s = self._settings_raw()
        return Path(s.get("chatllm_dir",
                          str(getattr(core, "_CHATLLM_DIR", BASE_DIR / "chatllm"))))

    def _chatllm_available(self) -> bool:
        """chatllm 是否該對使用者「現身」（向下相容判準）。

        條件（任一成立）：① 機器上實際備有 chatllm 核心二進位（libchatllm.dll）；
        ② 目前已記住的 backend 就是 chatllm（曾選用過）。新安裝的乾淨機器兩者皆否
        → chatllm 不出現在模型清單／自檢，UI 維持以 CASR-Qwen 為主。
        """
        try:
            if self._persisted_backend() == "chatllm":
                return True
            if (self._chatllm_dir() / "libchatllm.dll").exists():
                return True
        except Exception:
            pass
        return False

    # ── 基礎模式：用途 → 依硬體建議模型 ────────────────────────────────
    def _hw_tier(self) -> tuple[str, str]:
        """回傳 (tier, 硬體摘要文字)。tier ∈ gpu / igpu / cpu。

        tier 決定基礎模式要推薦哪個量化：有獨顯給最準的 Q8，只有內顯給較輕的
        Q4／Q5，完全沒顯示卡則中文用途改推純 CPU 的 OpenVINO 0.6B。
        """
        from downloader import detect_hardware
        try:
            hw = detect_hardware()
        except Exception:
            return "igpu", _t.msg("hw.probeFailed")
        gpus = hw.get("gpus") or []
        if not gpus:
            return "cpu", _t.msg("hw.noGpu")
        names = "、".join(g["name"] for g in gpus)
        if hw.get("has_discrete"):
            return "gpu", _t.msg("hw.discrete", names=names)
        return "igpu", _t.msg("hw.integrated", names=names)

    def _present_for(self, backend: str, patch: dict) -> bool:
        """某個目錄項（backend + settings patch）的模型檔是否已下載。

        與 selected_model_present() 同樣的判斷，但改吃「任意一項」而非目前選定的，
        供基礎模式在卡片上標示「已下載／需下載」。
        """
        try:
            from downloader import (quick_check, quick_check_1p7b, quick_check_breeze)
            model_dir = self._model_dir()
            if backend == "openvino":
                return (quick_check_1p7b(model_dir)
                        if "1.7B" in patch.get("cpu_model_size", "0.6B")
                        else quick_check(model_dir))
            if backend == "crispasr":
                cm = patch.get("crisp_model", "breeze")
                if cm in _QWEN_CASR_MODELS:
                    _, check_fn, _ = _qwen_casr_dl(cm)
                    return check_fn(model_dir, patch.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT))
                return quick_check_breeze(self._crispasr_dir(),
                                          patch.get("crisp_quant", "q5"))
            if backend == "chatllm":
                return ((self._chatllm_dir() / "qwen3-asr-1.7b.bin").exists()
                        or (model_dir / "qwen3-asr-1.7b.bin").exists())
        except Exception:
            traceback.print_exc()
        return False

    def get_basic_profiles(self) -> dict:
        """基礎模式的用途清單（已套用硬體建議）。

        回傳 {"tier","hardware","accel","profiles":[…],"current"}；每個 profile 帶
        它在本機硬體下建議的 (core, model) 與該模型是否已下載。前端點選後仍走
        既有的 set_model()／request_load()，不另開載入路徑。
        """
        from downloader import crispasr_variants
        tier, hw_text = self._hw_tier()
        cur_core, cur_model = self._current_selection()
        variant = self._crisp_variant()
        vmeta = crispasr_variants().get(variant, {})

        profiles = []
        for p in _BASIC_PROFILES:
            core_label, model_label = p["picks"].get(tier, p["picks"]["igpu"])
            entry = next((e for e in _MODEL_CATALOG
                          if e[0] == core_label and e[1] == model_label), None)
            if entry is None:            # 目錄改名時不要整頁壞掉，跳過該用途
                continue
            _, _, be, patch = entry
            note = p.get("note", "")
            if tier == "cpu" and be != "openvino":
                note = (_t.join(note, _BASIC_CPU_SLOW_NOTE) if note
                        else _BASIC_CPU_SLOW_NOTE)
            profiles.append({
                "key": p["key"], "title": p["title"], "desc": p["desc"],
                "note": note,
                "core": core_label, "model": model_label,
                "arch": self._backend_label(be, be),
                "present": self._present_for(be, patch),
                "selected": (core_label == cur_core and model_label == cur_model),
            })
        return {
            "tier": tier, "hardware": hw_text,
            "accel": vmeta.get("short") or vmeta.get("label") or variant,
            "profiles": profiles,
            "current": {"core": cur_core, "model": cur_model},
        }

    def get_model_options(self) -> dict:
        """核心/模型階層 + 目前選擇 + 每模型對應架構標籤（前端渲染下拉用）。

        chatllm 為「向下相容」項：僅在 _chatllm_available 時納入清單（見其註解）。
        """
        cur_core, cur_model = self._current_selection()
        show_chatllm = self._chatllm_available()
        order, by_core = [], {}
        for core_label, model_label, be, _patch in _MODEL_CATALOG:
            if be == "chatllm" and not show_chatllm:
                continue
            if core_label not in by_core:
                by_core[core_label] = []
                order.append(core_label)
            by_core[core_label].append({
                "label": model_label, "backend": be,
                "arch": self._backend_label(be, be),
                "note": self._note_for(be, model_label),
            })
        active = getattr(self, "_active_backend", None)
        return {
            "cores": [{"label": c, "models": by_core[c]} for c in order],
            "current": {"core": cur_core, "model": cur_model},
            "activeArch": self._backend_label(active, "") if active else "",
        }

    def set_model(self, core_label: str, model_label: str) -> dict:
        """選定 (核心,模型) → 寫對應 settings 鍵、回是否需重啟。"""
        entry = next((e for e in _MODEL_CATALOG
                      if e[0] == core_label and e[1] == model_label), None)
        if entry is None:   # 防呆：退回該核心首項 / 目錄首項
            entry = next((e for e in _MODEL_CATALOG if e[0] == core_label),
                         _MODEL_CATALOG[0])
        core_label, model_label, backend, patch = entry

        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            cur = {}
        cur["backend"] = backend
        cur.update(patch)                 # cpu_model_size / crisp_model / crisp_quant
        try:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        arch = self._backend_label(backend, backend)
        # 尚未載入任何核心（首次啟動）→ 可「就地下載並載入」，免重啟（此時沒有
        # 既有 Vulkan context 會衝突，安全）。已載入核心後再換 → 仍需重啟（safety）。
        loaded_or_loading = self._loaded or self._loading
        identity_changed = (self._identity_for(backend, cur)
                            != getattr(self, "_active_identity", None))
        can_load_now = not loaded_or_loading
        restart = loaded_or_loading and identity_changed
        if can_load_now:
            message = _t.msg("msg.model.selectedCanLoad", core=core_label,
                            model=model_label, arch=arch)
        elif restart:
            message = _t.msg("msg.model.selectedRestart", core=core_label,
                            model=model_label, arch=arch)
        else:
            message = _t.msg("msg.model.same", core=core_label, model=model_label)
        note = self._note_for(backend, model_label)
        if note:                          # chatllm AMD 問題／TEA 域限制 → 一併提醒
            message = _t.msg("msg.model.withNote", message=message, note=note)
        return {
            "ok": True, "core": core_label, "model": model_label,
            "backend": backend, "arch": arch, "restartRequired": restart,
            "canLoadNow": can_load_now,
            "note": note,
            "message": message,
        }

    def request_load(self) -> dict:
        """前端「下載並載入模型」→ 觸發背景載入（讀已持久化的 backend 選擇）。

        僅在尚未載入時有效（start_load 內含 guard）。下載/載入進度經 SSE
        "progress" 事件推送，完成時 "status" 事件 modelReady=True。
        """
        already = self._loaded or self._loading
        self.start_load()
        return {"ok": True, "loading": True, "alreadyLoaded": bool(self._loaded),
                "wasBusy": bool(already)}

    def get_languages(self) -> dict:
        """辨識語言清單：OpenVINO 用 processor.supported_languages，其餘用常用清單。

        value="" 代表自動偵測（transcribe 會轉成 None）；其餘 value 即引擎接受的
        語系字串（如 "Chinese"）。
        """
        langs = None
        try:
            proc = getattr(self.engine, "processor", None)
            if proc is not None and getattr(proc, "supported_languages", None):
                langs = list(proc.supported_languages)
        except Exception:
            langs = None
        if not langs:
            langs = _COMMON_LANGS
        return {"languages": [{"label": _t.msg("msg.lang.auto"), "value": ""}]
                + [{"label": l, "value": l} for l in langs]}

    # ── 啟動自檢：每核心 × 每能力，實際探測檔案/鍵路是否就緒 ─────────────
    #   status：green=已就緒 / yellow=缺但會自動下載 / red=缺且需處理 / na=不適用
    def health_check(self) -> dict:
        from downloader import (quick_check, quick_check_1p7b, quick_check_aligner,
                                quick_check_diarization, quick_check_crispasr,
                                quick_check_breeze, quick_check_qwen3_asr_gguf,
                                quick_check_qwen3_asr_ja_gguf, quick_check_aligner_gguf,
                                quick_check_tea_asr_gguf, crispasr_variants,
                                _CRISPASR_VERSION)
        s = self._settings_raw()
        model_dir = self._model_dir()
        crispasr_dir = self._crispasr_dir()

        def item(key, label, ok, ok_d, miss_d, downloadable=True):
            return {"key": key, "label": label,
                    "status": "green" if ok else ("yellow" if downloadable else "red"),
                    "detail": ok_d if ok else miss_d}

        def _vad_ok():
            return _vad_present(model_dir, getattr(self.engine, "vad_sess", None))

        # 共用：說話者分離（外部 ONNX，所有核心共用）＋ ffmpeg
        diar_ok = quick_check_diarization(model_dir)
        ff = self._resolve_ffmpeg()

        # OpenVINO（CPU）路徑
        ov_size = s.get("cpu_model_size", "0.6B")
        ov_model_ok = quick_check_1p7b(model_dir) if "1.7B" in ov_size else quick_check(model_dir)
        ov = {
            "label": _t.msg("health.group.ov"), "backend": "openvino",
            "items": [
                item("model", _t.msg("health.ov.model", size=ov_size), ov_model_ok,
                     _t.msg("state.downloaded"), _t.msg("state.notDownloaded")),
                item("vad", _t.msg("health.ov.vad"), _vad_ok(),
                     _t.msg("state.builtin"), _t.msg("state.vadMissing"), downloadable=False),
                item("fa", _t.msg("health.ov.fa"), quick_check_aligner(model_dir),
                     _t.msg("state.downloaded"), _t.msg("state.notDownloadedFa")),
                item("diar", _t.msg("health.ov.diar"), diar_ok,
                     _t.msg("state.downloaded"), _t.msg("state.notDownloadedDiar")),
            ],
        }

        # CRISPASR 路徑：核心 exe + Breeze/Qwen/TEA 模型 + FA aligner gguf + 共用 diar
        #   核心的「就緒」條件含版本與加速版本 → 舊版或選了別的加速版本都算未就緒，
        #   會在載入時自動重抓（訊息裡標明目前選的是哪一個 build）。
        variant = self._crisp_variant()
        variants_meta = crispasr_variants()
        vmeta = variants_meta.get(variant, variants_meta["vulkan"])
        core_ok = quick_check_crispasr(crispasr_dir, variant)
        # Wave 14／需求 15：修正「選 Qwen 1.7B + CPU，卻報核心未下載/版本不符」的誤報。
        #   舊版把兩件事混成一句：(a) 整體核心就緒與否，(b) 目前**選中的加速版本**。
        #   使用者選了 OpenVINO（CPU）這條路時，CRISPASR 核心本來就不需要；
        #   即便需要，只要 crispasr.exe 在、只是 marker 版本/變體跟設定不同，
        #   也該說「版本不符，啟用時會自動重下」，而不是「未下載」。
        _core_ver = None
        try:
            from downloader import installed_crispasr_info
            _core_ver = (installed_crispasr_info(crispasr_dir) or {}).get("version")
        except Exception:
            _core_ver = None
        _backend_now = getattr(self, "_active_backend", "openvino")
        if _backend_now == "openvino":
            _core_detail = _t.msg("health.crisp.coreNotNeeded")
        elif _core_ver == _CRISPASR_VERSION:
            _core_detail = _t.msg("health.crisp.coreVariantMismatch")
        else:
            _core_detail = _t.msg("health.crisp.coreMissing", size=vmeta['size_mb'])
        crisp_quant = s.get("crisp_quant", "q5")
        qwen_quant = s.get("crisp_qwen_quant", _QWEN_QUANT_DEFAULT)
        fa_quant = s.get("crisp_fa_quant", "q5")
        _variant_label = vmeta.get('short') or vmeta['label']
        crisp = {
            "label": _t.msg("health.group.crisp", variant=_variant_label),
            "backend": "crispasr",
            "items": [
                item("core", _t.msg("health.crisp.core", version=_CRISPASR_VERSION,
                                    variant=_variant_label),
                     core_ok, _t.msg("state.downloaded"), _core_detail),
                item("breeze", _t.msg("health.crisp.breeze", quant=crisp_quant.upper()),
                     quick_check_breeze(crispasr_dir, crisp_quant),
                     _t.msg("state.downloaded"), _t.msg("state.notDownloaded")),
                item("qwen", _t.msg("health.crisp.qwen", quant=qwen_quant.upper()),
                     quick_check_qwen3_asr_gguf(model_dir, qwen_quant),
                     _t.msg("state.downloaded"), _t.msg("state.notDownloaded")),
                item("qwen_ja", _t.msg("health.crisp.qwenJa", quant=qwen_quant.upper()),
                     quick_check_qwen3_asr_ja_gguf(model_dir, qwen_quant),
                     _t.msg("state.downloaded"), _t.msg("health.crisp.qwenJaMissing")),
                item("tea", _t.msg("health.crisp.tea"),
                     quick_check_tea_asr_gguf(model_dir, "q8"),
                     _t.msg("state.downloaded"), _t.msg("health.crisp.teaMissing")),
                item("fa", _t.msg("health.crisp.fa", quant=fa_quant.upper()),
                     quick_check_aligner_gguf(crispasr_dir, fa_quant),
                     _t.msg("state.downloaded"), _t.msg("health.crisp.faMissing")),
                item("diar", _t.msg("health.crisp.diar"), diar_ok,
                     _t.msg("state.downloaded"), _t.msg("state.notDownloadedDiar")),
            ],
        }

        shared = [
            item("ffmpeg", _t.msg("health.shared.ffmpeg"), bool(ff),
                 _t.msg("health.shared.ffmpegOk", path=str(ff)) if ff else "",
                 _t.msg("health.shared.ffmpegMissing"), downloadable=True),
            item("diar_shared", _t.msg("health.shared.diar"), diar_ok,
                 _t.msg("state.downloaded"), _t.msg("health.shared.diarMissing")),
        ]

        cores = [ov, crisp]

        # chatllm（向下相容）：僅在 _chatllm_available 時列出，讓既有使用者能確認
        # 其載入狀況。核心 DLL「不隨安裝包提供」→ 缺則紅燈（非可自動下載）；
        # .bin 模型缺則黃燈（啟用時自動下載）。FA／diar 與 OpenVINO 共用。
        if self._chatllm_available():
            chatllm_dir = self._chatllm_dir()
            dll_ok = (chatllm_dir / "libchatllm.dll").exists()
            bin_candidates = [
                Path(s.get("model_path") or "") if s.get("model_path") else None,
                Path(s.get("gguf_path") or "") if s.get("gguf_path") else None,
                getattr(core, "_BIN_PATH", None),
                model_dir / "qwen3-asr-1.7b.bin",
            ]
            bin_ok = any(p and Path(p).exists() for p in bin_candidates)
            chatllm = {
                "label": _t.msg("health.group.chatllm"), "backend": "chatllm",
                "items": [
                    item("core", _t.msg("health.chatllm.core"), dll_ok,
                         _t.msg("state.ready"), _t.msg("state.notProvided"),
                         downloadable=False),
                    item("model", _t.msg("health.chatllm.model"), bin_ok,
                         _t.msg("state.downloaded"), _t.msg("health.chatllm.modelMissing")),
                    item("fa", _t.msg("health.chatllm.fa"),
                         quick_check_aligner(model_dir),
                         _t.msg("state.downloaded"), _t.msg("state.notDownloadedFa")),
                    item("diar", _t.msg("health.crisp.diar"), diar_ok,
                         _t.msg("state.downloaded"), _t.msg("state.notDownloadedDiar")),
                ],
            }
            cores.append(chatllm)

        # 紅燈：缺且不可自動補（目前僅 VAD/ffmpeg 屬此類）
        reds = sum(1 for c in cores for it in c["items"] if it["status"] == "red")
        yellows = sum(1 for c in cores for it in c["items"] if it["status"] == "yellow")
        return {
            "cores": cores, "shared": shared,
            "summary": {"red": reds, "yellow": yellows,
                        "ok": reds == 0},
            "activeBackend": getattr(self, "_active_backend", None),
        }

    # ── 設定（讀寫既有 settings.json）───────────────────────
    def get_settings(self) -> dict:
        s = {}
        try:
            f = getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json")
            if Path(f).exists():
                s = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            s = {}
        # 簡繁/詞彙：app.py 以 output_simplified + vocab_convert 兩 bool 表達；
        # webview 前端用單一字串（off/s2twp/s2t）。此處還原成字串供前端預選。
        vc = s.get("vocab_convert", True)
        if isinstance(vc, str):                       # 舊版 webview 曾誤存字串 → 直接沿用
            vocab = vc if vc in ("off", "s2twp", "s2t") else "s2twp"
        else:
            vocab = self._flags_to_vocab(bool(s.get("output_simplified", False)), bool(vc))
        return {
            "scale": int(s.get("ui_scale", 100)),
            "format": s.get("output_format", "srt"),
            "vocab": vocab,
            "mirror": s.get("hf_mirror", ""),
            "ffmpeg": s.get("ffmpeg_path", ""),
            "theme": s.get("appearance", "light"),
            "uiLang": s.get("ui_lang", "繁體中文"),
            "vad": float(s.get("vad_threshold", 0.5)),
            "chunkSecs": int(s.get("chunk_secs", 0) or 0),   # 0=自動（依模型上限）
            # 模型頁模式：basic（用途導向，預設）／advanced（核心+模型全手動）
            "modelMode": (s.get("model_mode") if s.get("model_mode") in ("basic", "advanced")
                          else "basic"),
            # Wave 4 (B3): transcription-page controls are persisted too, so a
            # restart no longer resets language / diarization / hint / trial.
            "language": s.get("language", ""),
            "diarize": bool(s.get("diarize", True)),
            "nSpeakers": s.get("n_speakers", "auto"),
            "align": bool(s.get("align", True)),
            "hint": s.get("hint", ""),
            "trial": bool(s.get("trial", False)),
            # Wave 12：轉檔完成後自動釋放模型記憶體（GPU 後端會顯示為 off）。
            "autoRelease": bool(s.get("auto_release_model", True)),
        }

    def set_settings(self, patch: dict) -> dict:
        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        cur = {}
        try:
            if f.exists():
                cur = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
        patch = patch or {}
        # 簡繁/詞彙：webview 單一字串 → 寫成 app.py 相容的兩個 bool。
        if "vocab" in patch:
            simp, conv = self._vocab_to_flags(patch["vocab"])
            cur["output_simplified"] = simp
            cur["vocab_convert"] = conv
        key_map = {"scale": "ui_scale", "format": "output_format",
                   "mirror": "hf_mirror", "ffmpeg": "ffmpeg_path", "theme": "appearance",
                   "uiLang": "ui_lang", "vad": "vad_threshold", "chunkSecs": "chunk_secs",
                   "modelMode": "model_mode",
                   # Wave 4 (B3): transcription-page controls.
                   "language": "language", "diarize": "diarize",
                   "nSpeakers": "n_speakers", "align": "align", "hint": "hint",
                   "trial": "trial",
                   # Wave 12 (問題 1): model memory lifecycle.
                   "autoRelease": "auto_release_model"}
        for k, v in patch.items():
            if k in key_map:
                cur[key_map[k]] = v
        try:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        # ── 即時套用副作用（與 app.py 的 _on_*_change 對齊）──────────────
        if "vad" in patch:
            self._apply_vad(patch["vad"])      # _detect_speech_groups 讀全域，立即生效
        if "vocab" in patch:
            self._apply_vocab(*self._vocab_to_flags(patch["vocab"]))
        if "mirror" in patch:
            self._apply_mirror(patch["mirror"])
        if "format" in patch:
            self._apply_output_format(patch["format"])
        if "theme" in patch and self._theme_cb:    # 通知視窗層同步標題列深淺
            try:
                self._theme_cb(patch["theme"])
            except Exception:
                pass
        return self.get_settings()

    def set_theme_callback(self, cb):
        """由 app_webview 註冊：theme 變更時呼叫 cb(theme_str) 同步視窗標題列深淺。"""
        self._theme_cb = cb

    def persisted_theme(self) -> str:
        """目前持久化的外觀設定（light/dark/system），供視窗層決定初始標題列。"""
        return self._settings_raw().get("appearance", "light") or "light"

    # ── 偏好即時生效（重用 app.py 的全域旗標機制）─────────────────────────
    @staticmethod
    def _vocab_to_flags(vocab: str):
        """webview vocab 字串 → (output_simplified, vocab_convert)。

        off   → 簡體輸出（不經 OpenCC 繁化）
        s2t   → 繁體・標準字形（僅字形轉換，保留原始用詞）
        s2twp → 繁體・台灣用語（含詞彙在地化，預設）
        """
        if vocab == "off":
            return True, True
        if vocab == "s2t":
            return False, False
        return False, True

    @staticmethod
    def _flags_to_vocab(output_simplified: bool, vocab_convert: bool):
        if output_simplified:
            return "off"
        return "s2twp" if vocab_convert else "s2t"

    def _apply_vocab(self, output_simplified: bool, vocab_convert: bool):
        """同步三引擎各自模組的簡繁/詞彙旗標，並即時重建當前引擎的 OpenCC。

        OpenVINO 讀 app(core)._g_*；chatllm/crisp 各讀自己模組級 _output_simplified/
        _vocab_convert。三者都有 rebuild_cc()，故先設旗標、再重建即可免重載生效。
        """
        import importlib
        try:
            core._g_output_simplified = bool(output_simplified)
            core._g_vocab_convert = bool(vocab_convert)
        except Exception:
            pass
        for name in ("chatllm_engine", "crisp_engine"):
            try:
                m = importlib.import_module(name)
                m._output_simplified = bool(output_simplified)
                m._vocab_convert = bool(vocab_convert)
            except Exception:
                pass
        eng = getattr(self, "engine", None)
        if eng is not None and hasattr(eng, "rebuild_cc"):
            try:
                eng.rebuild_cc()
            except Exception:
                pass

    def _apply_mirror(self, base):
        try:
            import downloader as _dl
            _dl.set_mirror((base or "").strip())
        except Exception:
            pass

    def _apply_output_format(self, fmt):
        try:
            import subtitle_lines as _subs
            _subs.OUTPUT_FORMAT = "txt" if str(fmt).lower() == "txt" else "srt"
        except Exception:
            pass

    def _apply_vad(self, value):
        """把 VAD 閾值即時套到 app 模組全域 —— _detect_speech_groups 於呼叫時讀取。"""
        try:
            core.VAD_THRESHOLD = float(value)
        except Exception:
            pass

    def _apply_runtime_prefs(self):
        """啟動時把持久化偏好一次套用到各引擎模組全域（對齊 app._apply_ui_prefs）。

        在 start_load() 之前呼叫 → 模組旗標先就位，引擎載入時即依正確 OpenCC
        設定建構 cc，且 VAD/鏡像/輸出格式皆於首次轉錄前生效。
        """
        s = self._settings_raw()
        try:
            self._apply_vad(s.get("vad_threshold", 0.5))
        except Exception:
            pass
        vc = s.get("vocab_convert", True)
        if isinstance(vc, str):
            simp, conv = self._vocab_to_flags(vc if vc in ("off", "s2twp", "s2t") else "s2twp")
        else:
            simp, conv = bool(s.get("output_simplified", False)), bool(vc)
        self._apply_vocab(simp, conv)
        self._apply_mirror(s.get("hf_mirror", ""))
        self._apply_output_format(s.get("output_format", "srt"))

    # ── LAN 端點服務（重用 api_server.TranscribeServer）─────
    def _endpoint_port(self) -> int:
        """目前要使用的監聽埠：已啟動的服務 > 設定值 > 預設 11435。"""
        if self._server:
            return self._server._port
        try:
            p = int(self._settings_raw().get("endpoint_port", 11435) or 11435)
            return p if 1 <= p <= 65535 else 11435
        except Exception:
            return 11435

    def get_endpoint(self) -> dict:
        from api_server import get_local_ip
        running = bool(self._server and self._server.running)
        host = get_local_ip()
        port = self._endpoint_port()
        key = self._server.token if self._server else ""
        url = f"http://{host}:{port}/?k={key}" if running else ""
        return {"running": running, "host": host, "port": port, "key": key, "url": url}

    def toggle_endpoint(self, on_: bool, port=None) -> dict:
        from api_server import TranscribeServer
        if on_:
            # 解析監聽埠：前端傳入 > 設定 > 預設；變更時持久化並重建服務。
            new_port = None
            if port is not None and str(port).strip():
                try:
                    cand = int(port)
                    if 1 <= cand <= 65535:
                        new_port = cand
                except Exception:
                    new_port = None
            target = new_port if new_port is not None else self._endpoint_port()
            if new_port is not None:
                self._persist_setting("endpoint_port", new_port)
                if self._server and self._server._port != new_port:
                    self._server.stop()                 # 換埠 → 丟棄舊服務重建
                    self._server = None
            if not self._server:
                self._server = TranscribeServer(get_engine=lambda: self.engine, port=target)
            self._server.start()
        elif self._server:
            self._stop_tunnel()              # 端點停 → 外網通道一併停（金鑰/埠失效）
            self._server.stop()
        return self.get_endpoint()

    def _persist_setting(self, key: str, value):
        f = Path(getattr(core, "SETTINGS_FILE", BASE_DIR / "settings.json"))
        try:
            cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        except Exception:
            cur = {}
        cur[key] = value
        try:
            f.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def regen_key(self) -> dict:
        was = bool(self._server and self._server.running)
        self._stop_tunnel()                  # 換金鑰 → 舊外網網址失效，先收掉通道
        if self._server:
            self._server.stop()
        self._server = None
        if was:
            self.toggle_endpoint(True)
        return self.get_endpoint()

    # ── 對外臨時網址（Cloudflare quick tunnel）＋ QR ──────────────────────
    #   通道指向 LAN 端點（api_server）的埠＋金鑰，故須先啟動端點服務。
    #   建立耗時數秒 → 背景執行緒跑，狀態/網址經 SSE "tunnel" 事件推給前端。
    def get_tunnel(self) -> dict:
        t = self._tunnel
        return {
            "running": bool(t and t.running),
            "url": (t.url if t else "") or "",
            "status": (t.status if t else "") or "",
        }

    def toggle_tunnel(self, on_: bool) -> dict:
        import cf_tunnel
        if on_:
            if not (self._server and self._server.running):
                return {"running": False, "url": "", "status": "請先啟動端點服務", "error": True}
            if self._tunnel and self._tunnel.running:
                return self.get_tunnel()
            self._tunnel = cf_tunnel.CloudflareTunnel()
            port, token = self._server._port, self._server.token

            def worker():
                try:
                    self._tunnel.start(
                        port, token,
                        status_cb=lambda m: self._emit("tunnel", {
                            "status": m, "running": bool(self._tunnel and self._tunnel.running),
                            "url": (self._tunnel.url if self._tunnel else "") or "",
                        }))
                except Exception as e:
                    head = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
                    self._emit("tunnel", {"status": _t.msg("progress.tunnelFailed",
                                                            error=head),
                                          "running": False, "url": "", "error": True})

            threading.Thread(target=worker, name="cf-tunnel", daemon=True).start()
            return {"running": True, "url": "",
                    "status": _t.msg("progress.tunnelStarting")}
        self._stop_tunnel()
        return self.get_tunnel()

    def _stop_tunnel(self):
        if self._tunnel:
            try:
                self._tunnel.stop()
            except Exception:
                pass
        self._emit("tunnel", {"status": "", "running": False, "url": ""})

    def make_qr(self, data: str) -> bytes | None:
        """回傳 QR 的 PNG 位元組（供 /api/qr 路由）。"""
        import cf_tunnel
        return cf_tunnel.make_qr_png(data) if data else None

    # ── 批次（桌面實機後續搬入）────────────────────────────
    def get_batch(self) -> dict:
        # Kept for API compatibility; the real queue lives on WebViewServer.
        return {"summary": {"done": 0, "total": 0}, "items": []}

    # ── C1 / C2: optional exports derived from a finished SRT ──────────
    @staticmethod
    def _safe_srt(srt_path) -> Path:
        """Resolve a subtitle path, refusing anything outside the output dir.

        The path comes from the browser (and from the LAN endpoint), so it is
        attacker-controlled. Never trust it for a read or a write.
        """
        base = Path(getattr(core, "SRT_DIR", BASE_DIR / "subtitles")).resolve()
        path = Path(str(srt_path or "")).resolve()
        if path != base and base not in path.parents:
            raise ValueError("字幕路徑不在輸出目錄內")
        if not path.is_file():
            raise FileNotFoundError("找不到字幕檔")
        return path

    def refine_punctuation(self, srt_path: str) -> dict:
        """C1: write a ``*.punct.srt`` copy with cross-segment punctuation."""
        from subtitle_extras import parse_srt, refine_srt_file
        path = self._safe_srt(srt_path)
        dst = refine_srt_file(path)
        return {"ok": True, "path": str(dst),
                "lines": len(parse_srt(path.read_text(encoding="utf-8-sig")))}

    def export_handout(self, srt_path: str, fmt: str = "md") -> dict:
        """C2: write a Markdown / TXT lecture note from the finished SRT."""
        from subtitle_extras import parse_srt, export_handout_file
        path = self._safe_srt(srt_path)
        fmt = "txt" if str(fmt).lower() in ("txt", "text") else "md"
        dst = export_handout_file(path, fmt=fmt)
        return {"ok": True, "path": str(dst), "format": fmt,
                "bytes": dst.stat().st_size,
                "lines": len(parse_srt(path.read_text(encoding="utf-8-sig")))}
