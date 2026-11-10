# QwenASRGUI

繁體中文 · [简体中文](README.md)

> 本機語音辨識轉字幕工具。純 CPU + OpenVINO INT8，不需要 NVIDIA 顯卡，不依賴 PyTorch。
> 本專案是 [`dseditor/QwenASRMiniTool`](https://github.com/dseditor/QwenASRMiniTool) 的 fork。

**先講邊界**（讀完這一段再決定要不要用）：

- 主場景是 **3–5 小時中文網課 / 會議錄音轉字幕**，追求準確率，不追求即時。
- 模型權重**不內嵌**：首次使用要在「模型」頁下載 0.6B（約 1.2 GB）或 1.7B（約 4.4 GB）。
- 1.7B 段長上限 **10 秒**，來自 IR 匯出時固定的 `T=1000`，放寬會靜默丟字，介面會顯示實際生效值。
- 所有回歸測試使用 **mock ASR**。真實 INT8 權重、ForcedAligner、聲紋 ONNX 從未在 CI 中載入。
  Windows 實機的視窗、冷啟動、3–5 小時完整任務**未複驗**。詳見 [驗證邊界](docs/驗證邊界.zh-Hant.md)。

---

## 這是什麼

一條統一的串流管道：邊讀音訊、邊按 VAD 分段、邊增量寫 SRT、邊透過 SSE 推進度。
不再按音訊長短分兩套邏輯，也不再把整段波形讀進記憶體。取消與崩潰都保留已完成字幕，
同一檔案再次開始可從中斷點續跑，結果逐位元組一致。

| 能力 | 狀態 |
|---|---|
| 長音訊串流轉寫 / 中斷點續跑 / 取消保留 | 已實作，mock 回歸覆蓋 |
| 5 分鐘試跑 / 切分預檢（p50/p90/p99） | 已實作 |
| 模型下載中心（多源測速、SHA256、離線清單、匯入驗證） | 已實作 |
| 服務端持久批次佇列（重新整理不丟、失敗隔離、重試） | 已實作 |
| 詞級時間軸 / 說話者分離 | 選用增強，另存 `*.rich.json`，失敗不影響 SRT |
| 跨段標點修正 / 匯出講義 | 另存 `*.punct.srt` / `*.handout.md`，不改原字幕 |
| CTk 桌面版 | 遺留，不再更新；WebView 版是唯一主線 |

完整矩陣見 [功能矩陣](docs/功能矩陣.zh-Hant.md)。

## 快速開始

**發行包（Windows）**：到 [Releases](https://github.com/1-1-2/QwenASRGUI/releases/latest) 下載 ZIP，
解壓整個資料夾，雙擊 `QwenASR-WebView.exe`。無需安裝 Python。

> **Important · SmartScreen**：分層目錄版的啟動器是未簽章的 EXE。首次執行 Windows
> SmartScreen 可能提示「Windows 已保護您的電腦」，點「其他資訊」→「仍要執行」。
> 啟動器內容穩定、更新不再整包替換，隨下載量累積該提示會減少；正式簽章需要程式碼簽章憑證，
> 目前**未簽章**。請勿只拷走 EXE —— `_internal/` 是程式本體，必須與 EXE 一起移動。

**原始碼**：

```bash
python -m pip install -r requirements.txt
python app_webview.py            # 起本機服務 + Edge --app 視窗
python app_webview.py --selftest # 無視窗冷啟動基準（輸出 JSON）
python app_webview.py transcribe 課程.mp4 --no-resume --json
```

## 目錄佈局（onedir 分層）

```text
QwenASR-WebView.exe    啟動器（內容穩定，可累積 SmartScreen 信譽）
_internal/             程式本體、相依、前端資源（隨更新替換）
manifest.json          內容定址清單（SHA256，用於校驗 / 差量 / 回滾）
ov_models/ subtitles/ longform_state/ ffmpeg/   使用者資料（更新時不動）
settings.json          設定（更新時不動）
```

更新只替換前三項；`app.prev/` 保存上一版本以便回滾。詳見 [打包與升級](docs/打包與升級.zh-Hant.md)。

## 文件

| 文件 | 內容 |
|---|---|
| [文件索引](docs/README.zh-Hant.md) | 全部文件的閱讀入口 |
| [架構說明](docs/架構說明.zh-Hant.md) | 視窗策略、打包形態、路徑規則、SSE 契約 |
| [長音訊串流方案](docs/长音频流式方案.md) | 串流 Runner 原始技術說明 |
| [打包與升級](docs/打包與升級.zh-Hant.md) | manifest、差量、回滾、簽章與 SmartScreen |
| [功能矩陣](docs/功能矩陣.zh-Hant.md) | 目前支援 / 遺留未遷移 |
| [驗證邊界](docs/驗證邊界.zh-Hant.md) | 已驗證與未驗證，逐項列證據 |
| [模型下載清單](docs/模型下载清单.md) | 由 `models.json` 產生的產物（不翻譯） |
| [風格規範](docs/風格規範.zh-Hant.md) | 文件與程式碼的語言、語氣約定 |
| [變更記錄](CHANGELOG.md) | 版本歷史 |
| [NOTICE](NOTICE) | 第三方資產與授權 |

## 建置與驗證

```bash
python build_release.py --version 2.0.0 --sha <short-sha>   # 需 PyInstaller + Windows
python release_manifest.py verify --root release_pkg/QwenASR-WebView
python -m compileall -q *.py tests/*.py tools/*.py
for t in tests/test_*.py; do python "$t"; done
python tools/offscreen_render.py --out ./_offscreen_out      # 需 Playwright + Chromium
```

CI 只跑不依賴權重的調度層回歸；發行包由手動觸發的 `release.yml` 在 Windows runner 上建置。

## 回推上游與授權

上游倉庫**沒有 License**。本站是 fork，最終目標是拆成原子化 PR 回推上游。
`NOTICE` 逐項列出第三方資產（OpenVINO Runtime、Silero VAD、dseditor IR、CrispASR 等）的來源與授權狀態；
在上游明確授權之前，這是 PR 回推的硬門檻。

上游原始 README（繁體，未改動）歸檔於 [docs/legacy/README_upstream.md](docs/legacy/README_upstream.md)。
