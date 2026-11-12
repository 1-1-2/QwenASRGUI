/* ============================================================
   i18n.js — 介面語言（繁體中文 / 简体中文 / English）
   用法：在 HTML 元素掛 data-i18n="key"（textContent）、
        data-i18n-html（innerHTML）、data-i18n-ph（placeholder）、
        data-i18n-title（title）。呼叫 window.I18N.setLang(uiLang) 切換。
   字典僅涵蓋靜態介面字串；後端動態訊息走下方 M 字典的 key + vars 契約
   （見 ui_text.py），自由文字（例外、路徑、使用者提示詞）保留原文。
   ============================================================ */
(function () {
  "use strict";

  // B5.2: stable locale enums are the only values persisted / stored in the
  // <select>. Legacy Chinese labels (older settings.json) still map here so
  // existing users are migrated on first load instead of falling back to hant.
  const LANG_MAP = {
    "hant": "hant", "hans": "hans", "en": "en",
    "繁體中文": "hant", "简体中文": "hans", "簡體中文": "hans", "English": "en",
  };
  let LOCALE = "hant";
  function normalize(uiLang) { return LANG_MAP[uiLang] || "hant"; }

  // key: [繁體, 简体, English]
  const D = {
    "nav.file": ["音檔", "音频", "Audio"],
    "nav.batch": ["批次", "批量", "Batch"],
    "nav.record": ["錄製", "录制", "Record"],
    "nav.endpoint": ["端點", "端点", "Endpoint"],
    "nav.model": ["模型", "模型", "Model"],
    "nav.settings": ["設定", "设置", "Settings"],

    "view.file": ["音檔轉字幕", "音频转字幕", "Audio to Subtitles"],
    "view.batch": ["批次辨識", "批量识别", "Batch Transcribe"],
    "view.record": ["錄製轉換", "录制转换", "Record & Transcribe"],
    "view.endpoint": ["端點服務", "端点服务", "Endpoint Service"],
    "view.model": ["模型與裝置", "模型与设备", "Model & Devices"],
    "view.settings": ["設定", "设置", "Settings"],

    "file.dropBig": ["拖曳音訊到此，或<b>點擊選擇檔案</b>", "拖拽音频到此，或<b>点击选择文件</b>", "Drop audio here, or <b>click to choose a file</b>"],
    "file.dropSub": ["支援 mp3 / wav / m4a / 影片音軌", "支持 mp3 / wav / m4a / 视频音轨", "Supports mp3 / wav / m4a / video audio"],
    "file.run": ["開始轉換", "开始转换", "Start"],
    "file.openDir": ["開啟輸出資料夾", "打开输出文件夹", "Open output folder"],
    "file.saveSub": ["字幕存檔", "保存字幕", "Save subtitles"],
    "file.verify": ["字幕驗證", "字幕校验", "Verify"],
    "file.diar": ["說話者分離", "说话者分离", "Speaker diarization"],
    "file.align": ["時間軸對齊", "时间轴对齐", "Timestamp align"],
    "file.hintTitle": ["辨識提示（可選）", "识别提示（可选）", "Recognition hint (optional)"],
    "file.hintDesc": ["貼入歌詞、關鍵字或背景說明，可提升辨識準確度", "粘贴歌词、关键字或背景说明，可提升识别准确度", "Paste lyrics, keywords or context to improve accuracy"],
    "file.hintPh": ["例如：本段為產品行銷 Podcast，會出現「轉換率」「漏斗」等行銷術語…", "例如：本段为产品营销 Podcast，会出现“转化率”“漏斗”等营销术语…", "e.g. This is a marketing podcast with terms like 'conversion rate', 'funnel'…"],
    "file.loadTxt": ["讀入 TXT…", "读入 TXT…", "Load TXT…"],
    "file.resultTitle": ["辨識結果", "识别结果", "Result"],

    // 切分參數面板：所有與「這份音檔怎麼切」有關的設定都集中在識別頁
    "file.segTitle": ["切分參數", "切分参数", "Segmentation"],
    "file.segDesc": ["切得越長，語句越完整。", "切得越长，语句越完整。", "Longer segments keep sentences intact."],
    "file.vad": ["語音偵測靈敏度（VAD）", "语音侦测灵敏度（VAD）", "VAD sensitivity"],
    "file.chunk": ["每段最長秒數", "每段最长秒数", "Max seconds per segment"],
    "file.effective": ["目前生效", "当前生效", "Effective"],
    "file.ramHint": ["記憶體", "内存", "Memory"],
    "file.ramExtra": ["（系統另計 1–2 GB）", "（系统另计 1–2 GB）", "(+1–2 GB system)"],
        "file.previewRun": ["切分預檢", "切分预检", "Check split"],
    "file.previewEmpty": ["尚未預檢", "尚未预检", "Not checked"],
    "file.trial": ["試跑前 5 分鐘", "试跑前 5 分钟", "Trial 5 min"],
    "file.trialBadge": ["試跑前 5 分鐘 · 不會完整轉檔", "试跑前 5 分钟 · 不会完整转档", "TRIAL · first 5 min only"],
    "file.segSummary": ["VAD {vad} · 每段 {chunk}s", "VAD {vad} · 每段 {chunk}s", "VAD {vad} · {chunk}s / segment"],
        "file.previewOk": ["切分合理", "切分合理", "Good split"],
    "file.streamingNote": ["可選增強：字級時間軸、說話人分離；失敗不影響字幕", "可选增强：词级时间轴、说话人分离；失败不影响字幕", "Optional: word timing & speakers. Failures never affect subtitles"],
    "file.enrichMissing": ["模型未安裝，請到模型頁下載", "模型未安装，请到模型页下载", "Model not installed — see the Model page"],
    "file.previewWarn": ["建議調整", "建议调整", "Adjust suggested"],
    "file.previewTruncated": ["只顯示前 %1 條字幕，完整字幕已存檔：", "只显示前 %1 条字幕，完整字幕已存档：", "Showing the first %1 subtitles; full transcript saved to:"],
    "set.segMoved": ["VAD 靈敏度、每段最長秒數、切分預檢與 5 分鐘試跑已移至「音檔」頁，與實際任務放在一起。", "VAD 灵敏度、每段最长秒数、切分预检与 5 分钟试跑已移至「音频」页，与实际任务放在一起。", "VAD sensitivity, segment length, segmentation preview and the 5-minute trial now live on the Audio page, next to the actual task."],
    "common.langAuto": ["語言 自動", "语言 自动", "Lang Auto"],
    "common.spkAuto": ["人數 自動", "人数 自动", "Count Auto"],

    "batch.add": ["加入檔案", "添加文件", "Add files"],
    "batch.dropBig": ["拖曳多個音訊到此，或<b>點擊選取檔案</b>", "拖拽多个音频到此，或<b>点击选取文件</b>", "Drop audio files here, or <b>click to choose files</b>"],
    "batch.dropSub": ["選取的檔案直接引用本機路徑：不複製、不上傳、不產生暫存副本", "选取的文件直接引用本机路径：不复制、不上传、不产生暂存副本", "Files are referenced by local path: no copy, no upload, no temp files"],
    "batch.dropAdded": ["已加入 {n} 個檔案", "已添加 {n} 个文件", "Added {n} files"],
    "batch.run": ["全部開始", "全部开始", "Start all"],
    "welcome.text": ["初次使用：請先在「模型與裝置」頁選擇並下載辨識模型，完成後即可開始轉寫。",
                     "初次使用：请先在「模型与设备」页选择并下载识别模型，完成后即可开始转写。",
                     "First time here: pick and download a recognition model on the Model page, then start transcribing."],
    "welcome.go": ["前往模型頁", "前往模型页", "Go to Model page"],
    "file.refine": ["跨段標點修正", "跨段标点修正", "Refine punctuation"],
    "file.refineHint": ["新字幕在轉錄完成時已自動修正；此按鈕對舊字幕或手改過的字幕另存 *.punct.srt 副本，不覆蓋原檔",
                        "新字幕在转录完成时已自动修正；此按钮对旧字幕或手改过的字幕另存 *.punct.srt 副本，不覆盖原档",
                        "New subtitles are refined automatically when transcription finishes. This button saves a *.punct.srt copy for older or hand-edited files without overwriting the original"],
    "file.refineDone": ["✓ 跨段標點修正完成：{path}（{lines} 行）", "✓ 跨段标点修正完成：{path}（{lines} 行）", "✓ Punctuation refined: {path} ({lines} lines)"],
    "file.refineFailed": ["✕ 跨段標點修正失敗：{error}", "✕ 跨段标点修正失败：{error}", "✕ Punctuation refinement failed: {error}"],
    "file.handoutDone": ["✓ 講義已匯出：{path}（{lines} 行 / {bytes} bytes）", "✓ 讲义已导出：{path}（{lines} 行 / {bytes} bytes）", "✓ Notes exported: {path} ({lines} lines / {bytes} bytes)"],
    "file.handoutFailed": ["✕ 講義匯出失敗：{error}", "✕ 讲义导出失败：{error}", "✕ Notes export failed: {error}"],
    "file.handout": ["匯出講義", "导出讲义", "Export notes"],
    "file.handoutHint": ["由字幕產生 Markdown 講義（依停頓分段）",
                         "由字幕生成 Markdown 讲义（依停顿分段）",
                         "Generate Markdown lecture notes from subtitles, split by pauses"],
    "warn.ssl": ["SSL 憑證驗證已降級：下載的模型檔無法驗證來源，請僅在可信網路環境中使用。",
                 "SSL 证书验证已降级：下载的模型文件无法验证来源，请仅在可信网络环境中使用。",
                 "TLS certificate verification is disabled: downloaded model files cannot be authenticated. Use a trusted network only."],
    "log.title": ["執行紀錄", "运行日志", "Run log"],
    "log.clear": ["清除", "清除", "Clear"],
    "file.running": ["辨識中…", "识别中…", "Transcribing…"],
    "batch.cancel": ["取消佇列", "取消队列", "Cancel queue"],
    "batch.clear": ["清除已完成", "清除已完成", "Clear finished"],
    "batch.resumeHint": ["服務端佇列：重新整理頁面仍會保留進度與斷點",
                         "服务端队列：刷新页面仍会保留进度与断点",
                         "Server-side queue: refresh keeps progress and checkpoints"],
    "batch.sharedOpts": ["語言、提示詞、切分與試跑參數共用「音檔轉字幕」頁的設定",
                         "语言、提示词、切分与试跑参数共用「音频转字幕」页的设置",
                         "Language, hint, segmentation and trial options are shared with the Audio page"],
    "common.close": ["關閉", "关闭", "Close"],

    // Wave 11：本機路徑直引（問題 3）與批次頁獨立進度（問題 8）
    "batch.addPath": ["貼上本機路徑", "粘贴本机路径", "Add local path"],
    "batch.pathPh": ["每行一個本機檔案絕對路徑，例如：\nC:\\DATA\\TMP\\audio\\001.m4a",
                     "每行一个本机文件绝对路径，例如：\nC:\\DATA\\TMP\\audio\\001.m4a",
                     "One absolute local path per line, e.g.:\nC:\\DATA\\TMP\\audio\\001.m4a"],
    "batch.pathAdd": ["加入路徑", "加入路径", "Add paths"],
    "batch.pathHint": ["直接引用本機檔案：不複製、不上傳、不佔用暫存空間；移除任務也不會刪除你的原檔。每行一個路徑，僅支援本機磁碟。",
                       "直接引用本机文件：不复制、不上传、不占用暂存空间；移除任务也不会删除你的原文件。每行一个路径，仅支持本机磁盘。",
                       "References local files directly: no copy, no upload, no temp files; removing a task never deletes your original. One path per line, local drives only."],
    "batch.pathEmpty": ["請先貼上至少一個檔案路徑", "请先粘贴至少一个文件路径", "Paste at least one file path"],
    "batch.pathAdded": ["已加入 {n} 個本機檔案（未複製）", "已加入 {n} 个本机文件（未复制）", "Added {n} local files (no copy)"],
    "batch.pathPartial": ["加入 {ok} 個，{fail} 個失敗：{err}", "加入 {ok} 个，{fail} 个失败：{err}", "Added {ok}, {fail} failed: {err}"],
    "batch.localPath": ["本機路徑 · 不複製", "本机路径 · 不复制", "Local path · no copy"],
    "batch.idle": ["待處理", "待处理", "Idle"],
    "batch.progRunning": ["辨識中 · {name}", "识别中 · {name}", "Transcribing · {name}"],
    "batch.progPaused": ["已暫停 · 尚有 {n} 個待處理", "已暂停 · 还有 {n} 个待处理", "Paused · {n} pending"],
    "batch.progDone": ["全部完成", "全部完成", "All finished"],
    "batch.progEmpty": ["尚未加入檔案", "尚未加入文件", "No files yet"],
    "batch.progCount": ["{done} / {total} 完成", "{done} / {total} 完成", "{done} / {total} done"],
    "batch.progFailed": ["{n} 失敗", "{n} 失败", "{n} failed"],
    "batch.progActive": ["目前：{name} · {status}", "当前：{name} · {status}", "Now: {name} · {status}"],
    "batch.queue": ["佇列", "队列", "Queue"],
    "batch.queueMeta": ["{n} 個檔案 · 總音訊 {audio} · 本檔剩餘 {left}", "{n} 个文件 · 总音频 {audio} · 本档剩余 {left}", "{n} files · total {audio} · current left {left}"],
    "batch.currentFile": ["目前第 {i}/{n} 個 · 正在識別：{name}", "当前第 {i}/{n} 个 · 正在识别：{name}", "File {i}/{n} · transcribing: {name}"],
    "batch.noActive": ["—", "—", "—"],
    "batch.elapsed": ["已耗時 {t}", "已耗时 {t}", "Elapsed {t}"],
    "batch.doneRatio": ["已完成 {done}/{total}", "已完成 {done}/{total}", "{done}/{total} done"],
    "batch.groups": ["已識別 {n} 段", "已识别 {n} 段", "{n} segments done"],
    "batch.pickUnsupported": [
      "此環境無法叫出系統選檔視窗，請改用「貼上本機路徑」",
      "此环境无法叫出系统选档窗口，请改用“粘贴本机路径”",
      "The native file picker is unavailable here — paste a local path instead"],
    "batch.dropNoPath": [
      "已收到 {n} 個檔案，但瀏覽器不會把拖入檔案的真實路徑交給網頁（Chromium 的安全設計，無法繞過）。請改為「點擊此框」用系統選檔，可一次選多個——同樣只引用路徑、不複製；或把路徑貼到下方。",
      "已收到 {n} 个文件，但浏览器不会把拖入文件的真实路径交给网页（Chromium 的安全设计，无法绕过）。请改为「点击此框」用系统选档，可一次选多个——同样只引用路径、不复制；或把路径粘贴到下方。",
      "Received {n} file(s), but the browser never exposes the real path of a dropped file (a Chromium security rule with no workaround). Click this box to pick several files at once via the system dialog — still referenced by path, never copied — or paste a path below."],
    "batch.fileElapsed": ["本檔已耗時 {t}", "本档已耗时 {t}", "File elapsed {t}"],
    "batch.speed": ["速度 {x}×", "速度 {x}×", "Speed {x}×"],
    "batch.fileRemaining": ["預計本檔剩餘 {t}", "预计本档剩余 {t}", "File ETA {t}"],
    "batch.noDetail": ["—", "—", "—"],
    "batch.used": ["已用 {t}", "已用 {t}", "Used {t}"],
    "batch.left": ["剩餘 {t}", "剩余 {t}", "Left {t}"],
    "batch.etaPending": ["預計 —", "预计 —", "ETA —"],
    "batch.menuTitle": ["更多動作", "更多操作", "More actions"],
    "batch.menuOpenOut": ["開啟輸出資料夾", "打开输出文件夹", "Open output folder"],
    "batch.menuRetry": ["重試此檔", "重试此文件", "Retry this file"],
    "batch.menuSkip": ["略過此檔（繼續下一個）", "跳过此文件（继续下一个）", "Skip this file (continue queue)"],
    "batch.menuStop": ["停止佇列", "停止队列", "Stop queue"],
    "batch.menuRemove": ["移除", "移除", "Remove"],
    "batch.menuPromote": ["置頂（下一個處理）", "置顶（下一个处理）", "Move to next"],
    "batch.moveHint": ["可拖曳列調整處理順序", "可拖拽行调整处理顺序", "Drag a row to reorder"],
    "batch.moveOnlyPending": ["只有「等待中」的項目能調整順序", "只有「等待中」的项目能调整顺序", "Only pending items can be reordered"],
    "batch.removeBusy": ["此項目正在執行，已先停止佇列；若仍在，請再點一次移除。", "此项目正在执行，已先停止队列；若仍在，请再点一次移除。", "Item is running; queue was stopped. Click remove again if it remains."],

    "record.mic": ["麥克風", "麦克风", "Microphone"],
    "record.refresh": ["重新整理", "刷新", "Refresh"],
    "record.liveTitle": ["即時字幕", "实时字幕", "Live captions"],
    "file.seekHint": [" · 點波形、字幕或下方區塊可跳播", " · 点波形、字幕或下方区块可跳播", " · Click the waveform, a subtitle or a block below to seek"],
    "live.title": ["即時字幕", "实时字幕", "Live subtitles"],
    "live.desc": ["邊跑邊顯示，最多保留最近 200 條；完整結果仍以 SRT 為準",
                  "边跑边显示，最多保留最近 200 条；完整结果仍以 SRT 为准",
                  "Shown while running; keeps the latest 200. The full result is still in the SRT."],
    "live.follow": ["回到最新", "回到最新", "Jump to latest"],
    "record.liveDesc": ["說完停頓自動辨識；可清除或依輸出格式存檔", "说完停顿自动识别；可清除或按输出格式保存", "Auto-transcribes on pause; clear or save by output format"],
    "record.autosave": ["即時存檔", "实时保存", "Auto-save"],
    "record.clear": ["清除", "清除", "Clear"],
    "record.save": ["儲存字幕", "保存字幕", "Save subtitles"],
    "record.note": ["錄製轉換：偵測到說話停頓時才辨識，句中短暫停頓不會中斷。按麥克風開始，再按一次結束並完成最後一段。", "录制转换：检测到说话停顿时才识别，句中短暂停顿不会中断。按麦克风开始，再按一次结束并完成最后一段。", "Transcribes on speech pauses; brief mid-sentence pauses won't cut. Tap the mic to start, tap again to finish the last segment."],
    "record.placeholder": ["開始錄音後，辨識結果會逐段出現在這裡。", "开始录音后，识别结果会逐段出现在这里。", "After recording starts, results appear here segment by segment."],
    "record.permGranted": ["麥克風已授權", "麦克风已授权", "Microphone allowed"],
    "record.permPrompt": ["按麥克風時將請求授權", "按麦克风时将请求授权", "Will ask permission on record"],
    "record.permDenied": ["麥克風權限被拒。請在視窗網址列的權限圖示中允許麥克風，或於系統設定開放本程式的麥克風存取後再試。", "麦克风权限被拒。请在窗口地址栏的权限图标中允许麦克风，或在系统设置开放本程序的麦克风访问后再试。", "Microphone blocked. Allow it via the permission icon in the address bar, or enable mic access for this app in system settings, then retry."],
    "record.permDeniedShort": ["麥克風被拒", "麦克风被拒", "Mic blocked"],

    "ep.uploadPage": ["上傳網頁（手機掃 QR 或開啟）", "上传网页（手机扫 QR 或打开）", "Upload page (scan the QR on your phone)"],
    "ep.copy": ["複製", "复制", "Copy"],
    "ep.accessKey": ["存取金鑰（等同密碼）", "访问密钥（等同密码）", "Access token (acts as a password)"],
    "ep.reveal": ["顯示", "显示", "Reveal"],
    "ep.reset": ["重設", "重置", "Reset"],
    "ep.tunnelDesc": ["透過 Cloudflare 臨時通道讓外網（非同網段）也能上傳；每次重啟網址會變，首次使用會下載 cloudflared（約 25MB）", "通过 Cloudflare 临时通道让外网（非同网段）也能上传；每次重启网址会变，首次使用会下载 cloudflared（约 25MB）", "Opens a temporary Cloudflare tunnel so devices outside your LAN can upload. The URL changes on every restart; the first use downloads cloudflared (~25 MB)."],
    "ep.tunnelWarn": ["⚠️ 網址含金鑰（等同密碼），任何取得「完整網址」的人都能上傳、消耗你的 CPU/GPU。用完請立即關閉，勿外流網址／QR。", "⚠️ 网址含密钥（等同密码），任何取得「完整网址」的人都能上传、消耗你的 CPU/GPU。用完请立即关闭，勿外泄网址／QR。", "⚠️ The URL contains the token (a password). Anyone who gets the full URL can upload and use your CPU/GPU. Turn it off when done and do not share the URL or QR."],
    "ep.tunnelQrHint": ["外網臨時網址（含金鑰）— 掃描或複製給遠端裝置", "外网临时网址（含密钥）— 扫描或复制给远程设备", "Public temporary URL (with token) — scan or copy it to the remote device"],
    "ep.svcTitle": ["OpenAI 相容轉錄服務", "OpenAI 兼容转录服务", "OpenAI-compatible transcription"],
    "ep.svcDesc": ["讓手機或其他程式透過區網上傳音檔辨識", "让手机或其他程序通过局域网上传音频识别", "Let phones or apps upload audio over LAN"],
    "ep.start": ["啟動服務", "启动服务", "Start service"],
    "ep.connInfo": ["連線資訊", "连接信息", "Connection"],
    "ep.cfTitle": ["對外臨時網址（Cloudflare）", "对外临时网址（Cloudflare）", "Public temporary URL (Cloudflare)"],
    "ep.cfMake": ["建立對外網址", "创建对外网址", "Create public URL"],
    "ep.cfOut": ["對外", "对外", "Public"],
    "ep.settings": ["設定", "设置", "Settings"],
    "ep.port": ["監聽埠", "监听端口", "Listen port"],
    "ep.path": ["端點路徑", "端点路径", "Endpoint path"],
    "ep.pathDesc": ["OpenAI Whisper 相容，可直接替換 base URL 使用", "OpenAI Whisper 兼容，可直接替换 base URL 使用", "OpenAI Whisper-compatible; swap your base URL directly"],
    "ep.tunnelReady": ["✅ 已建立對外網址（含金鑰，等同密碼）— 用完請關閉", "✅ 已创建对外网址（含密钥，等同密码）— 用完请关闭", "✅ Public URL ready (the token is a password) — close it when done"],

    "model.health": ["系統自檢", "系统自检", "System check"],
    "model.recheck": ["重新檢查", "重新检查", "Re-check"],
    "model.pick": ["選擇模型", "选择模型", "Choose a model"],
    "model.modeBasic": ["基礎", "基础", "Basic"],
    "model.modeAdv": ["進階", "进阶", "Advanced"],
    "model.core": ["推理核心", "推理核心", "Inference core"],
    "model.model": ["模型", "模型", "Model"],
    "model.load": ["下載並載入模型", "下载并载入模型", "Download & load model"],
    "model.releaseBtn": ["釋放模型記憶體", "释放模型内存", "Release model memory"],
    "model.autoRelease": ["轉檔後自動釋放", "转档后自动释放", "Auto-release after run"],
    "model.releaseDone": ["模型記憶體已釋放，下次轉檔會自動重新載入。", "模型内存已释放，下次转档会自动重新载入。", "Model memory released; it will reload automatically on the next run."],
    "model.releaseBusy": ["正在轉檔或載入中，稍後再試。", "正在转档或载入中，稍后再试。", "Busy transcribing or loading; try again shortly."],
    "model.loading": ["下載／載入中…", "下载／载入中…", "Downloading / loading…"],
    "model.goto": ["前往語音轉文字", "前往语音转文字", "Go to transcription"],
    "model.needRestart": ["切換核心需重新啟動", "切换核心需重启", "Switching core needs restart"],
    "model.nModels": ["{n} 種模型可選", "{n} 种模型可选", "{n} models"],
    "model.devices": ["偵測到的裝置", "检测到的设备", "Detected devices"],
    // Sidebar is only ~100px wide: keep these short enough for one line.
    "status.ready": ["模型已就緒", "模型已就绪", "Ready"],
    "status.loading": ["載入中…", "加载中…", "Loading…"],
    "status.needModel": ["未載入模型", "未加载模型", "No model"],
    "status.appName": ["聲音辨識小工具", "声音识别小工具", "Speech to Text"],
    "model.accel": ["推理加速版本", "推理加速版本", "Acceleration build"],
    "model.diag": ["診斷", "诊断", "Diagnostics"],
    "model.restartPending": ["已記住「{core} · {model}」（{arch}），將於重新啟動後套用（目前以 {active} 運行）。", "已记住「{core} · {model}」（{arch}），将在重启后生效（当前以 {active} 运行）。", "Saved {core} · {model} ({arch}); it will apply after a restart (currently running on {active})."],
    "model.hwSummary": ["{hardware}　加速方式：{accel}", "{hardware}　加速方式：{accel}", "{hardware} · Acceleration: {accel}"],
    "model.readySwitched": ["模型已就緒。已為你切換到「語音轉文字」。", "模型已就绪。已为你切换到「音频转字幕」。", "Model ready. Switched to Audio to Subtitles."],
    "model.loadFailed": ["載入失敗：{error}", "加载失败：{error}", "Failed to load: {error}"],
    "model.applyFailed": ["套用失敗：{error}", "应用失败：{error}", "Could not apply: {error}"],
    "model.loadingHint": ["正在下載並載入模型，請稍候——可留在本頁觀看進度。", "正在下载并载入模型，请稍候——可留在本页查看进度。", "Downloading and loading the model. You can stay on this page to watch the progress."],
    "accel.needsDownload": ["目前選定的核心尚未下載或版本不符（最新 {latest}），會在下次載入模型時自動取得。", "目前选定的核心尚未下载或版本不符（最新 {latest}），会在下次载入模型时自动获取。", "The selected engine is not downloaded or is out of date (latest {latest}); it will be fetched automatically the next time a model loads."],
    "accel.tagRecommended": ["建議", "建议", "Recommended"],
    "accel.tagInstalled": ["已安裝", "已安装", "Installed"],
    "accel.sizeMb": ["約 {size} MB", "约 {size} MB", "~{size} MB"],

    "set.scale": ["介面縮放", "界面缩放", "UI scale"],
    "set.scaleDesc": ["調整文字與控制項大小", "调整文字与控件大小", "Adjust text and control size"],
    "set.format": ["輸出格式", "输出格式", "Output format"],
    "set.formatDesc": ["全域預設，影響單檔／批次／端點", "全局默认，影响单文件／批量／端点", "Global default for single / batch / endpoint"],
    "set.fmtSrt": ["SRT 字幕", "SRT 字幕", "SRT subtitles"],
    "set.fmtTxt": ["純文字", "纯文本", "Plain text"],
    "set.vad": ["語音偵測靈敏度（VAD）", "语音检测灵敏度（VAD）", "Voice detection (VAD)"],
    "set.vadDesc": ["降低閾值可減少漏識（被當成空白的片段可能有聲音）；提高則減少假陽性。預設 0.50", "降低阈值可减少漏识（被当成空白的片段可能有声音）；提高则减少假阳性。默认 0.50", "Lower threshold reduces misses; higher reduces false positives. Default 0.50"],
    "set.chunk": ["每段最長秒數（字級對齊）", "每段最长秒数（字级对齐）", "Max chunk seconds (alignment)"],
    "set.chunkDesc": ["調短可減少長段歌詞／語音「段尾字級逐漸跑掉、換段才歸位」的漂移。實際上限依模型自動夾低（0.6B 30s、1.7B 10s）；CrispASR 走自身視窗，不受此設定影響。", "调短可减少长段歌词／语音「段尾字级逐渐跑掉、换段才归位」的漂移。实际上限依模型自动夹低（0.6B 30s、1.7B 10s）；CrispASR 走自身窗口，不受此设定影响。", "Shorter chunks reduce word-timing drift toward the end of long sung/spoken segments. The cap is auto-limited per model (0.6B 30s, 1.7B 10s); CrispASR uses its own window and is unaffected."],
    "set.vocab": ["簡繁詞彙轉換", "简繁词汇转换", "Chinese variant"],
    "set.vocabDesc": ["把模型輸出轉成在地用語", "把模型输出转成本地用语", "Localize model output wording"],
    "set.vocabOff": ["關閉", "关闭", "Off"],
    "set.vocabTW": ["台灣用語", "台湾用语", "Taiwan"],
    "set.vocabStd": ["標準", "标准", "Standard"],
    "set.mirror": ["HuggingFace 鏡像站", "HuggingFace 镜像站", "HuggingFace mirror"],
    "set.mirrorDesc": ["下載模型較慢時可改用鏡像", "下载模型较慢时可改用镜像", "Use a mirror if downloads are slow"],
    "set.ffmpeg": ["FFmpeg 路徑", "FFmpeg 路径", "FFmpeg path"],
    "set.ffmpegDesc": ["處理影片音軌所需（留空則需要時自動下載）", "处理视频音轨所需（留空则需要时自动下载）", "Needed for video audio (auto-downloads if blank)"],
    "set.theme": ["外觀主題", "外观主题", "Appearance"],
    "set.themeLight": ["淺色", "浅色", "Light"],
    "set.themeDark": ["深色", "深色", "Dark"],
    "set.themeSystem": ["跟隨系統", "跟随系统", "System"],
    "set.lang": ["介面語言", "界面语言", "Language"],
    "set.appDesc": ["本地推理 · 資料不離開你的電腦", "本地推理 · 数据不离开你的电脑", "Local inference · data stays on your PC"],
    "set.checkUpdate": ["檢查更新", "检查更新", "Check update"],

    // ── Wave 7: runtime strings that used to be hard-coded in app.js ──────
    // Every one of these was concatenated in Traditional Chinese directly, so
    // a Simplified or English UI still showed Hant text as soon as an action
    // produced a message (download centre, batch queue, endpoint page…).
    "model.detectingHw": ["偵測硬體中…", "检测硬件中…", "Detecting hardware…"],
    "model.gpuDiag": ["GPU 偵測由 <code style=\"font-family:var(--font-mono)\">crispasr.exe --diagnostics</code> 進行；若未列出獨立顯示卡，請更新顯卡 / Vulkan 驅動後重試。", "GPU 检测由 <code style=\"font-family:var(--font-mono)\">crispasr.exe --diagnostics</code> 进行；若未列出独立显卡，请更新显卡 / Vulkan 驱动后重试。", "GPU detection runs <code style=\"font-family:var(--font-mono)\">crispasr.exe --diagnostics</code>. If no discrete GPU is listed, update your graphics / Vulkan driver and retry."],
    "dl.center": ["下載中心", "下载中心", "Download center"],
    "dl.honesty": ["檔案大小與 SHA256 若顯示「未知」，代表尚未對遠端檔案實測驗證，不會用估計值冒充。括號中的約略值僅供參考，不用於進度計算。", "文件大小与 SHA256 若显示「未知」，代表尚未对远程文件实测验证，不会用估计值冒充。括号中的约略值仅供参考，不用于进度计算。", "When size or SHA256 says \"unknown\", it has not been measured against the remote file — estimates are never passed off as fact. Approximate values are for reference only and are not used for progress."],
    "dl.refresh": ["重新整理", "重新整理", "Refresh"],
    "dl.manifest": ["輸出下載清單 (.txt)", "导出下载清单 (.txt)", "Export download list (.txt)"],
    "dl.import": ["匯入並驗證", "导入并校验", "Import & verify"],
    "dl.mirror": ["目前鏡像：{name}", "当前镜像：{name}", "Mirror: {name}"],
    "dl.mirrorOfficial": ["目前鏡像：官方（未設定）", "当前镜像：官方（未设置）", "Mirror: official (not set)"],
    "dl.summary": ["{mirror} · 已完整 {done}/{total} 組 · 待補 {missing} 組 · 本機已佔用 {used} · 根目錄 {root}",
                   "{mirror} · 已完整 {done}/{total} 组 · 待补 {missing} 组 · 本机已占用 {used} · 根目录 {root}",
                   "{mirror} · {done}/{total} complete · {missing} to go · {used} on disk · root {root}"],
    "dl.badgeComplete": ["完整", "完整", "Complete"],
    "dl.badgeMissing": ["缺 {missing}/{total}", "缺 {missing}/{total}", "{missing}/{total} missing"],
    "dl.estimated": ["約 {size}（估計）", "约 {size}（估计）", "~{size} (est.)"],
    "dl.sizeUnknown": ["大小未知", "大小未知", "Size unknown"],
    "dl.shaOk": ["SHA256 ✓", "SHA256 ✓", "SHA256 ✓"],
    "dl.shaBad": ["SHA256 ✗", "SHA256 ✗", "SHA256 ✗"],
    "dl.shaUnverified": ["SHA256 待驗證", "SHA256 待验证", "SHA256 unverified"],
    "dl.shaUnknown": ["SHA256 未知", "SHA256 未知", "SHA256 unknown"],
    "dl.missing": ["缺失", "缺失", "missing"],
    "dl.target": ["目標：{dir} · 來源：{provider}{optional}", "目标：{dir} · 来源：{provider}{optional}",
                  "Target: {dir} · Source: {provider}{optional}"],
    "dl.optional": [" · 選用", " · 选用", " · optional"],
    "dl.empty": ["無資料", "无数据", "No data"],
    "dl.readFailed": ["讀取失敗：{err}", "读取失败：{err}", "Failed to load: {err}"],
    "dl.manifestWritten": ["已輸出：{path}（{size}，{count} 組）", "已输出：{path}（{size}，{count} 组）",
                           "Written: {path} ({size}, {count} bundles)"],
    "dl.manifestFailed": ["輸出失敗：{err}", "输出失败：{err}", "Export failed: {err}"],
    "dl.needDir": ["請先填入手動下載的資料夾路徑", "请先填入手动下载的文件夹路径",
                   "Enter the folder that holds the manual download first"],
    "dl.imported": ["匯入完成：{msg}（複製 {n} 檔）", "导入完成：{msg}（复制 {n} 个文件）",
                    "Imported: {msg} ({n} files copied)"],
    "dl.importRejected": ["匯入未通過：{msg}", "导入未通过：{msg}", "Import rejected: {msg}"],
    "dl.importFailed": ["匯入失敗：{err}", "导入失败：{err}", "Import failed: {err}"],
    "set.outputDir": ["輸出資料夾", "输出文件夹", "Output folder"],
    "set.outputDirDesc": ["字幕存到這裡；同名的 .rich.json 是「詞級時間軸＋說話者」明細（卡拉OK 同步與說話者標籤用），不是第二份字幕，刪掉不影響 .srt", "字幕存到这里；同名的 .rich.json 是“词级时间轴＋说话人”明细（卡拉OK 同步与说话人标签用），不是第二份字幕，删掉不影响 .srt", "Subtitles land here. The same-named .rich.json holds word timings and speaker labels (for karaoke sync and speaker tags) — it is not a second subtitle file and can be deleted without harming the .srt."],
    "dl.selfCheck": ["同「系統自檢」判斷", "同“系统自检”判断", "Same verdict as System check"],
    "dl.gotoSelfCheck": ["查看系統自檢", "查看系统自检", "Open system check"],
    "dl.importDirHint": ["用途：從別台電腦／瀏覽器手動下載好模型檔後，把「裝著這些檔案的資料夾」貼進來，按「匯入並驗證」即會核對大小與 SHA256 並複製到正確位置；不會重新下載。", "用途：从别的电脑／浏览器手动下载好模型文件后，把“装着这些文件的文件夹”贴进来，按“导入并校验”会核对大小与 SHA256 并复制到正确位置；不会重新下载。", "Purpose: after downloading model files manually (another machine or a browser), paste the folder that contains them and click Import & verify — sizes and SHA256 are checked, files are copied into place, nothing is re-downloaded."],

    "batch.runAll": ["全部開始", "全部开始", "Start all"],
    "batch.stDone": ["完成", "完成", "Done"],
    "batch.stRunning": ["辨識中", "识别中", "Transcribing"],
    "batch.stPending": ["待處理", "待处理", "Pending"],
    "batch.stFailed": ["失敗", "失败", "Failed"],
    "batch.stCanceled": ["已取消", "已取消", "Canceled"],
    "batch.stUploading": ["上傳中", "上传中", "Uploading"],
    "batch.cancelAll": ["取消佇列", "取消队列", "Cancel queue"],
    "batch.remove": ["移除", "移除", "Remove"],
    "batch.openOut": ["開啟輸出資料夾", "打开输出文件夹", "Open output folder"],
    "batch.retry": ["重試", "重试", "Retry"],
    "batch.seconds": ["{n} 秒", "{n} 秒", "{n}s"],
    "batch.uploading": ["上傳中 {i} / {n}…", "上传中 {i} / {n}…", "Uploading {i} / {n}…"],
    "batch.uploadFailed": ["上傳失敗：{err}", "上传失败：{err}", "Upload failed: {err}"],
    "batch.progress": ["{done} / {total} 完成 · 總進度 {pct}%", "{done} / {total} 完成 · 总进度 {pct}%",
                       "{done} / {total} done · {pct}% overall"],
    "batch.failedSuffix": [" · {n} 失敗", " · {n} 失败", " · {n} failed"],
    "batch.runningSuffix": [" · 執行中", " · 运行中", " · running"],
    "batch.empty": ["尚未加入檔案 — 點「加入檔案」可一次選多個音訊，由服務端依序處理。",
                    "尚未加入文件 — 点「加入文件」可一次选多个音频，由服务端依序处理。",
                    "No files yet. Click Add files to pick several audio files; the server processes them in order."],

    "file.dropBigHtml": ["拖曳音訊到此，或<b>點擊選擇檔案</b>", "拖拽音频到此，或<b>点击选择文件</b>",
                         "Drop audio here, or <b>click to choose a file</b>"],
    "file.dropSubHtml": ["支援 mp3 / wav / m4a / 影片音軌", "支持 mp3 / wav / m4a / 视频音轨",
                         "Supports mp3 / wav / m4a / video audio"],

    "run.preparing": ["準備中…", "准备中…", "Preparing…"],
    "run.preparingShort": ["準備中", "准备中", "Preparing"],
    "run.running": ["識別中", "识别中", "Transcribing"],
    "run.paused": ["已暫停", "已暂停", "Paused"],
    "run.done": ["完成", "完成", "Done"],
    "run.started": ["▶ 開始轉換：{name}", "▶ 开始转换：{name}", "▶ Started: {name}"],
    "run.finished": ["✓ 完成，共 {n} 段{srt}", "✓ 完成，共 {n} 段{srt}", "✓ Done, {n} segments{srt}"],
    "run.finishedSrt": ["，已輸出 {path}", "，已输出 {path}", ", written to {path}"],
    "run.finishedLog": ["✓ 完成", "✓ 完成", "✓ Done"],
    "run.trialNote": ["試跑模式：僅處理前 5 分鐘，字幕另存為 .trial.srt",
                      "试跑模式：仅处理前 5 分钟，字幕另存为 .trial.srt",
                      "Trial run: first 5 minutes only, saved as .trial.srt"],
    "run.processed": ["已處理 {done} / {total}", "已处理 {done} / {total}", "Processed {done} / {total}"],
    "run.groups": ["{n} 段", "{n} 段", "{n} segments"],
    "run.remaining": ["剩餘 {eta}", "剩余 {eta}", "{eta} left"],
    "run.remainingUnknown": ["剩餘 —", "剩余 —", "… left"],
    "run.modelCeiling": ["（模型上限 {n}s）", "（模型上限 {n}s）", "(model ceiling {n}s)"],
    "run.speaker": ["說話者 {n}", "说话人 {n}", "Speaker {n}"],
    "run.speakerPrefix": ["說話者{n}：", "说话人{n}：", "Speaker {n}: "],
    "run.editLine": ["編輯此行", "编辑此行", "Edit this line"],
    "run.savedSubtitle": ["✓ 已存檔字幕：{name}", "✓ 已保存字幕：{name}", "✓ Subtitles saved: {name}"],
    "run.defaultSubtitleName": ["字幕", "字幕", "subtitles"],

    "catalog.itemsToFix": ["{n} 項需處理", "{n} 项需处理", "{n} to fix"],
    "catalog.coreReady": ["核心就緒 · {n} 項將於啟用時自動下載", "核心就绪 · {n} 项将在启用时自动下载",
                          "Engine ready · {n} will download when enabled"],
    "catalog.allReady": ["全部就緒", "全部就绪", "All ready"],
    "catalog.shared": ["共用元件", "共用组件", "Shared components"],
    "catalog.inUse": ["使用中", "使用中", "In use"],
    "catalog.downloaded": ["已下載", "已下载", "Downloaded"],
    "catalog.needsDownload": ["需下載", "需下载", "Needs download"],
    "catalog.suggested": ["建議模型：{model}", "建议模型：{model}", "Suggested model: {model}"],
    "catalog.prepareLoad": ["準備載入…", "准备加载…", "Preparing to load…"],
    "catalog.checking": ["檢查中…", "检查中…", "Checking…"],

    "ep.notRunning": ["（服務未啟動）", "（服务未启动）", "Service not running"],
    "ep.qrHint": ["同網段裝置可掃此 QR 直接開啟上傳頁", "同网段设备可扫此 QR 直接打开上传页",
                  "Devices on the same network can scan this QR to open the upload page"],
    "ep.public": ["對外中", "对外中", "Public"],
    "ep.starting": ["建立中…", "创建中…", "Starting…"],
    "ep.inactive": ["未啟用", "未启用", "Disabled"],
    "ep.running": ["執行中", "运行中", "Running"],
    "ep.stopped": ["已停止", "已停止", "Stopped"],
    "ep.startFirst": ["請先啟動端點服務", "请先启动端点服务", "Start the endpoint service first"],
    "ep.show": ["顯示", "显示", "Show"],
    "ep.hide": ["隱藏", "隐藏", "Hide"],
    "ep.copied": ["已複製", "已复制", "Copied"],

    "split.prechecking": ["預檢中…", "预检中…", "Checking…"],
    "split.precheckOk": ["✓ 切分預檢完成：{n} 組", "✓ 切分预检完成：{n} 组", "✓ Split check done: {n} groups"],
    "split.precheckFailed": ["✕ 切分預檢失敗：{err}", "✕ 切分预检失败：{err}", "✕ Split check failed: {err}"],
    "split.stats": ["{groups} 組 · 最長 {max}s", "{groups} 组 · 最长 {max}s", "{groups} groups · longest {max}s"],
    "split.overLimit": ["超上限 {pct}%", "超上限 {pct}%", "{pct}% over limit"],
    "split.veryShort": ["極短段 {pct}%", "极短段 {pct}%", "{pct}% very short"],
    "split.silence": ["靜音 {pct}%", "静音 {pct}%", "{pct}% silence"],
    "split.asrCalls": ["預計 {n} 次 ASR/小時", "预计 {n} 次 ASR/小时", "~{n} ASR calls/hour"],
    "common.unknown": ["未知", "未知", "Unknown"],
    "set.opening": ["開啟中…", "正在打开…", "Opening…"],
    "set.ffmpegPh": ["（留空＝自動偵測／需要時下載）", "（留空＝自动检测／需要时下载）",
                     "(blank = auto-detect)"],
    "set.ffDetected": ["已偵測", "已检测", "Detected"],
    "dl.importDirPh": ["手動下載的資料夾路徑", "手动下载的文件夹路径",
                       "Path to the manual download folder"],

    // Recording page (browser MediaRecorder path) -----------------------
    "record.unsupported": ["不支援錄音", "不支持录音", "Recording unsupported"],
    "record.defaultMic": ["預設麥克風", "默认麦克风", "Default microphone"],
    "record.micN": ["麥克風 {n}", "麦克风 {n}", "Microphone {n}"],
    "record.noApi": ["此環境不支援錄音 API。", "此环境不支持录音 API。",
                     "This environment does not expose the recording API."],
    "record.micDenied": ["麥克風權限被拒。", "麦克风权限被拒绝。", "Microphone permission denied."],
    "record.micFailed": ["無法取得麥克風：", "无法获取麦克风：", "Cannot access the microphone: "],
    "record.listening": ["聆聽中…說完停頓約 2 秒會自動辨識；再按一次結束並完成最後一段。",
                         "聆听中…说完停顿约 2 秒会自动识别；再按一次结束并完成最后一段。",
                         "Listening… a ~2 s pause ends the segment. Press again to finish."],
    "record.asrFailed": ["辨識失敗：{err}", "识别失败：{err}", "Transcription failed: {err}"],
    "record.stopped": ["已停止。", "已停止。", "Stopped."],
    "record.autosaveOn": ["即時存檔已啟用：每段辨識完成後會自動寫入所選檔案。",
                          "实时保存已启用：每段识别完成后会自动写入所选文件。",
                          "Live saving is on: each finished segment is written to the chosen file."],
    "record.subtitleFile": ["字幕檔", "字幕文件", "Subtitle file"],
    "record.autosaveFailed": ["即時存檔寫入失敗：{err}", "实时保存写入失败：{err}",
                              "Live save failed: {err}"],
  };

  function idx() { return LOCALE === "hans" ? 1 : (LOCALE === "en" ? 2 : 0); }
  function t(key, vars) {
    const e = D[key];
    if (!e) return null;
    let out = e[idx()] || e[0];
    if (vars) for (const k in vars) out = out.split("{" + k + "}").join(String(vars[k]));
    return out;
  }

  // ── H6/H7: backend messages (structured key + vars) ───────────────────
  // The backend no longer sends prose. It sends {k, v, t} (see ui_text.py):
  //   k = stable message key, v = variables, t = Hant fallback / log text.
  // Every key below has three *hand written* strings. That is the whole point:
  // Simplified Chinese is not Traditional Chinese with a character table -
  // wording differs ("記憶體"/"内存", "預設"/"默认", "透過"/"通过"), and a
  // parameterised sentence needs the variable in a different position in
  // English. Free-form text (exceptions, paths, user prompts, run logs) cannot
  // be enumerated, so it goes through the fallback() path at the bottom.
  // `tests/test_i18n_contract.py` keeps M and ui_text.TEMPLATES in sync and
  // checks every {placeholder} on both sides.
  const M = {
    "profile.zh.title": ["中文（標準）", "中文（标准）", "Chinese (standard)"],
    "profile.zh.desc": ["一般用途首選：中文為主、斷句與標點最完整，長影片／會議／訪談都適用。", "一般用途首选：以中文为主，断句和标点最完整，长视频／会议／访谈都适用。", "Best general-purpose choice: Chinese-first, with the most complete sentence breaks and punctuation. Works for long videos, meetings and interviews."],
    "profile.tea.title": ["繁體中文 TEA（乾淨聲音）", "繁体中文 TEA（干净人声）", "Traditional Chinese TEA (clean speech)"],
    "profile.tea.desc": ["台灣國語特化：原生輸出繁體與台灣用語，中英夾雜也保得住。限乾淨人聲（訪談、質詢、講座）。", "台湾腔国语特化：原生输出繁体和台湾用语，中英混说也能保住。仅限干净人声（访谈、质询、讲座）。", "Tuned for Taiwan Mandarin: emits Traditional Chinese and local wording natively, and handles mixed Chinese/English. Clean speech only (interviews, Q&A, lectures)."],
    "profile.ja.title": ["日本語", "日语", "Japanese"],
    "profile.ja.desc": ["日語／動漫特化：日文歌詞與台詞辨識明顯較佳，且輸出保留日文原生漢字（不會被誤轉成繁體字形）。", "日语／动漫特化：日文歌词和台词识别明显更好，输出保留日文原生汉字（不会被误转成繁体字形）。", "Tuned for Japanese and anime: markedly better on lyrics and dialogue, and keeps native Japanese kanji instead of converting them to Chinese forms."],
    "profile.twmix.title": ["國台語混合（高速）", "国台语混合（高速）", "Mandarin + Taiwanese (fast)"],
    "profile.twmix.desc": ["Whisper Breeze-ASR-26：繁中／台語特化且速度極快，適合大量批次。模型不輸出標點，字幕改以長度切行（斷句較平）。", "Whisper Breeze-ASR-26：繁中／台语特化且速度极快，适合大批量任务。模型不输出标点，字幕按长度切行（断句较平）。", "Whisper Breeze-ASR-26: tuned for Traditional Chinese and Taiwanese, and very fast - ideal for large batches. Emits no punctuation, so subtitles are split by length (flatter phrasing)."],
    "profile.note.tea": ["⚠️ TEA-ASR 為「台灣國語日常人聲」特化模型：乾淨人聲最準且原生輸出繁體，但背景配樂較重的段落或歌曲可能整段無輸出。音樂類素材請改用 Qwen3-ASR-1.7B（通用）或 Whisper Breeze。", "⚠️ TEA-ASR 是「台湾腔国语日常人声」特化模型：干净人声最准且原生输出繁体，但配乐较重的段落或歌曲可能整段无输出。音乐类素材请改用 Qwen3-ASR-1.7B（通用）或 Whisper Breeze。", "⚠️ TEA-ASR is specialised for everyday Taiwan Mandarin speech: most accurate on clean speech and natively outputs Traditional Chinese, but music-heavy passages or songs may produce no output at all. For music use Qwen3-ASR-1.7B (general) or Whisper Breeze instead."],
    "profile.note.cpuSlow": ["此用途沒有純 CPU 的專用模型，將以 CrispASR 在處理器上執行，速度會明顯較慢；若可接受請直接下載，或改選「中文（標準）」。", "此用途没有纯 CPU 专用模型，会用 CrispASR 在处理器上运行，速度明显较慢；可接受就直接下载，或改选「中文（标准）」。", "There is no CPU-only model for this use case: CrispASR will run on the processor and be noticeably slower. Download anyway if that is fine, or switch to Chinese (standard)."],
    "profile.note.chatllm": ["⚠️ chatllm 核心在部分 AMD 內顯／APU 有已知相容問題，且核心二進位未隨安裝包提供（保留供既有使用者向下相容）；若遇當機或無輸出，建議改用「Qwen · CRISPASR/Vulkan」核心。", "⚠️ chatllm 核心在部分 AMD 核显／APU 上有已知兼容问题，且核心二进制未随安装包提供（保留供既有用户向下兼容）；若遇崩溃或无输出，建议改用「Qwen · CRISPASR/Vulkan」核心。", "⚠️ The chatllm engine has known compatibility issues on some AMD integrated GPUs / APUs, and its binaries are not bundled (kept only for existing installs). If it crashes or produces no output, switch to the Qwen · CRISPASR/Vulkan engine."],

    "state.downloaded": ["已下載", "已下载", "Downloaded"],
    "state.notDownloaded": ["未下載（啟用時自動下載）", "未下载（启用时自动下载）", "Not downloaded (auto-downloads when enabled)"],
    "state.builtin": ["已內建", "已内置", "Built in"],
    "state.vadMissing": ["缺 VAD onnx", "缺少 VAD onnx", "VAD onnx missing"],
    "state.ready": ["已就緒", "已就绪", "Ready"],
    "state.notProvided": ["未提供（核心未隨安裝包附帶，僅供既有使用者）", "未提供（核心未随安装包附带，仅供既有用户）", "Not supplied (the engine is not bundled; existing installs only)"],
    "state.notDownloadedDiar": ["未下載（約 32MB，啟用分離時下載）", "未下载（约 32MB，启用分离时下载）", "Not downloaded (~32MB; downloads when diarization is enabled)"],
    "state.notDownloadedFa": ["未下載（約 939MB，啟用對齊時下載）", "未下载（约 939MB，启用对齐时下载）", "Not downloaded (~939MB; downloads when alignment is enabled)"],

    "health.group.ov": ["Qwen · OpenVINO（CPU）", "Qwen · OpenVINO（CPU）", "Qwen · OpenVINO (CPU)"],
    "health.group.crisp": ["CRISPASR（{variant}：Whisper/Breeze + Qwen + TEA）", "CRISPASR（{variant}：Whisper/Breeze + Qwen + TEA）", "CRISPASR ({variant}: Whisper/Breeze + Qwen + TEA)"],
    "health.group.chatllm": ["Qwen · chatllm（Vulkan · 相容）", "Qwen · chatllm（Vulkan · 兼容）", "Qwen · chatllm (Vulkan · compatibility)"],
    "health.ov.model": ["ASR 模型（{size}）", "ASR 模型（{size}）", "ASR model ({size})"],
    "health.ov.vad": ["語音分段 VAD（silero）", "语音分段 VAD（silero）", "Voice segmentation VAD (silero)"],
    "health.ov.fa": ["時間軸對齊 FA（ForcedAligner .bin）", "时间轴对齐 FA（ForcedAligner .bin）", "Timestamp alignment FA (ForcedAligner .bin)"],
    "health.ov.diar": ["說話者分離（外部 ONNX）", "说话人分离（外部 ONNX）", "Speaker diarization (external ONNX)"],
    "health.crisp.core": ["CrispASR {version} 核心（{variant}）", "CrispASR {version} 核心（{variant}）", "CrispASR {version} engine ({variant})"],
    "health.crisp.coreMissing": ["未下載或版本不符（約 {size}MB，啟用時下載）", "未下载或版本不符（约 {size}MB，启用时下载）", "Not downloaded or version mismatch (~{size}MB; downloads when enabled)"],
    "health.crisp.coreNotNeeded": ["目前使用 OpenVINO（CPU），此核心不需要；改用 GPU 核心時會自動下載", "当前使用 OpenVINO（CPU），此核心不需要；改用 GPU 核心时会自动下载", "Using OpenVINO (CPU) — this engine is not needed; it downloads automatically if you switch to a GPU core"],
    "health.crisp.coreVariantMismatch": ["版本相符但加速版本不同，啟用時會自動換成目前選擇的版本", "版本相符但加速版本不同，启用时会自动换成当前选择的版本", "Version matches but the accelerator build differs; it will be swapped when enabled"],
    "health.crisp.breeze": ["Whisper/Breeze 模型（{quant}）", "Whisper/Breeze 模型（{quant}）", "Whisper/Breeze model ({quant})"],
    "health.crisp.qwen": ["Qwen3-ASR-1.7B 模型（{quant}）", "Qwen3-ASR-1.7B 模型（{quant}）", "Qwen3-ASR-1.7B model ({quant})"],
    "health.crisp.qwenJa": ["Qwen3-ASR-1.7B 日語動漫模型（{quant}）", "Qwen3-ASR-1.7B 日语动漫模型（{quant}）", "Qwen3-ASR-1.7B Japanese/anime model ({quant})"],
    "health.crisp.qwenJaMissing": ["未下載（日語增強，啟用時下載）", "未下载（日语增强，启用时下载）", "Not downloaded (Japanese enhancement; downloads when enabled)"],
    "health.crisp.tea": ["TEA-ASR-1.1 台灣國語模型（Q8）", "TEA-ASR-1.1 台湾国语模型（Q8）", "TEA-ASR-1.1 Taiwan Mandarin model (Q8)"],
    "health.crisp.teaMissing": ["未下載（繁中／台灣用語增強，啟用時下載）", "未下载（繁中／台湾用语增强，启用时下载）", "Not downloaded (Traditional Chinese / Taiwan wording; downloads when enabled)"],
    "health.crisp.fa": ["時間軸對齊 FA（aligner gguf {quant}）", "时间轴对齐 FA（aligner gguf {quant}）", "Timestamp alignment FA (aligner gguf {quant})"],
    "health.crisp.faMissing": ["未下載（約 643MB，啟用時下載）", "未下载（约 643MB，启用时下载）", "Not downloaded (~643MB; downloads when enabled)"],
    "health.crisp.diar": ["說話者分離（外部 ONNX，與 OpenVINO 共用）", "说话人分离（外部 ONNX，与 OpenVINO 共用）", "Speaker diarization (external ONNX, shared with OpenVINO)"],
    "health.shared.ffmpeg": ["FFmpeg（影片抽音軌用）", "FFmpeg（视频提取音轨用）", "FFmpeg (extract audio from video)"],
    "health.shared.ffmpegOk": ["已偵測：{path}", "已检测到：{path}", "Detected: {path}"],
    "health.shared.ffmpegMissing": ["未下載（轉錄影片時自動下載解壓）", "未下载（转录视频时自动下载解压）", "Not downloaded (auto-downloads when transcribing video)"],
    "health.shared.diar": ["說話者分離模型（OpenVINO/CRISPASR 共用）", "说话人分离模型（OpenVINO/CRISPASR 共用）", "Speaker diarization model (shared by OpenVINO / CRISPASR)"],
    "health.shared.diarMissing": ["未下載（啟用分離時自動下載）", "未下载（启用分离时自动下载）", "Not downloaded (auto-downloads when diarization is enabled)"],
    "health.chatllm.core": ["chatllm 核心（libchatllm.dll）", "chatllm 核心（libchatllm.dll）", "chatllm engine (libchatllm.dll)"],
    "health.chatllm.model": ["Qwen3-ASR 模型（.bin）", "Qwen3-ASR 模型（.bin）", "Qwen3-ASR model (.bin)"],
    "health.chatllm.modelMissing": ["未下載（約 2.3GB，啟用時自動下載）", "未下载（约 2.3GB，启用时自动下载）", "Not downloaded (~2.3GB; auto-downloads when enabled)"],
    "health.chatllm.fa": ["時間軸對齊 FA（ForcedAligner .bin）", "时间轴对齐 FA（ForcedAligner .bin）", "Timestamp alignment FA (ForcedAligner .bin)"],

    "device.inUse": ["使用中", "使用中", "In use"],
    "device.vram": ["{gb} GB 可用", "{gb} GB 可用", "{gb} GB free"],
    "device.diag.noDetector": ["GPU 偵測需要 CrispASR 核心；啟用 GPU 核心時會自動下載，之後即可在此列出可用的獨立顯示卡。目前僅 CPU 推理可用。", "GPU 检测需要 CrispASR 核心；启用 GPU 核心时会自动下载，之后即可在此列出可用的独立显卡。目前仅 CPU 推理可用。", "GPU detection needs the CrispASR engine. Enabling a GPU engine downloads it automatically, after which available discrete GPUs are listed here. Only CPU inference is available for now."],
    "device.diag.probeFailed": ["GPU 偵測未完成（{source}）：{error}　已自動改用 CPU 推理。", "GPU 检测未完成（{source}）：{error}　已自动改用 CPU 推理。", "GPU detection failed ({source}): {error} Falling back to CPU inference."],
    "device.diag.noGpu": ["未偵測到可用的獨立 GPU，僅 CPU 推理可用。", "未检测到可用的独立 GPU，仅 CPU 推理可用。", "No usable discrete GPU detected; only CPU inference is available."],
    "device.diag.exception": ["GPU 偵測例外：{error}", "GPU 检测异常：{error}", "GPU detection error: {error}"],

    "msg.accel.unknown": ["未知的加速版本：{variant}", "未知的加速版本：{variant}", "Unknown acceleration build: {variant}"],
    "msg.accel.ready": ["已選用「{label}」（核心已就緒）。", "已选用「{label}」（核心已就绪）。", "Selected {label} (engine is ready)."],
    "msg.accel.remembered": ["已記住「{label}」。下次載入模型時會下載該版本核心（約 {size} MB），請重新啟動程式以套用。", "已记住「{label}」。下次加载模型时会下载该版本核心（约 {size} MB），请重新启动程序以生效。", "Saved {label}. That engine build (~{size} MB) will be downloaded the next time a model loads. Restart the app to apply it."],
    "msg.backend.same": ["「{label}」已是目前使用的核心。", "「{label}」已是当前使用的核心。", "{label} is already the active engine."],
    "msg.backend.restart": ["已記住「{label}」。請重新啟動程式以套用新核心。", "已记住「{label}」。请重新启动程序以启用新核心。", "Saved {label}. Restart the app to switch engines."],
    "msg.backend.firstRun": ["已記住「{label}」。請重新啟動程式以套用 —— 首次啟用該核心會在啟動時自動下載對應模型。", "已记住「{label}」。请重新启动程序以生效 —— 首次启用该核心时会在启动时自动下载对应模型。", "Saved {label}. Restart the app to apply it - the matching model downloads automatically on first launch with this engine."],
    "msg.model.selectedCanLoad": ["已選定「{core} · {model}」（{arch}）。點「下載並載入模型」即可開始（不需重啟）。", "已选定「{core} · {model}」（{arch}）。点「下载并载入模型」即可开始（无需重启）。", "Selected {core} · {model} ({arch}). Click Download & load model to start - no restart needed."],
    "msg.model.selectedRestart": ["已記住「{core} · {model}」（{arch}）。切換核心需重新啟動程式以套用。", "已记住「{core} · {model}」（{arch}）。切换核心需重新启动程序才能生效。", "Saved {core} · {model} ({arch}). Restart the app to apply the engine switch."],
    "msg.model.same": ["「{core} · {model}」已是目前使用的模型。", "「{core} · {model}」已是当前使用的模型。", "{core} · {model} is already in use."],
    "msg.model.withNote": ["{message}\n{note}", "{message}\n{note}", "{message}\n{note}"],
    "msg.lang.auto": ["自動偵測", "自动检测", "Auto detect"],

    "status.appName": ["聲音辨識小工具", "声音识别小工具", "Speech to Text"],
    "status.backendCpu": ["CPU · OpenVINO INT8", "CPU · OpenVINO INT8", "CPU · OpenVINO INT8"],

    "progress.download17b": ["下載 1.7B 模型（約 4.3 GB）…", "下载 1.7B 模型（约 4.3 GB）…", "Downloading 1.7B model (~4.3 GB)…"],
    "progress.downloadChatllm": ["下載 chatllm 模型（~2.3 GB）…", "下载 chatllm 模型（~2.3 GB）…", "Downloading chatllm model (~2.3 GB)…"],
    "progress.downloadCrisp": ["下載 CrispASR 核心…", "下载 CrispASR 核心…", "Downloading CrispASR engine…"],
    "progress.downloadGguf": ["下載 {name}{note} {quant} GGUF…", "下载 {name}{note} {quant} GGUF…", "Downloading {name}{note} {quant} GGUF…"],
    "progress.downloadBreeze": ["下載 Breeze-ASR-26 {quant} 模型…", "下载 Breeze-ASR-26 {quant} 模型…", "Downloading Breeze-ASR-26 {quant} model…"],
    "progress.downloadAligner": ["下載時間軸對齊器…", "下载时间轴对齐器…", "Downloading timestamp aligner…"],
    "progress.downloadFfmpeg": ["下載 ffmpeg（影片音軌提取）…", "下载 ffmpeg（视频音轨提取）…", "Downloading ffmpeg (video audio extraction)…"],
    "progress.downloadDiar": ["下載說話者分離模型（約 32 MB）…", "下载说话人分离模型（约 32 MB）…", "Downloading speaker diarization model (~32 MB)…"],
    "progress.downloadFa": ["下載時間軸對齊模型（約 939 MB）…", "下载时间轴对齐模型（约 939 MB）…", "Downloading timestamp alignment model (~939 MB)…"],
    "progress.downloadSource": ["下載來源切換：{name}（{code}）{hint}", "下载来源切换：{name}（{code}）{hint}", "Download source switched: {name} ({code}) {hint}"],
    "progress.retry": ["重試 {attempt}：{name} {hint}", "重试 {attempt}：{name} {hint}", "Retry {attempt}: {name} {hint}"],
    "progress.manifest": ["已輸出下載清單：{path}", "已输出下载清单：{path}", "Download manifest written to {path}"],
    "progress.importLocal": ["匯入本地模型：{bundle} → {message}", "导入本地模型：{bundle} → {message}", "Importing local model: {bundle} → {message}"],
    "progress.gpuFallback": ["{backend} 載入失敗，改用 CPU 核心…", "{backend} 加载失败，改用 CPU 核心…", "{backend} failed to load; falling back to CPU…"],
    "progress.enrichSkipped": ["{label}模型未安裝，本次跳過，字幕不受影響", "{label}模型未安装，本次跳过，字幕不受影响", "{label} model is not installed; skipped for this run. Subtitles are unaffected."],
    "progress.trialPrefix": ["試跑前 {mins} 分鐘 · ", "试跑前 {mins} 分钟 · ", "Trial: first {mins} min · "],
    "progress.runInfo": ["{trial}音檔 {mins} 分鐘 · 分段增量落盤 · 生效切分 {chunk} 秒 · 預估常駐約 {ram} GB", "{trial}音频 {mins} 分钟 · 分段增量落盘 · 生效切分 {chunk} 秒 · 预估常驻约 {ram} GB", "{trial}Audio {mins} min · incremental segment writes · effective chunk {chunk}s · estimated resident ~{ram} GB"],
    "progress.tunnelFailed": ["建立失敗：{error}", "创建失败：{error}", "Setup failed: {error}"],
    "progress.tunnelStarting": ["建立中…", "创建中…", "Setting up…"],
    "progress.accelProbeFailed": ["硬體偵測失敗：{error}", "硬件检测失败：{error}", "Hardware detection failed: {error}"],
    "warn.sslInsecure": ["SSL 憑證驗證已降級（{reason}）：下載的模型檔無法驗證來源，請在可信網路環境中使用。", "SSL 证书验证已降级（{reason}）：下载的模型文件无法验证来源，请仅在可信网络环境中使用。", "TLS certificate verification was downgraded ({reason}): downloaded model files cannot be authenticated. Use a trusted network only."],

    "hw.noGpu": ["未偵測到顯示卡 —— 以純處理器（CPU）推理為準給建議。", "未检测到显卡 —— 按纯处理器（CPU）推理给出建议。", "No GPU detected — recommendations assume CPU-only inference."],
    "hw.probeFailed": ["無法偵測顯示卡，以一般配備為準給建議。", "无法检测显卡，按一般配置给出建议。", "Could not detect the GPU; recommendations assume a typical configuration."],
    "hw.discrete": ["偵測到獨立顯示卡：{names} —— 可用最準的模型。", "检测到独立显卡：{names} —— 可用最准的模型。", "Discrete GPU detected: {names} — the most accurate models are available."],
    "hw.integrated": ["偵測到內建顯示晶片：{names} —— 建議用較輕量的模型。", "检测到集成显卡：{names} —— 建议用较轻量的模型。", "Integrated GPU detected: {names} — lighter models are recommended."],

    "accel.reason.noGpu": ["未偵測到顯示卡資訊，先以 Vulkan 版為預設——它在沒有獨立顯卡時會自動退回 CPU 推理，最不會出事。", "未检测到显卡信息，先以 Vulkan 版为默认——它在没有独立显卡时会自动退回 CPU 推理，最不容易出问题。", "No GPU information found, so Vulkan is the default: it falls back to CPU inference when there is no discrete GPU, which is the safest choice."],
    "accel.reason.nvidia": ["偵測到 NVIDIA 顯示卡（{names}）。預設仍用 Vulkan（僅 34 MB、實測速度接近）；若要壓榨 Qwen3-ASR 的極速，可改選 {cuda}。", "检测到 NVIDIA 显卡（{names}）。默认仍用 Vulkan（仅 34 MB、实测速度接近）；若要压榨 Qwen3-ASR 的极速，可改选 {cuda}。", "NVIDIA GPU detected ({names}). Vulkan stays the default (only 34 MB and about as fast in our tests); pick {cuda} if you want maximum Qwen3-ASR speed."],
    "accel.reason.amdIntel": ["偵測到 {vendor} 顯示卡（{names}）。CrispASR 的 Windows 版沒有 ROCm／SYCL，Vulkan 就是這張卡的最佳解。", "检测到 {vendor} 显卡（{names}）。CrispASR 的 Windows 版没有 ROCm／SYCL，Vulkan 就是这张卡的最佳选择。", "{vendor} GPU detected ({names}). The Windows build of CrispASR has no ROCm / SYCL backend, so Vulkan is the best option for this card."],
    "accel.reason.noAccel": ["未偵測到可加速的顯示卡，使用 Vulkan 版（會自動退回 CPU 推理）。", "未检测到可加速的显卡，使用 Vulkan 版（会自动退回 CPU 推理）。", "No GPU usable for acceleration was found; using the Vulkan build (it falls back to CPU inference)."],
    "accel.note.needNvidia": ["需要 NVIDIA 顯示卡", "需要 NVIDIA 显卡", "Requires an NVIDIA GPU"],
    "accel.note.driverTooOld": ["需要 {min} 以上的驅動（目前 {cur}）", "需要 {min} 以上的驱动（目前 {cur}）", "Requires driver {min} or newer (current {cur})"],
    "accel.note.otherCuda": ["驅動版本建議改用另一個 CUDA 版本", "驱动版本建议改用另一个 CUDA 版本", "Your driver is better matched to a different CUDA build"],
    "accel.note.legacyOnly": ["只有在一般 CPU 版閃退時才需要", "只有在一般 CPU 版闪退时才需要", "Only needed if the standard CPU build crashes"],
    "accel.opt.vulkan.label": ["Vulkan（通用 GPU）", "Vulkan（通用 GPU）", "Vulkan (universal GPU)"],
    "accel.opt.vulkan.desc": ["NVIDIA / AMD / Intel 通吃，體積最小。已長期實測穩定，無獨立顯卡時亦可走內顯。", "NVIDIA / AMD / Intel 通吃，体积最小。已长期实测稳定，无独立显卡时也可走核显。", "Works with NVIDIA / AMD / Intel and is the smallest download. Long-term tested and stable; also runs on integrated graphics."],
    "accel.opt.cuda12.label": ["CUDA 12（NVIDIA 專用）", "CUDA 12（NVIDIA 专用）", "CUDA 12 (NVIDIA only)"],
    "accel.opt.cuda12.desc": ["NVIDIA 專用，自帶 CUDA 12 runtime。大型語音 LLM 後端（Qwen3-ASR）通常比 Vulkan 快，但下載量大得多。", "NVIDIA 专用，自带 CUDA 12 runtime。大型语音 LLM 后端（Qwen3-ASR）通常比 Vulkan 快，但下载量大得多。", "NVIDIA only, bundles the CUDA 12 runtime. Usually faster than Vulkan for large speech LLM backends (Qwen3-ASR), but a much bigger download."],
    "accel.opt.cuda13.label": ["CUDA 13（NVIDIA 新驅動）", "CUDA 13（NVIDIA 新驱动）", "CUDA 13 (newer NVIDIA drivers)"],
    "accel.opt.cuda13.desc": ["同 CUDA 12 但用較新的 runtime，體積小一些；需要 580 以上的 NVIDIA 驅動。", "同 CUDA 12 但用较新的 runtime，体积小一些；需要 580 以上的 NVIDIA 驱动。", "Same as CUDA 12 with a newer runtime and a smaller download; requires NVIDIA driver 580 or newer."],
    "accel.opt.cpu.label": ["CPU（純處理器）", "CPU（纯处理器）", "CPU (processor only)"],
    "accel.opt.cpu.desc": ["完全不碰顯卡。速度最慢，但在顯卡驅動有問題時最不會出事。", "完全不碰显卡。速度最慢，但在显卡驱动有问题时最不容易出事。", "Never touches the GPU. Slowest, but the most reliable option when GPU drivers are broken."],
    "accel.opt.cpu-legacy.label": ["CPU 相容版（無 AVX2 的老 CPU）", "CPU 兼容版（无 AVX2 的老 CPU）", "CPU compatibility build (older CPUs without AVX2)"],
    "accel.opt.cpu-legacy.desc": ["給 2013 年前、不支援 AVX2 的老處理器；一般 CPU 版開不起來（閃退／非法指令）時才選這個。", "给 2013 年前、不支持 AVX2 的老处理器；一般 CPU 版起不来（闪退／非法指令）时才选这个。", "For pre-2013 processors without AVX2. Choose this only if the standard CPU build crashes or reports an illegal instruction."],
    // 下載中心「執行期就緒」註記 —— 由後端 _t.msg() 發出，所以屬於 M 而非 D。
    // 放在 D 裡時 t 欄位會退化成 key 原文，且 test_i18n_contract 會判 key 不存在。
    "dl.runtimeCrisp": ["核心 v{version} 已安裝（只需其中一個加速版本）", "核心 v{version} 已安装（只需其中一个加速版本）", "Core v{version} installed (one accelerator build is enough)"],
    "dl.runtimeFfmpegOk": ["ffmpeg 就緒：{path}", "ffmpeg 就绪：{path}", "ffmpeg ready: {path}"],
    "dl.runtimeFfmpegMissing": ["找不到 ffmpeg，需要時會自動下載", "找不到 ffmpeg，需要时会自动下载", "ffmpeg not found; it will be downloaded on demand"],
    "dl.runtimeVadOk": ["VAD 已就緒（隨套件附帶，或已在模型目錄）", "VAD 已就绪（随套件附带，或已在模型目录）", "VAD ready (bundled, or present in the model directory)"],
    "dl.runtimeVadMissing": ["找不到 silero VAD 模型，需重新安裝或手動放入 ov_models", "找不到 silero VAD 模型，需重新安装或手动放入 ov_models", "silero VAD model not found; reinstall or place it in ov_models"],
  };

  function renderMsg(key, vars, fallbackText) {
    const e = M[key];
    if (!e) return fallback(fallbackText != null ? fallbackText : key);
    let out = e[idx()] || e[0];
    if (vars) for (const k in vars) out = out.split("{" + k + "}").join(L(vars[k]));
    return out;
  }

  // Free-form text fallback. Simplified Chinese uses a WORD-level table first
  // (wording differs between variants), then a character table for anything
  // left over. English keeps the original: exception text, paths, model names
  // and user-authored prompts are deliberately not translated.
  const W2S_WORDS = [
    ["重新啟動", "重启"], ["記憶體", "内存"], ["獨立顯示卡", "独立显卡"],
    ["作業系統", "操作系统"], ["資料夾", "文件夹"], ["說話者", "说话人"],
    ["時間軸", "时间轴"], ["靈敏度", "灵敏度"], ["處理器", "处理器"],
    ["麥克風", "麦克风"], ["顯示卡", "显卡"], ["位元組", "字节"],
    ["程式碼", "代码"], ["品質", "质量"], ["準確度", "准确度"],
    ["預設值", "默认值"], ["內顯", "核显"], ["詞彙", "词汇"],
    ["影片", "视频"], ["音軌", "音轨"], ["音檔", "音频"], ["音訊", "音频"],
    ["檔案", "文件"], ["支援", "支持"], ["網路", "网络"], ["軟體", "软件"],
    ["辨識", "识别"], ["設定", "设置"], ["偵測", "检测"], ["載入", "加载"],
    ["下載", "下载"], ["上傳", "上传"], ["儲存", "保存"], ["存檔", "存档"],
    ["顯示", "显示"], ["選擇", "选择"], ["選項", "选项"], ["執行", "执行"],
    ["啟動", "启动"], ["訊息", "消息"], ["程式", "程序"], ["專案", "项目"],
    ["優先", "优先"], ["批次", "批量"], ["佇列", "队列"], ["分離", "分离"],
    ["對齊", "对齐"], ["輸出", "输出"], ["輸入", "输入"], ["讀取", "读取"],
    ["寫入", "写入"], ["產生", "生成"], ["進行", "进行"], ["轉換", "转换"],
    ["簡體", "简体"], ["繁體", "繁体"], ["透過", "通过"], ["預設", "默认"],
    ["憑證", "证书"], ["驗證", "验证"], ["降級", "降级"], ["來源", "来源"],
    ["環境", "环境"], ["伺服器", "服务器"], ["端點", "端点"], ["連線", "连接"],
    ["網址", "网址"], ["權限", "权限"], ["錄製", "录制"], ["分鐘", "分钟"],
    ["秒數", "秒数"], ["預檢", "预检"], ["試跑", "试跑"], ["常駐", "常驻"],
    ["預估", "预估"], ["硬體", "硬件"], ["裝置", "设备"], ["建議", "建议"],
    ["例外", "异常"], ["語系", "语言"], ["位元", "位"],
    ["位址", "地址"], ["緩衝", "缓冲"], ["釋放", "释放"], ["殘留", "残留"],
  ];
  // Sort longest-first so "重新啟動" wins over "啟動" etc.
  W2S_WORDS.sort((a, b) => b[0].length - a[0].length);
  const S2S = {
    "丟":"丢", "並":"并", "乾":"干", "亂":"乱", "亙":"亘", "亞":"亚", "佇":"伫", "佈":"布", "佔":"占", "併":"并", "來":"来", "侖":"仑",
    "侶":"侣", "侷":"局", "俁":"俣", "係":"系", "俔":"伣", "俠":"侠", "俥":"伡", "俬":"私", "倀":"伥", "倆":"俩", "倈":"俫", "倉":"仓",
    "個":"个", "們":"们", "倖":"幸", "倫":"伦", "偉":"伟", "側":"侧", "偵":"侦", "偽":"伪", "傑":"杰", "傖":"伧", "傘":"伞", "備":"备",
    "傢":"家", "傭":"佣", "傯":"偬", "傳":"传", "傴":"伛", "債":"债", "傷":"伤", "傾":"倾", "僂":"偻", "僅":"仅", "僉":"佥", "僑":"侨",
    "僕":"仆", "僞":"伪", "僥":"侥", "僨":"偾", "僱":"雇", "價":"价", "儀":"仪", "儁":"俊", "儂":"侬", "億":"亿", "儈":"侩", "儉":"俭",
    "儎":"傤", "儐":"傧", "儔":"俦", "儕":"侪", "儘":"尽", "償":"偿", "優":"优", "儲":"储", "儷":"俪", "儺":"傩", "儻":"傥", "儼":"俨",
    "兇":"凶", "兌":"兑", "兒":"儿", "兗":"兖", "內":"内", "兩":"两", "冊":"册", "冑":"胄", "冪":"幂", "凈":"净", "凍":"冻", "凜":"凛",
    "凱":"凯", "別":"别", "刪":"删", "剄":"刭", "則":"则", "剋":"克", "剎":"刹", "剗":"刬", "剛":"刚", "剝":"剥", "剮":"剐", "剴":"剀",
    "創":"创", "剷":"铲", "劃":"划", "劇":"剧", "劉":"刘", "劊":"刽", "劌":"刿", "劍":"剑", "劑":"剂", "勁":"劲", "動":"动", "務":"务",
    "勛":"勋", "勝":"胜", "勞":"劳", "勢":"势", "勩":"勚", "勱":"劢", "勳":"勋", "勵":"励", "勸":"劝", "勻":"匀", "匭":"匦", "匯":"汇",
    "匱":"匮", "區":"区", "協":"协", "卹":"恤", "卻":"却", "卽":"即", "厙":"厍", "厠":"厕", "厤":"历", "厭":"厌", "厲":"厉", "厴":"厣",
    "參":"参", "叄":"叁", "叢":"丛", "吒":"咤", "吳":"吴", "吶":"呐", "呂":"吕", "咼":"呙", "員":"员", "唄":"呗", "唸":"念", "問":"问",
    "啓":"启", "啞":"哑", "啟":"启", "啢":"唡", "喚":"唤", "喪":"丧", "喫":"吃", "喬":"乔", "單":"单", "喲":"哟", "嗆":"呛", "嗇":"啬",
    "嗊":"唝", "嗎":"吗", "嗚":"呜", "嗩":"唢", "嗶":"哔", "嘆":"叹", "嘍":"喽", "嘓":"啯", "嘔":"呕", "嘖":"啧", "嘗":"尝", "嘜":"唛",
    "嘩":"哗", "嘮":"唠", "嘯":"啸", "嘰":"叽", "嘵":"哓", "嘸":"呒", "嘽":"啴", "噁":"恶", "噓":"嘘", "噝":"咝", "噠":"哒", "噥":"哝",
    "噦":"哕", "噯":"嗳", "噲":"哙", "噴":"喷", "噸":"吨", "噹":"当", "嚀":"咛", "嚇":"吓", "嚌":"哜", "嚐":"尝", "嚕":"噜", "嚙":"啮",
    "嚥":"咽", "嚦":"呖", "嚨":"咙", "嚮":"向", "嚲":"亸", "嚳":"喾", "嚴":"严", "嚶":"嘤", "囀":"啭", "囁":"嗫", "囂":"嚣", "囅":"冁",
    "囈":"呓", "囉":"啰", "囌":"苏", "囑":"嘱", "囪":"囱", "圇":"囵", "國":"国", "圍":"围", "園":"园", "圓":"圆", "圖":"图", "團":"团",
    "垻":"坝", "埡":"垭", "埰":"采", "執":"执", "堅":"坚", "堊":"垩", "堖":"垴", "堝":"埚", "堯":"尧", "報":"报", "場":"场", "塊":"块",
    "塋":"茔", "塏":"垲", "塒":"埘", "塗":"涂", "塚":"冢", "塢":"坞", "塤":"埙", "塵":"尘", "塹":"堑", "墊":"垫", "墜":"坠", "墮":"堕",
    "墰":"坛", "墳":"坟", "墶":"垯", "墻":"墙", "墾":"垦", "壇":"坛", "壋":"垱", "壎":"埙", "壓":"压", "壘":"垒", "壙":"圹", "壚":"垆",
    "壜":"坛", "壞":"坏", "壟":"垄", "壠":"垅", "壢":"坜", "壩":"坝", "壪":"塆", "壯":"壮", "壺":"壶", "壼":"壸", "壽":"寿", "夠":"够",
    "夢":"梦", "夥":"伙", "夾":"夹", "奐":"奂", "奧":"奥", "奩":"奁", "奪":"夺", "奬":"奖", "奮":"奋", "奼":"姹", "妝":"妆", "姍":"姗",
    "姦":"奸", "娛":"娱", "婁":"娄", "婦":"妇", "婭":"娅", "媧":"娲", "媯":"妫", "媼":"媪", "媽":"妈", "嫋":"袅", "嫗":"妪", "嫵":"妩",
    "嫺":"娴", "嫻":"娴", "嫿":"婳", "嬀":"妫", "嬃":"媭", "嬈":"娆", "嬋":"婵", "嬌":"娇", "嬙":"嫱", "嬡":"嫒", "嬤":"嬷", "嬪":"嫔",
    "嬰":"婴", "嬸":"婶", "孃":"娘", "孌":"娈", "孫":"孙", "學":"学", "孿":"孪", "宮":"宫", "寀":"采", "寢":"寝", "實":"实", "寧":"宁",
    "審":"审", "寫":"写", "寬":"宽", "寵":"宠", "寶":"宝", "將":"将", "專":"专", "尋":"寻", "對":"对", "導":"导", "尷":"尴", "屆":"届",
    "屍":"尸", "屓":"屃", "屜":"屉", "屢":"屡", "層":"层", "屨":"屦", "屬":"属", "岡":"冈", "峯":"峰", "峴":"岘", "島":"岛", "峽":"峡",
    "崍":"崃", "崑":"昆", "崗":"岗", "崙":"仑", "崢":"峥", "崬":"岽", "嵐":"岚", "嵗":"岁", "嶁":"嵝", "嶄":"崭", "嶇":"岖", "嶔":"嵚",
    "嶗":"崂", "嶠":"峤", "嶢":"峣", "嶧":"峄", "嶨":"峃", "嶮":"崄", "嶸":"嵘", "嶺":"岭", "嶼":"屿", "嶽":"岳", "巋":"岿", "巒":"峦",
    "巔":"巅", "巖":"岩", "巰":"巯", "巹":"卺", "帥":"帅", "師":"师", "帳":"帐", "帶":"带", "幀":"帧", "幃":"帏", "幗":"帼", "幘":"帻",
    "幟":"帜", "幣":"币", "幫":"帮", "幬":"帱", "幹":"干", "幾":"几", "庫":"库", "廁":"厕", "廂":"厢", "廄":"厩", "廈":"厦", "廎":"庼",
    "廕":"荫", "廚":"厨", "廝":"厮", "廟":"庙", "廠":"厂", "廡":"庑", "廢":"废", "廣":"广", "廩":"廪", "廬":"庐", "廳":"厅", "弒":"弑",
    "弔":"吊", "弳":"弪", "張":"张", "強":"强", "彆":"别", "彈":"弹", "彌":"弥", "彎":"弯", "彔":"录", "彙":"汇", "彠":"彟", "彥":"彦",
    "彫":"雕", "彲":"彨", "彿":"佛", "後":"后", "徑":"径", "從":"从", "徠":"徕", "復":"复", "徵":"征", "徹":"彻", "恆":"恒", "恥":"耻",
    "悅":"悦", "悞":"悮", "悵":"怅", "悶":"闷", "悽":"凄", "惡":"恶", "惱":"恼", "惲":"恽", "惻":"恻", "愛":"爱", "愜":"惬", "愨":"悫",
    "愴":"怆", "愷":"恺", "愾":"忾", "慄":"栗", "態":"态", "慍":"愠", "慘":"惨", "慚":"惭", "慟":"恸", "慣":"惯", "慤":"悫", "慪":"怄",
    "慫":"怂", "慮":"虑", "慳":"悭", "慶":"庆", "慼":"戚", "慾":"欲", "憂":"忧", "憊":"惫", "憐":"怜", "憑":"凭", "憒":"愦", "憖":"慭",
    "憚":"惮", "憤":"愤", "憫":"悯", "憮":"怃", "憲":"宪", "憶":"忆", "懇":"恳", "應":"应", "懌":"怿", "懍":"懔", "懞":"蒙", "懟":"怼",
    "懣":"懑", "懨":"恹", "懲":"惩", "懶":"懒", "懷":"怀", "懸":"悬", "懺":"忏", "懼":"惧", "懾":"慑", "戀":"恋", "戇":"戆", "戔":"戋",
    "戧":"戗", "戩":"戬", "戰":"战", "戱":"戯", "戲":"戏", "戶":"户", "拋":"抛", "挩":"捝", "挱":"挲", "挾":"挟", "捨":"舍", "捫":"扪",
    "捱":"挨", "捲":"卷", "掃":"扫", "掄":"抡", "掗":"挜", "掙":"挣", "掛":"挂", "採":"采", "揀":"拣", "揚":"扬", "換":"换", "揮":"挥",
    "揯":"搄", "損":"损", "搖":"摇", "搗":"捣", "搵":"揾", "搶":"抢", "摑":"掴", "摜":"掼", "摟":"搂", "摯":"挚", "摳":"抠", "摶":"抟",
    "摺":"折", "摻":"掺", "撈":"捞", "撏":"挦", "撐":"撑", "撓":"挠", "撟":"挢", "撣":"掸", "撥":"拨", "撫":"抚", "撲":"扑", "撳":"揿",
    "撻":"挞", "撾":"挝", "撿":"捡", "擁":"拥", "擄":"掳", "擇":"择", "擊":"击", "擋":"挡", "擔":"担", "據":"据", "擠":"挤", "擣":"捣",
    "擬":"拟", "擯":"摈", "擰":"拧", "擱":"搁", "擲":"掷", "擴":"扩", "擷":"撷", "擺":"摆", "擻":"擞", "擼":"撸", "擾":"扰", "攄":"摅",
    "攆":"撵", "攏":"拢", "攔":"拦", "攖":"撄", "攙":"搀", "攛":"撺", "攜":"携", "攝":"摄", "攢":"攒", "攣":"挛", "攤":"摊", "攪":"搅",
    "攬":"揽", "敎":"教", "敓":"敚", "敗":"败", "敘":"叙", "敵":"敌", "數":"数", "斂":"敛", "斃":"毙", "斆":"敩", "斕":"斓", "斬":"斩",
    "斷":"断", "於":"于", "旂":"旗", "旣":"既", "昇":"升", "時":"时", "晉":"晋", "晝":"昼", "暈":"晕", "暉":"晖", "暘":"旸", "暢":"畅",
    "暫":"暂", "曄":"晔", "曆":"历", "曇":"昙", "曉":"晓", "曏":"向", "曖":"暧", "曠":"旷", "曨":"昽", "曬":"晒", "書":"书", "會":"会",
    "朧":"胧", "朮":"术", "東":"东", "枴":"拐", "柵":"栅", "柺":"拐", "査":"查", "桿":"杆", "梔":"栀", "梘":"枧", "條":"条", "梟":"枭",
    "梲":"棁", "棄":"弃", "棊":"棋", "棖":"枨", "棗":"枣", "棟":"栋", "棧":"栈", "棲":"栖", "棶":"梾", "椏":"桠", "楊":"杨", "楓":"枫",
    "楨":"桢", "業":"业", "極":"极", "榘":"矩", "榦":"干", "榪":"杩", "榮":"荣", "榲":"榅", "榿":"桤", "構":"构", "槍":"枪", "槓":"杠",
    "槤":"梿", "槧":"椠", "槨":"椁", "槮":"椮", "槳":"桨", "槶":"椢", "槼":"椝", "樁":"桩", "樂":"乐", "樅":"枞", "樑":"梁", "樓":"楼",
    "標":"标", "樞":"枢", "樣":"样", "樧":"榝", "樳":"桪", "樸":"朴", "樹":"树", "樺":"桦", "樿":"椫", "橈":"桡", "橋":"桥", "機":"机",
    "橢":"椭", "橫":"横", "檁":"檩", "檉":"柽", "檔":"档", "檜":"桧", "檟":"槚", "檢":"检", "檣":"樯", "檮":"梼", "檯":"台", "檳":"槟",
    "檸":"柠", "檻":"槛", "櫃":"柜", "櫓":"橹", "櫚":"榈", "櫛":"栉", "櫝":"椟", "櫞":"橼", "櫟":"栎", "櫥":"橱", "櫧":"槠", "櫨":"栌",
    "櫪":"枥", "櫫":"橥", "櫬":"榇", "櫱":"蘖", "櫳":"栊", "櫸":"榉", "櫻":"樱", "欄":"栏", "欅":"榉", "權":"权", "欏":"椤", "欒":"栾",
    "欖":"榄", "欞":"棂", "欽":"钦", "歎":"叹", "歐":"欧", "歟":"欤", "歡":"欢", "歲":"岁", "歷":"历", "歸":"归", "歿":"殁", "殘":"残",
    "殞":"殒", "殤":"殇", "殫":"殚", "殭":"僵", "殮":"殓", "殯":"殡", "殲":"歼", "殺":"杀", "殻":"壳", "殼":"壳", "毀":"毁", "毆":"殴",
    "毿":"毵", "氂":"牦", "氈":"毡", "氌":"氇", "氣":"气", "氫":"氢", "氬":"氩", "氳":"氲", "氾":"泛", "汎":"泛", "汙":"污", "決":"决",
    "沒":"没", "沖":"冲", "況":"况", "泝":"溯", "洩":"泄", "洶":"汹", "浹":"浃", "涇":"泾", "涗":"涚", "涼":"凉", "淒":"凄", "淚":"泪",
    "淥":"渌", "淨":"净", "淩":"凌", "淪":"沦", "淵":"渊", "淶":"涞", "淺":"浅", "渙":"涣", "減":"减", "渢":"沨", "渦":"涡", "測":"测",
    "渾":"浑", "湊":"凑", "湞":"浈", "湧":"涌", "湯":"汤", "溈":"沩", "準":"准", "溝":"沟", "溫":"温", "溮":"浉", "溳":"涢", "溼":"湿",
    "滄":"沧", "滅":"灭", "滌":"涤", "滎":"荥", "滙":"汇", "滬":"沪", "滯":"滞", "滲":"渗", "滷":"卤", "滸":"浒", "滻":"浐", "滾":"滚",
    "滿":"满", "漁":"渔", "漊":"溇", "漚":"沤", "漢":"汉", "漣":"涟", "漬":"渍", "漲":"涨", "漵":"溆", "漸":"渐", "漿":"浆", "潁":"颍",
    "潑":"泼", "潔":"洁", "潙":"沩", "潛":"潜", "潤":"润", "潯":"浔", "潰":"溃", "潷":"滗", "潿":"涠", "澀":"涩", "澆":"浇", "澇":"涝",
    "澐":"沄", "澗":"涧", "澠":"渑", "澤":"泽", "澦":"滪", "澩":"泶", "澮":"浍", "澱":"淀", "濁":"浊", "濃":"浓", "濕":"湿", "濘":"泞",
    "濚":"溁", "濛":"蒙", "濜":"浕", "濟":"济", "濤":"涛", "濫":"滥", "濰":"潍", "濱":"滨", "濺":"溅", "濼":"泺", "濾":"滤", "瀂":"澛",
    "瀅":"滢", "瀆":"渎", "瀉":"泻", "瀋":"沈", "瀏":"浏", "瀕":"濒", "瀘":"泸", "瀝":"沥", "瀟":"潇", "瀠":"潆", "瀦":"潴", "瀧":"泷",
    "瀨":"濑", "瀰":"弥", "瀲":"潋", "瀾":"澜", "灃":"沣", "灄":"滠", "灑":"洒", "灕":"漓", "灘":"滩", "灝":"灏", "灣":"湾", "灤":"滦",
    "灧":"滟", "灩":"滟", "災":"灾", "為":"为", "烏":"乌", "烴":"烃", "無":"无", "煉":"炼", "煒":"炜", "煙":"烟", "煢":"茕", "煥":"焕",
    "煩":"烦", "煬":"炀", "熅":"煴", "熒":"荧", "熗":"炝", "熱":"热", "熲":"颎", "熾":"炽", "燁":"烨", "燈":"灯", "燉":"炖", "燒":"烧",
    "燙":"烫", "燜":"焖", "營":"营", "燦":"灿", "燬":"毁", "燭":"烛", "燴":"烩", "燻":"熏", "燼":"烬", "燾":"焘", "爍":"烁", "爐":"炉",
    "爛":"烂", "爭":"争", "爲":"为", "爺":"爷", "爾":"尔", "牀":"床", "牆":"墙", "牘":"牍", "牽":"牵", "犖":"荦", "犛":"牦", "犢":"犊",
    "犧":"牺", "狀":"状", "狹":"狭", "狽":"狈", "猙":"狰", "猶":"犹", "猻":"狲", "獁":"犸", "獃":"呆", "獄":"狱", "獅":"狮", "獎":"奖",
    "獨":"独", "獪":"狯", "獫":"猃", "獮":"狝", "獰":"狞", "獲":"获", "獵":"猎", "獷":"犷", "獸":"兽", "獺":"獭", "獻":"献", "獼":"猕",
    "玀":"猡", "現":"现", "琱":"雕", "琺":"珐", "琿":"珲", "瑋":"玮", "瑒":"玚", "瑣":"琐", "瑤":"瑶", "瑩":"莹", "瑪":"玛", "瑲":"玱",
    "璉":"琏", "璡":"琎", "璣":"玑", "璦":"瑷", "璫":"珰", "環":"环", "璵":"玙", "璸":"瑸", "璽":"玺", "璿":"璇", "瓊":"琼", "瓏":"珑",
    "瓔":"璎", "瓚":"瓒", "甌":"瓯", "甕":"瓮", "產":"产", "産":"产", "甦":"苏", "甯":"宁", "畝":"亩", "畢":"毕", "畫":"画", "異":"异",
    "畵":"画", "當":"当", "疇":"畴", "疊":"叠", "痙":"痉", "痠":"酸", "痾":"疴", "瘂":"痖", "瘋":"疯", "瘍":"疡", "瘓":"痪", "瘞":"瘗",
    "瘡":"疮", "瘧":"疟", "瘮":"瘆", "瘲":"疭", "瘺":"瘘", "瘻":"瘘", "療":"疗", "癆":"痨", "癇":"痫", "癉":"瘅", "癒":"愈", "癘":"疠",
    "癟":"瘪", "癡":"痴", "癢":"痒", "癤":"疖", "癥":"症", "癧":"疬", "癩":"癞", "癬":"癣", "癭":"瘿", "癮":"瘾", "癰":"痈", "癱":"瘫",
    "癲":"癫", "發":"发", "皁":"皂", "皚":"皑", "皰":"疱", "皸":"皲", "皺":"皱", "盃":"杯", "盜":"盗", "盞":"盏", "盡":"尽", "監":"监",
    "盤":"盘", "盧":"卢", "盪":"荡", "眞":"真", "眥":"眦", "眾":"众", "睏":"困", "睜":"睁", "睞":"睐", "瞘":"眍", "瞞":"瞒", "瞶":"瞆",
    "瞼":"睑", "矇":"蒙", "矓":"眬", "矚":"瞩", "矯":"矫", "硃":"朱", "硜":"硁", "硤":"硖", "硨":"砗", "硯":"砚", "碕":"埼", "碩":"硕",
    "碭":"砀", "碸":"砜", "確":"确", "碼":"码", "磑":"硙", "磚":"砖", "磠":"硵", "磣":"碜", "磧":"碛", "磯":"矶", "磽":"硗", "礄":"硚",
    "礆":"硷", "礎":"础", "礙":"碍", "礦":"矿", "礪":"砺", "礫":"砾", "礬":"矾", "礱":"砻", "祕":"秘", "祿":"禄", "禍":"祸", "禎":"祯",
    "禕":"祎", "禡":"祃", "禦":"御", "禪":"禅", "禮":"礼", "禰":"祢", "禱":"祷", "禿":"秃", "秈":"籼", "稅":"税", "稈":"秆", "稜":"棱",
    "稟":"禀", "種":"种", "稱":"称", "穀":"谷", "穌":"稣", "積":"积", "穎":"颖", "穠":"秾", "穡":"穑", "穢":"秽", "穩":"稳", "穫":"获",
    "穭":"穞", "窩":"窝", "窪":"洼", "窮":"穷", "窯":"窑", "窵":"窎", "窶":"窭", "窺":"窥", "竄":"窜", "竅":"窍", "竇":"窦", "竈":"灶",
    "竊":"窃", "竪":"竖", "競":"竞", "筆":"笔", "筍":"笋", "筧":"笕", "箇":"个", "箋":"笺", "箏":"筝", "節":"节", "範":"范", "築":"筑",
    "篋":"箧", "篔":"筼", "篠":"筿", "篤":"笃", "篩":"筛", "篳":"筚", "簀":"箦", "簍":"篓", "簑":"蓑", "簞":"箪", "簡":"简", "簣":"篑",
    "簫":"箫", "簹":"筜", "簽":"签", "簾":"帘", "籃":"篮", "籌":"筹", "籙":"箓", "籛":"篯", "籜":"箨", "籟":"籁", "籠":"笼", "籤":"签",
    "籩":"笾", "籪":"簖", "籬":"篱", "籮":"箩", "籲":"吁", "粵":"粤", "糉":"粽", "糝":"糁", "糞":"粪", "糧":"粮", "糰":"团", "糲":"粝",
    "糴":"籴", "糶":"粜", "糹":"纟", "糾":"纠", "紀":"纪", "紂":"纣", "約":"约", "紅":"红", "紆":"纡", "紇":"纥", "紈":"纨", "紉":"纫",
    "紋":"纹", "納":"纳", "紐":"纽", "紓":"纾", "純":"纯", "紕":"纰", "紖":"纼", "紗":"纱", "紘":"纮", "紙":"纸", "級":"级", "紛":"纷",
    "紜":"纭", "紝":"纴", "紡":"纺", "紮":"扎", "細":"细", "紱":"绂", "紲":"绁", "紳":"绅", "紵":"纻", "紹":"绍", "紺":"绀", "紼":"绋",
    "紿":"绐", "絀":"绌", "終":"终", "絃":"弦", "組":"组", "絆":"绊", "絎":"绗", "結":"结", "絕":"绝", "絛":"绦", "絝":"绔", "絞":"绞",
    "絡":"络", "絢":"绚", "給":"给", "絨":"绒", "絰":"绖", "統":"统", "絲":"丝", "絳":"绛", "絶":"绝", "絹":"绢", "綁":"绑", "綃":"绡",
    "綆":"绠", "綈":"绨", "綉":"绣", "綌":"绤", "綏":"绥", "綑":"捆", "經":"经", "綜":"综", "綞":"缍", "綠":"绿", "綢":"绸", "綣":"绻",
    "綫":"线", "綬":"绶", "維":"维", "綯":"绹", "綰":"绾", "綱":"纲", "網":"网", "綳":"绷", "綴":"缀", "綵":"彩", "綸":"纶", "綹":"绺",
    "綺":"绮", "綻":"绽", "綽":"绰", "綾":"绫", "綿":"绵", "緄":"绲", "緇":"缁", "緊":"紧", "緋":"绯", "緑":"绿", "緒":"绪", "緓":"绬",
    "緔":"绱", "緗":"缃", "緘":"缄", "緙":"缂", "線":"线", "緝":"缉", "緞":"缎", "締":"缔", "緡":"缗", "緣":"缘", "緦":"缌", "編":"编",
    "緩":"缓", "緬":"缅", "緯":"纬", "緱":"缑", "緲":"缈", "練":"练", "緶":"缏", "緹":"缇", "緻":"致", "緼":"缊", "縈":"萦", "縉":"缙",
    "縊":"缢", "縋":"缒", "縐":"绉", "縑":"缣", "縕":"缊", "縗":"缞", "縛":"缚", "縝":"缜", "縞":"缟", "縟":"缛", "縣":"县", "縧":"绦",
    "縫":"缝", "縭":"缡", "縮":"缩", "縱":"纵", "縲":"缧", "縴":"纤", "縵":"缦", "縶":"絷", "縷":"缕", "縹":"缥", "總":"总", "績":"绩",
    "繃":"绷", "繅":"缫", "繆":"缪", "繒":"缯", "織":"织", "繕":"缮", "繚":"缭", "繞":"绕", "繡":"绣", "繢":"缋", "繩":"绳", "繪":"绘",
    "繫":"系", "繭":"茧", "繮":"缰", "繯":"缳", "繰":"缲", "繳":"缴", "繹":"绎", "繼":"继", "繽":"缤", "繾":"缱", "纇":"颣", "纈":"缬",
    "纊":"纩", "續":"续", "纍":"累", "纏":"缠", "纓":"缨", "纔":"才", "纖":"纤", "纘":"缵", "纜":"缆", "缽":"钵", "罈":"坛", "罌":"罂",
    "罎":"坛", "罰":"罚", "罵":"骂", "罷":"罢", "羅":"罗", "羆":"罴", "羈":"羁", "羋":"芈", "羣":"群", "羥":"羟", "羨":"羡", "義":"义",
    "羶":"膻", "習":"习", "翫":"玩", "翬":"翚", "翹":"翘", "翽":"翙", "耬":"耧", "耮":"耢", "聖":"圣", "聞":"闻", "聯":"联", "聰":"聪",
    "聲":"声", "聳":"耸", "聵":"聩", "聶":"聂", "職":"职", "聹":"聍", "聽":"听", "聾":"聋", "肅":"肃", "脅":"胁", "脈":"脉", "脛":"胫",
    "脣":"唇", "脩":"修", "脫":"脱", "脹":"胀", "腎":"肾", "腖":"胨", "腡":"脶", "腦":"脑", "腫":"肿", "腳":"脚", "腸":"肠", "膃":"腽",
    "膕":"腘", "膚":"肤", "膠":"胶", "膩":"腻", "膽":"胆", "膾":"脍", "膿":"脓", "臉":"脸", "臍":"脐", "臏":"膑", "臘":"腊", "臚":"胪",
    "臟":"脏", "臠":"脔", "臢":"臜", "臥":"卧", "臨":"临", "臺":"台", "與":"与", "興":"兴", "舉":"举", "舊":"旧", "舘":"馆", "艙":"舱",
    "艤":"舣", "艦":"舰", "艫":"舻", "艱":"艰", "艷":"艳", "芻":"刍", "苧":"苎", "茲":"兹", "荊":"荆", "莊":"庄", "莖":"茎", "莢":"荚",
    "莧":"苋", "華":"华", "菴":"庵", "菸":"烟", "萇":"苌", "萊":"莱", "萬":"万", "萴":"荝", "萵":"莴", "葉":"叶", "葒":"荭", "葤":"荮",
    "葦":"苇", "葯":"药", "葷":"荤", "蒐":"搜", "蒓":"莼", "蒔":"莳", "蒕":"蒀", "蒞":"莅", "蒼":"苍", "蓀":"荪", "蓆":"席", "蓋":"盖",
    "蓮":"莲", "蓯":"苁", "蓴":"莼", "蓽":"荜", "蔔":"卜", "蔘":"参", "蔞":"蒌", "蔣":"蒋", "蔥":"葱", "蔦":"茑", "蔭":"荫", "蕁":"荨",
    "蕆":"蒇", "蕎":"荞", "蕒":"荬", "蕓":"芸", "蕕":"莸", "蕘":"荛", "蕢":"蒉", "蕩":"荡", "蕪":"芜", "蕭":"萧", "蕷":"蓣", "薀":"蕰",
    "薈":"荟", "薊":"蓟", "薌":"芗", "薑":"姜", "薔":"蔷", "薘":"荙", "薟":"莶", "薦":"荐", "薩":"萨", "薴":"苧", "薹":"苔", "薺":"荠",
    "藍":"蓝", "藎":"荩", "藝":"艺", "藥":"药", "藪":"薮", "藴":"蕴", "藶":"苈", "藹":"蔼", "藺":"蔺", "蘀":"萚", "蘄":"蕲", "蘆":"芦",
    "蘇":"苏", "蘊":"蕴", "蘋":"苹", "蘚":"藓", "蘞":"蔹", "蘢":"茏", "蘭":"兰", "蘺":"蓠", "蘿":"萝", "虆":"蔂", "處":"处", "虛":"虚",
    "虜":"虏", "號":"号", "虧":"亏", "虯":"虬", "蛺":"蛱", "蛻":"蜕", "蜆":"蚬", "蝕":"蚀", "蝟":"猬", "蝦":"虾", "蝨":"虱", "蝸":"蜗",
    "螄":"蛳", "螞":"蚂", "螢":"萤", "螻":"蝼", "螿":"螀", "蟄":"蛰", "蟈":"蝈", "蟎":"螨", "蟣":"虮", "蟬":"蝉", "蟯":"蛲", "蟲":"虫",
    "蟶":"蛏", "蟻":"蚁", "蠁":"蚃", "蠅":"蝇", "蠆":"虿", "蠍":"蝎", "蠐":"蛴", "蠑":"蝾", "蠔":"蚝", "蠟":"蜡", "蠣":"蛎", "蠨":"蟏",
    "蠱":"蛊", "蠶":"蚕", "蠻":"蛮", "衆":"众", "衊":"蔑", "術":"术", "衕":"同", "衚":"胡", "衛":"卫", "衝":"冲", "袞":"衮", "裊":"袅",
    "裏":"里", "補":"补", "裝":"装", "裡":"里", "製":"制", "複":"复", "褌":"裈", "褘":"袆", "褲":"裤", "褳":"裢", "褸":"褛", "褻":"亵",
    "襇":"裥", "襉":"裥", "襏":"袯", "襖":"袄", "襝":"裣", "襠":"裆", "襤":"褴", "襪":"袜", "襬":"摆", "襯":"衬", "襲":"袭", "襴":"襕",
    "覈":"核", "見":"见", "覎":"觃", "規":"规", "覓":"觅", "視":"视", "覘":"觇", "覡":"觋", "覥":"觍", "覦":"觎", "親":"亲", "覬":"觊",
    "覯":"觏", "覲":"觐", "覷":"觑", "覺":"觉", "覽":"览", "覿":"觌", "觀":"观", "觴":"觞", "觶":"觯", "觸":"触", "訁":"讠", "訂":"订",
    "訃":"讣", "計":"计", "訊":"讯", "訌":"讧", "討":"讨", "訐":"讦", "訒":"讱", "訓":"训", "訕":"讪", "訖":"讫", "託":"托", "記":"记",
    "訛":"讹", "訝":"讶", "訟":"讼", "訣":"诀", "訥":"讷", "訩":"讻", "訪":"访", "設":"设", "許":"许", "訴":"诉", "訶":"诃", "診":"诊",
    "註":"注", "証":"证", "詁":"诂", "詆":"诋", "詎":"讵", "詐":"诈", "詒":"诒", "詔":"诏", "評":"评", "詖":"诐", "詗":"诇", "詘":"诎",
    "詛":"诅", "詞":"词", "詠":"咏", "詡":"诩", "詢":"询", "詣":"诣", "試":"试", "詩":"诗", "詫":"诧", "詬":"诟", "詭":"诡", "詮":"诠",
    "詰":"诘", "話":"话", "該":"该", "詳":"详", "詵":"诜", "詼":"诙", "詿":"诖", "誄":"诔", "誅":"诛", "誆":"诓", "誇":"夸", "誌":"志",
    "認":"认", "誑":"诳", "誒":"诶", "誕":"诞", "誘":"诱", "誚":"诮", "語":"语", "誠":"诚", "誡":"诫", "誣":"诬", "誤":"误", "誥":"诰",
    "誦":"诵", "誨":"诲", "說":"说", "説":"说", "誰":"谁", "課":"课", "誶":"谇", "誹":"诽", "誼":"谊", "誾":"訚", "調":"调", "諂":"谄",
    "諄":"谆", "談":"谈", "諉":"诿", "請":"请", "諍":"诤", "諏":"诹", "諑":"诼", "諒":"谅", "論":"论", "諗":"谂", "諛":"谀", "諜":"谍",
    "諝":"谞", "諞":"谝", "諡":"谥", "諢":"诨", "諤":"谔", "諦":"谛", "諧":"谐", "諫":"谏", "諭":"谕", "諮":"咨", "諱":"讳", "諳":"谙",
    "諶":"谌", "諷":"讽", "諸":"诸", "諺":"谚", "諼":"谖", "諾":"诺", "謀":"谋", "謁":"谒", "謂":"谓", "謄":"誊", "謅":"诌", "謊":"谎",
    "謎":"谜", "謐":"谧", "謔":"谑", "謖":"谡", "謗":"谤", "謙":"谦", "謚":"谥", "講":"讲", "謝":"谢", "謠":"谣", "謡":"谣", "謨":"谟",
    "謫":"谪", "謬":"谬", "謭":"谫", "謳":"讴", "謹":"谨", "謾":"谩", "譁":"哗", "證":"证", "譎":"谲", "譏":"讥", "譖":"谮", "識":"识",
    "譙":"谯", "譚":"谭", "譜":"谱", "譟":"噪", "譫":"谵", "譭":"毁", "譯":"译", "議":"议", "譴":"谴", "護":"护", "譸":"诪", "譽":"誉",
    "譾":"谫", "讀":"读", "讅":"谉", "變":"变", "讋":"詟", "讎":"雠", "讒":"谗", "讓":"让", "讕":"谰", "讖":"谶", "讚":"赞", "讜":"谠",
    "讞":"谳", "豈":"岂", "豎":"竖", "豐":"丰", "豔":"艳", "豬":"猪", "豶":"豮", "貓":"猫", "貝":"贝", "貞":"贞", "貟":"贠", "負":"负",
    "財":"财", "貢":"贡", "貧":"贫", "貨":"货", "販":"贩", "貪":"贪", "貫":"贯", "責":"责", "貯":"贮", "貰":"贳", "貲":"赀", "貳":"贰",
    "貴":"贵", "貶":"贬", "買":"买", "貸":"贷", "貺":"贶", "費":"费", "貼":"贴", "貽":"贻", "貿":"贸", "賀":"贺", "賁":"贲", "賂":"赂",
    "賃":"赁", "賄":"贿", "賅":"赅", "資":"资", "賈":"贾", "賊":"贼", "賑":"赈", "賒":"赊", "賓":"宾", "賕":"赇", "賙":"赒", "賚":"赉",
    "賜":"赐", "賞":"赏", "賠":"赔", "賡":"赓", "賢":"贤", "賣":"卖", "賤":"贱", "賦":"赋", "賧":"赕", "質":"质", "賫":"赍", "賬":"账",
    "賭":"赌", "賴":"赖", "賵":"赗", "賺":"赚", "賻":"赙", "購":"购", "賽":"赛", "賾":"赜", "贄":"贽", "贅":"赘", "贇":"赟", "贈":"赠",
    "贊":"赞", "贋":"赝", "贍":"赡", "贏":"赢", "贐":"赆", "贓":"赃", "贔":"赑", "贖":"赎", "贗":"赝", "贛":"赣", "贜":"赃", "赬":"赪",
    "趕":"赶", "趙":"赵", "趨":"趋", "趲":"趱", "跡":"迹", "踐":"践", "踰":"逾", "踴":"踊", "蹌":"跄", "蹕":"跸", "蹟":"迹", "蹠":"跖",
    "蹣":"蹒", "蹤":"踪", "蹺":"跷", "躂":"跶", "躉":"趸", "躊":"踌", "躋":"跻", "躍":"跃", "躑":"踯", "躒":"跞", "躓":"踬", "躕":"蹰",
    "躚":"跹", "躡":"蹑", "躥":"蹿", "躦":"躜", "躪":"躏", "軀":"躯", "車":"车", "軋":"轧", "軌":"轨", "軍":"军", "軑":"轪", "軒":"轩",
    "軔":"轫", "軛":"轭", "軟":"软", "軤":"轷", "軫":"轸", "軲":"轱", "軸":"轴", "軹":"轵", "軺":"轺", "軻":"轲", "軼":"轶", "軾":"轼",
    "較":"较", "輅":"辂", "輇":"辁", "輈":"辀", "載":"载", "輊":"轾", "輒":"辄", "輓":"挽", "輔":"辅", "輕":"轻", "輛":"辆", "輜":"辎",
    "輝":"辉", "輞":"辋", "輟":"辍", "輥":"辊", "輦":"辇", "輩":"辈", "輪":"轮", "輬":"辌", "輯":"辑", "輳":"辏", "輸":"输", "輻":"辐",
    "輼":"辒", "輾":"辗", "輿":"舆", "轀":"辒", "轂":"毂", "轄":"辖", "轅":"辕", "轆":"辘", "轉":"转", "轍":"辙", "轎":"轿", "轔":"辚",
    "轟":"轰", "轡":"辔", "轢":"轹", "轤":"轳", "辦":"办", "辭":"辞", "辮":"辫", "辯":"辩", "農":"农", "迴":"回", "逕":"迳", "這":"这",
    "連":"连", "週":"周", "進":"进", "遊":"游", "運":"运", "過":"过", "達":"达", "違":"违", "遙":"遥", "遜":"逊", "遞":"递", "遠":"远",
    "遡":"溯", "適":"适", "遲":"迟", "遷":"迁", "選":"选", "遺":"遗", "遼":"辽", "邁":"迈", "還":"还", "邇":"迩", "邊":"边", "邏":"逻",
    "邐":"逦", "郟":"郏", "郵":"邮", "鄆":"郓", "鄉":"乡", "鄒":"邹", "鄔":"邬", "鄖":"郧", "鄧":"邓", "鄭":"郑", "鄰":"邻", "鄲":"郸",
    "鄴":"邺", "鄶":"郐", "鄺":"邝", "酇":"酂", "酈":"郦", "醃":"腌", "醖":"酝", "醜":"丑", "醞":"酝", "醟":"蒏", "醣":"糖", "醫":"医",
    "醬":"酱", "醱":"酦", "釀":"酿", "釁":"衅", "釃":"酾", "釅":"酽", "釋":"释", "釐":"厘", "釒":"钅", "釓":"钆", "釔":"钇", "釕":"钌",
    "釗":"钊", "釘":"钉", "釙":"钋", "針":"针", "釣":"钓", "釤":"钐", "釦":"扣", "釧":"钏", "釩":"钒", "釵":"钗", "釷":"钍", "釹":"钕",
    "釺":"钎", "鈀":"钯", "鈁":"钫", "鈃":"钘", "鈄":"钭", "鈅":"钥", "鈈":"钚", "鈉":"钠", "鈍":"钝", "鈎":"钩", "鈐":"钤", "鈑":"钣",
    "鈒":"钑", "鈔":"钞", "鈕":"钮", "鈞":"钧", "鈡":"钟", "鈣":"钙", "鈥":"钬", "鈦":"钛", "鈧":"钪", "鈮":"铌", "鈰":"铈", "鈳":"钶",
    "鈴":"铃", "鈷":"钴", "鈸":"钹", "鈹":"铍", "鈺":"钰", "鈽":"钸", "鈾":"铀", "鈿":"钿", "鉀":"钾", "鉅":"巨", "鉆":"钻", "鉈":"铊",
    "鉉":"铉", "鉋":"铇", "鉍":"铋", "鉑":"铂", "鉕":"钷", "鉗":"钳", "鉚":"铆", "鉛":"铅", "鉞":"钺", "鉢":"钵", "鉤":"钩", "鉦":"钲",
    "鉬":"钼", "鉭":"钽", "鉳":"锫", "鉶":"铏", "鉸":"铰", "鉺":"铒", "鉻":"铬", "鉿":"铪", "銀":"银", "銃":"铳", "銅":"铜", "銍":"铚",
    "銑":"铣", "銓":"铨", "銖":"铢", "銘":"铭", "銚":"铫", "銛":"铦", "銜":"衔", "銠":"铑", "銣":"铷", "銥":"铱", "銦":"铟", "銨":"铵",
    "銩":"铥", "銪":"铕", "銫":"铯", "銬":"铐", "銱":"铞", "銳":"锐", "銷":"销", "銹":"锈", "銻":"锑", "銼":"锉", "鋁":"铝", "鋃":"锒",
    "鋅":"锌", "鋇":"钡", "鋌":"铤", "鋏":"铗", "鋒":"锋", "鋙":"铻", "鋝":"锊", "鋟":"锓", "鋣":"铘", "鋤":"锄", "鋥":"锃", "鋦":"锔",
    "鋨":"锇", "鋩":"铓", "鋪":"铺", "鋭":"锐", "鋮":"铖", "鋯":"锆", "鋰":"锂", "鋱":"铽", "鋶":"锍", "鋸":"锯", "鋼":"钢", "錁":"锞",
    "錄":"录", "錆":"锖", "錇":"锫", "錈":"锩", "錏":"铔", "錐":"锥", "錒":"锕", "錕":"锟", "錘":"锤", "錙":"锱", "錚":"铮", "錛":"锛",
    "錟":"锬", "錠":"锭", "錡":"锜", "錢":"钱", "錦":"锦", "錨":"锚", "錩":"锠", "錫":"锡", "錮":"锢", "錯":"错", "録":"录", "錳":"锰",
    "錶":"表", "錸":"铼", "錼":"镎", "鍀":"锝", "鍁":"锨", "鍃":"锪", "鍅":"钫", "鍆":"钔", "鍇":"锴", "鍈":"锳", "鍊":"炼", "鍋":"锅",
    "鍍":"镀", "鍔":"锷", "鍘":"铡", "鍚":"钖", "鍛":"锻", "鍠":"锽", "鍤":"锸", "鍥":"锲", "鍩":"锘", "鍬":"锹", "鍰":"锾", "鍵":"键",
    "鍶":"锶", "鍺":"锗", "鍼":"针", "鍾":"钟", "鎂":"镁", "鎄":"锿", "鎇":"镅", "鎊":"镑", "鎌":"镰", "鎔":"镕", "鎖":"锁", "鎘":"镉",
    "鎚":"锤", "鎛":"镈", "鎡":"镃", "鎢":"钨", "鎣":"蓥", "鎦":"镏", "鎧":"铠", "鎩":"铩", "鎪":"锼", "鎬":"镐", "鎭":"镇", "鎮":"镇",
    "鎰":"镒", "鎲":"镋", "鎳":"镍", "鎵":"镓", "鎶":"鿔", "鎸":"镌", "鎿":"镎", "鏃":"镞", "鏇":"旋", "鏈":"链", "鏌":"镆", "鏍":"镙",
    "鏐":"镠", "鏑":"镝", "鏗":"铿", "鏘":"锵", "鏜":"镗", "鏝":"镘", "鏞":"镛", "鏟":"铲", "鏡":"镜", "鏢":"镖", "鏤":"镂", "鏨":"錾",
    "鏰":"镚", "鏵":"铧", "鏷":"镤", "鏹":"镪", "鏽":"锈", "鐃":"铙", "鐋":"铴", "鐐":"镣", "鐒":"铹", "鐓":"镦", "鐔":"镡", "鐘":"钟",
    "鐙":"镫", "鐝":"镢", "鐠":"镨", "鐦":"锎", "鐧":"锏", "鐨":"镄", "鐫":"镌", "鐮":"镰", "鐲":"镯", "鐳":"镭", "鐵":"铁", "鐶":"镮",
    "鐸":"铎", "鐺":"铛", "鐿":"镱", "鑄":"铸", "鑊":"镬", "鑌":"镔", "鑑":"鉴", "鑒":"鉴", "鑔":"镲", "鑕":"锧", "鑞":"镴", "鑠":"铄",
    "鑣":"镳", "鑥":"镥", "鑭":"镧", "鑰":"钥", "鑱":"镵", "鑲":"镶", "鑷":"镊", "鑹":"镩", "鑼":"锣", "鑽":"钻", "鑾":"銮", "鑿":"凿",
    "钁":"镢", "钂":"镋", "長":"长", "門":"门", "閂":"闩", "閃":"闪", "閆":"闫", "閈":"闬", "閉":"闭", "開":"开", "閌":"闶", "閎":"闳",
    "閏":"闰", "閑":"闲", "閒":"闲", "間":"间", "閔":"闵", "閘":"闸", "閡":"阂", "閣":"阁", "閤":"合", "閥":"阀", "閨":"闺", "閩":"闽",
    "閫":"阃", "閬":"阆", "閭":"闾", "閱":"阅", "閲":"阅", "閶":"阊", "閹":"阉", "閻":"阎", "閼":"阏", "閽":"阍", "閾":"阈", "閿":"阌",
    "闃":"阒", "闆":"板", "闇":"暗", "闈":"闱", "闊":"阔", "闋":"阕", "闌":"阑", "闍":"阇", "闐":"阗", "闒":"阘", "闓":"闿", "闔":"阖",
    "闕":"阙", "闖":"闯", "關":"关", "闞":"阚", "闠":"阓", "闡":"阐", "闢":"辟", "闤":"阛", "闥":"闼", "陘":"陉", "陝":"陕", "陞":"升",
    "陣":"阵", "陰":"阴", "陳":"陈", "陸":"陆", "陽":"阳", "隉":"陧", "隊":"队", "階":"阶", "隕":"陨", "際":"际", "隨":"随", "險":"险",
    "隯":"陦", "隱":"隐", "隴":"陇", "隸":"隶", "隻":"只", "雋":"隽", "雖":"虽", "雙":"双", "雛":"雏", "雜":"杂", "雞":"鸡", "離":"离",
    "難":"难", "雲":"云", "電":"电", "霑":"沾", "霢":"霡", "霧":"雾", "霽":"霁", "靂":"雳", "靄":"霭", "靆":"叇", "靈":"灵", "靉":"叆",
    "靚":"靓", "靜":"静", "靝":"靔", "靦":"腼", "靨":"靥", "鞏":"巩", "鞝":"绱", "鞦":"秋", "鞽":"鞒", "韁":"缰", "韃":"鞑", "韆":"千",
    "韉":"鞯", "韋":"韦", "韌":"韧", "韍":"韨", "韓":"韩", "韙":"韪", "韜":"韬", "韝":"鞲", "韞":"韫", "韻":"韵", "響":"响", "頁":"页",
    "頂":"顶", "頃":"顷", "項":"项", "順":"顺", "頇":"顸", "須":"须", "頊":"顼", "頌":"颂", "頎":"颀", "頏":"颃", "預":"预", "頑":"顽",
    "頒":"颁", "頓":"顿", "頗":"颇", "領":"领", "頜":"颌", "頡":"颉", "頤":"颐", "頦":"颏", "頭":"头", "頮":"颒", "頰":"颊", "頲":"颋",
    "頴":"颕", "頷":"颔", "頸":"颈", "頹":"颓", "頻":"频", "頽":"颓", "顆":"颗", "題":"题", "額":"额", "顎":"颚", "顏":"颜", "顒":"颙",
    "顓":"颛", "顔":"颜", "願":"愿", "顙":"颡", "顛":"颠", "類":"类", "顢":"颟", "顥":"颢", "顧":"顾", "顫":"颤", "顬":"颥", "顯":"显",
    "顰":"颦", "顱":"颅", "顳":"颞", "顴":"颧", "風":"风", "颭":"飐", "颮":"飑", "颯":"飒", "颱":"台", "颳":"刮", "颶":"飓", "颸":"飔",
    "颺":"飏", "颻":"飖", "颼":"飕", "飀":"飗", "飄":"飘", "飆":"飙", "飈":"飚", "飛":"飞", "飠":"饣", "飢":"饥", "飣":"饤", "飥":"饦",
    "飩":"饨", "飪":"饪", "飫":"饫", "飭":"饬", "飯":"饭", "飱":"飧", "飲":"饮", "飴":"饴", "飼":"饲", "飽":"饱", "飾":"饰", "飿":"饳",
    "餃":"饺", "餄":"饸", "餅":"饼", "餈":"糍", "餉":"饷", "養":"养", "餌":"饵", "餎":"饹", "餏":"饻", "餑":"饽", "餒":"馁", "餓":"饿",
    "餕":"馂", "餖":"饾", "餘":"余", "餚":"肴", "餛":"馄", "餜":"馃", "餞":"饯", "餡":"馅", "館":"馆", "餬":"糊", "餱":"糇", "餳":"饧",
    "餵":"喂", "餶":"馉", "餷":"馇", "餺":"馎", "餼":"饩", "餾":"馏", "餿":"馊", "饁":"馌", "饃":"馍", "饅":"馒", "饈":"馐", "饉":"馑",
    "饊":"馓", "饋":"馈", "饌":"馔", "饑":"饥", "饒":"饶", "饗":"飨", "饜":"餍", "饞":"馋", "饢":"馕", "馬":"马", "馭":"驭", "馮":"冯",
    "馱":"驮", "馳":"驰", "馴":"驯", "馹":"驲", "駁":"驳", "駐":"驻", "駑":"驽", "駒":"驹", "駔":"驵", "駕":"驾", "駘":"骀", "駙":"驸",
    "駛":"驶", "駝":"驼", "駟":"驷", "駡":"骂", "駢":"骈", "駭":"骇", "駰":"骃", "駱":"骆", "駸":"骎", "駿":"骏", "騁":"骋", "騂":"骍",
    "騅":"骓", "騌":"骔", "騍":"骒", "騎":"骑", "騏":"骐", "騖":"骛", "騙":"骗", "騤":"骙", "騫":"骞", "騭":"骘", "騮":"骝", "騰":"腾",
    "騶":"驺", "騷":"骚", "騸":"骟", "騾":"骡", "驀":"蓦", "驁":"骜", "驂":"骖", "驃":"骠", "驄":"骢", "驅":"驱", "驊":"骅", "驌":"骕",
    "驍":"骁", "驏":"骣", "驕":"骄", "驗":"验", "驚":"惊", "驛":"驿", "驟":"骤", "驢":"驴", "驤":"骧", "驥":"骥", "驦":"骦", "驪":"骊",
    "驫":"骉", "骯":"肮", "髏":"髅", "髒":"脏", "體":"体", "髕":"髌", "髖":"髋", "髮":"发", "鬆":"松", "鬍":"胡", "鬚":"须", "鬢":"鬓",
    "鬥":"斗", "鬧":"闹", "鬨":"哄", "鬩":"阋", "鬮":"阄", "鬱":"郁", "鬹":"鬶", "魎":"魉", "魘":"魇", "魚":"鱼", "魛":"鱽", "魢":"鱾",
    "魨":"鲀", "魯":"鲁", "魴":"鲂", "魷":"鱿", "魺":"鲄", "鮁":"鲅", "鮃":"鲆", "鮊":"鲌", "鮋":"鲉", "鮍":"鲏", "鮎":"鲇", "鮐":"鲐",
    "鮑":"鲍", "鮒":"鲋", "鮓":"鲊", "鮚":"鲒", "鮜":"鲘", "鮝":"鲞", "鮞":"鲕", "鮦":"鲖", "鮪":"鲔", "鮫":"鲛", "鮭":"鲑", "鮮":"鲜",
    "鮳":"鲓", "鮶":"鲪", "鮺":"鲝", "鯀":"鲧", "鯁":"鲠", "鯇":"鲩", "鯉":"鲤", "鯊":"鲨", "鯒":"鲬", "鯔":"鲻", "鯕":"鲯", "鯖":"鲭",
    "鯗":"鲞", "鯛":"鲷", "鯝":"鲴", "鯡":"鲱", "鯢":"鲵", "鯤":"鲲", "鯧":"鲳", "鯨":"鲸", "鯪":"鲮", "鯫":"鲰", "鯰":"鲶", "鯴":"鲺",
    "鯷":"鳀", "鯽":"鲫", "鯿":"鳊", "鰁":"鳈", "鰂":"鲗", "鰃":"鳂", "鰈":"鲽", "鰉":"鳇", "鰍":"鳅", "鰏":"鲾", "鰐":"鳄", "鰒":"鳆",
    "鰓":"鳃", "鰛":"鳁", "鰜":"鳒", "鰟":"鳑", "鰠":"鳋", "鰣":"鲥", "鰥":"鳏", "鰨":"鳎", "鰩":"鳐", "鰭":"鳍", "鰮":"鳁", "鰱":"鲢",
    "鰲":"鳌", "鰳":"鳓", "鰵":"鳘", "鰷":"鲦", "鰹":"鲣", "鰺":"鲹", "鰻":"鳗", "鰼":"鳛", "鰾":"鳔", "鱂":"鳉", "鱅":"鳙", "鱈":"鳕",
    "鱉":"鳖", "鱒":"鳟", "鱔":"鳝", "鱖":"鳜", "鱗":"鳞", "鱘":"鲟", "鱝":"鲼", "鱟":"鲎", "鱠":"鲙", "鱣":"鳣", "鱤":"鳡", "鱧":"鳢",
    "鱨":"鲿", "鱭":"鲚", "鱯":"鳠", "鱷":"鳄", "鱸":"鲈", "鱺":"鲡", "鳥":"鸟", "鳧":"凫", "鳩":"鸠", "鳬":"凫", "鳲":"鸤", "鳳":"凤",
    "鳴":"鸣", "鳶":"鸢", "鴆":"鸩", "鴇":"鸨", "鴉":"鸦", "鴒":"鸰", "鴕":"鸵", "鴛":"鸳", "鴝":"鸲", "鴞":"鸮", "鴟":"鸱", "鴣":"鸪",
    "鴦":"鸯", "鴨":"鸭", "鴯":"鸸", "鴰":"鸹", "鴴":"鸻", "鴻":"鸿", "鴿":"鸽", "鵂":"鸺", "鵃":"鸼", "鵐":"鹀", "鵑":"鹃", "鵒":"鹆",
    "鵓":"鹁", "鵜":"鹈", "鵝":"鹅", "鵠":"鹄", "鵡":"鹉", "鵪":"鹌", "鵬":"鹏", "鵮":"鹐", "鵯":"鹎", "鵰":"雕", "鵲":"鹊", "鵷":"鹓",
    "鵾":"鹍", "鶇":"鸫", "鶉":"鹑", "鶊":"鹒", "鶓":"鹋", "鶖":"鹙", "鶘":"鹕", "鶚":"鹗", "鶡":"鹖", "鶥":"鹛", "鶩":"鹜", "鶬":"鸧",
    "鶯":"莺", "鶲":"鹟", "鶴":"鹤", "鶹":"鹠", "鶺":"鹡", "鶻":"鹘", "鶼":"鹣", "鶿":"鹚", "鷀":"鹚", "鷁":"鹢", "鷂":"鹞", "鷄":"鸡",
    "鷊":"鹝", "鷓":"鹧", "鷖":"鹥", "鷗":"鸥", "鷙":"鸷", "鷚":"鹨", "鷥":"鸶", "鷦":"鹪", "鷫":"鹔", "鷯":"鹩", "鷲":"鹫", "鷳":"鹇",
    "鷴":"鹇", "鷸":"鹬", "鷹":"鹰", "鷺":"鹭", "鷽":"鸴", "鸇":"鹯", "鸌":"鹱", "鸏":"鹲", "鸕":"鸬", "鸘":"鹴", "鸚":"鹦", "鸛":"鹳",
    "鸝":"鹂", "鸞":"鸾", "鹵":"卤", "鹹":"咸", "鹺":"鹾", "鹼":"碱", "鹽":"盐", "麗":"丽", "麥":"麦", "麩":"麸", "麪":"面", "麫":"面",
    "麯":"曲", "麴":"曲", "麵":"面", "麼":"么", "麽":"么", "黃":"黄", "黌":"黉", "點":"点", "黨":"党", "黲":"黪", "黴":"霉", "黶":"黡",
    "黷":"黩", "黽":"黾", "黿":"鼋", "鼂":"鼌", "鼉":"鼍", "鼕":"冬", "鼴":"鼹", "齊":"齐", "齋":"斋", "齎":"赍", "齏":"齑", "齒":"齿",
    "齔":"龀", "齕":"龁", "齗":"龂", "齙":"龅", "齜":"龇", "齟":"龃", "齠":"龆", "齡":"龄", "齣":"出", "齦":"龈", "齧":"啮", "齪":"龊",
    "齬":"龉", "齲":"龋", "齶":"腭", "齷":"龌", "龍":"龙", "龎":"厐", "龐":"庞", "龔":"龚", "龕":"龛", "龜":"龟",
  };
  function toSimplified(text) {
    let out = String(text);
    for (const [from, to] of W2S_WORDS) out = out.split(from).join(to);
    let chars = "";
    for (const ch of out) chars += (S2S[ch] || ch);
    return chars;
  }

  // Wave 7: 反查表，用于「簡體自由文字 → 繁體介面」。
  // 背景：執行期狀態字串（長音訊 runner 的進度敘述、例外訊息等）無法窮舉，
  // 走的是 free-form 路徑。它們原本一律寫成簡體，結果繁體介面漏出「已处理」。
  //
  // 只做「字對字」反查，不反查 W2S_WORDS：那張表裡混了**用語差異**
  // （"語系"→"语言"、"採"→"采"），機械反轉會造出「語係」「埰用」這種錯字。
  // 一張簡→繁表天生多對一（"发" 可能是 發/髮），所以取 S2S 裡的第一個對應，
  // 並對已知會撞車的字改走 W2S_WORDS 的詞組優先替換來消歧義。
  const T2S_CHAR = {};
  for (const [t, s] of Object.entries(S2S)) {
    if (!(s in T2S_CHAR)) T2S_CHAR[s] = t;
  }
  // S2S 里有 139 个「一字对多繁」（钟←鈡/鍾/鐘、采←埰/寀/採、面←麪/麫/麵）。
  // 直接取第一条会造出「分鈡」「埰用」「裡麪」这类异体字，所以这些字一律
  // 用白名单指定常用正体，白名单优先于上面的自动反查。
  Object.assign(T2S_CHAR, {
    "钟": "鐘", "采": "採", "面": "面", "发": "發", "只": "只", "台": "臺",
    "里": "裡", "干": "乾", "后": "後", "系": "系", "了": "了", "么": "麼",
    "云": "雲", "胡": "胡", "姜": "薑", "于": "於", "余": "餘", "斗": "鬥",
    "谷": "谷", "征": "徵", "板": "板", "克": "克", "占": "佔", "并": "並",
    "汇": "匯", "舍": "捨", "松": "鬆", "致": "致", "制": "制", "志": "志",
    "划": "劃", "注": "註", "布": "佈", "个": "個", "几": "幾", "与": "與",
  });
  // 一簡對多繁的高頻字：先按詞組定死，剩下沒命中的才走字對字。
  const T2S_WORDS = [
    ["头发", "頭髮"], ["理发", "理髮"], ["发丝", "髮絲"], ["发型", "髮型"],
    ["发展", "發展"], ["发生", "發生"], ["发布", "發布"], ["发送", "發送"],
    ["发现", "發現"], ["发音", "發音"], ["出发", "出發"], ["引发", "引發"],
    ["干净", "乾淨"], ["干燥", "乾燥"],
    ["里面", "裡面"], ["这里", "這裡"], ["那里", "那裡"], ["哪里", "哪裡"],
    ["复杂", "複雜"], ["复制", "複製"], ["重复", "重複"], ["复数", "複數"],
    ["恢复", "恢復"],
    ["发", "發"], ["干", "乾"], ["里", "裡"], ["复", "複"],
  ].sort((a, b) => b[0].length - a[0].length);

  function toTraditional(text) {
    let out = String(text);
    // 1) 先把「一簡對多繁」的詞定死，避免字對字猜錯（发→發/髮、里→裡/里）。
    for (const [from, to] of T2S_WORDS) out = out.split(from).join(to);
    // 2) 剩下的逐字回退；已在第 1 步轉好的繁體字在 T2S_CHAR 裡沒有鍵，
    //    不會被二次改寫。
    let chars = "";
    for (const ch of out) chars += (T2S_CHAR[ch] || ch);
    return chars;
  }

  const EN_MAP = {
    "自動偵測": "Auto detect", "語言 自動": "Language: auto", "人數 自動": "Speakers: auto",
    "全部就緒": "All ready", "核心就緒": "Engine ready", "使用中": "In use",
    "共用元件": "Shared components", "尚未載入": "Not loaded", "已載入": "Loaded",
    "模型未安裝": "Model not installed", "未安裝": "Not installed", "已安裝": "Installed",
    "正在探测音频时长…": "Probing audio duration…", "準備中…": "Preparing…",
    "準備中": "Preparing", "識別中": "Transcribing", "完成": "Done",
    "已暫停": "Paused", "分段增量落盘": "incremental segment writes",
    "試跑前 5 分鐘": "Trial: first 5 minutes", "生效切分": "effective chunk",
    "預估常駐約": "estimated resident", "模型上限": "model ceiling",
    "中文（標準）": "Chinese (standard)",
    "繁體中文 TEA（乾淨聲音）": "Traditional Chinese TEA (clean speech)",
    "日本語": "Japanese", "台灣國語": "Taiwan Mandarin",
    "一般用途首選": "Best general-purpose choice",
    "CPU · OpenVINO INT8": "CPU · OpenVINO INT8", "GPU · CRISPASR": "GPU · CRISPASR",
    "GPU · chatllm Vulkan": "GPU · chatllm Vulkan",
    "待處理": "Pending", "辨識中": "Transcribing", "失敗": "Failed",
    "已取消": "Canceled", "上傳中": "Uploading",
    "服務端佇列": "Server-side queue",
    "模型輸出異常，此核心可能不相容，建議改用 CPU(OpenVINO) 核心。":
      "Engine output is abnormal; this backend may be incompatible. Try the CPU (OpenVINO) engine.",
  };
  // 執行期產生的自由文案（長音訊進度、續跑提示…）沒有字典條目，
  // 但格式穩定，用少量正則補上英文，避免英文介面整片中文。
  const EN_PATTERNS = [
    [/^已[處处]理\s*([\d.]+)\s*分[鐘钟]\s*·\s*(\d+)\s*段$/, (m, f) => `Processed ${m[1]} min · ${m[2]} segments`],
    [/^從第\s*([\d.]+)\s*分[鐘钟]續[跑钻]$/, (m, f) => `Resuming from ${m[1]} min`],
    [/^音[檔档][時时]長\s*([\d.]+)\s*分[鐘钟]，?[採采]用[流]?式分[段檔]?$/, (m, f) => `Audio is ${m[1]} min; using streaming segmentation`],
  ];

  function fallback(text) {
    const s = String(text);
    if (LOCALE === "en") {
      for (const [re, fn] of EN_PATTERNS) {
        const m = s.match(re);
        if (m) return fn(m, toSimplified(s));
      }
    }
    // 只有「確實含簡體專用字」才需要反轉；純繁體或純 ASCII 原樣返回，
    // 避免把使用者自由輸入（提示詞、歌詞）無謂地改寫。
    if (LOCALE === "hant") {
      for (const ch of s) if (ch in T2S_CHAR) return toTraditional(s);
      return s;
    }
    if (LOCALE === "hans") return toSimplified(s);
    return EN_MAP[s] || s;
  }

  // L() is the single entry point used by all 19 backend-string call sites.
  // It accepts both the new structured payloads and plain strings, so the
  // backend can be migrated incrementally without touching app.js.
  function L(value, keyHint) {
    if (value == null || value === "") return "";
    if (keyHint) { const v = t(keyHint); if (v) return v; }
    if (typeof value === "object") {
      if (Array.isArray(value)) return value.map(x => L(x)).join("");
      if (value.k) return renderMsg(value.k, value.v, value.t);
      if (value.parts) return value.parts.map(x => L(x)).filter(Boolean)
        .join(value.sep != null ? value.sep : "　");
      if (value.t != null) return fallback(value.t);
      return "";
    }
    return fallback(value);
  }

  // data-i18n 默认写 textContent，会连同元素内的子节点一起抹掉。带内嵌标记的
  // 元素（例如标题行里的版本号 <span class="chip">）要改用 data-i18n-lead：
  // 标签只替换「文字节点」，子元素原样保留并维持原有先后顺序。
  function applyLeadText(el, value) {
    if (value == null) return;
    let textNode = null;
    for (const n of Array.from(el.childNodes)) {
      if (n.nodeType === 3 && n.nodeValue.trim()) { textNode = n; break; }
    }
    if (textNode) {
      textNode.nodeValue = value + " ";
    } else {
      el.insertBefore(document.createTextNode(value + " "), el.firstChild);
    }
  }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach(el => { const v = t(el.dataset.i18n); if (v != null) el.textContent = v; });
    root.querySelectorAll("[data-i18n-html]").forEach(el => { const v = t(el.dataset.i18nHtml); if (v != null) el.innerHTML = v; });
    root.querySelectorAll("[data-i18n-ph]").forEach(el => { const v = t(el.dataset.i18nPh); if (v != null) el.setAttribute("placeholder", v); });
    root.querySelectorAll("[data-i18n-title]").forEach(el => { const v = t(el.dataset.i18nTitle); if (v != null) el.title = v; });
    root.querySelectorAll("[data-i18n-lead]").forEach(el => applyLeadText(el, t(el.dataset.i18nLead)));
  }

  // 語言切換後，除靜態 DOM 外還有「渲染時就把語言定死」的區塊（進度文案、
  // 後端資料驅動的自檢面板 / 端點狀態、執行期建立的 <option>）。它們由 app.js
  // 註冊回呼重繪 —— 放在這裡而不是只包在 app.js 的兩處呼叫點，是因為
  // 測試與未來的呼叫方都直接走 I18N.setLang()，漏掉包裝就會留下一半舊語言。
  let _afterLang = [];
  function onAfterLang(fn) { if (typeof fn === "function" && !_afterLang.includes(fn)) _afterLang.push(fn); }

  function setLang(uiLang) {
    LOCALE = normalize(uiLang);
    document.documentElement.lang = LOCALE === "en" ? "en" : (LOCALE === "hans" ? "zh-Hans" : "zh-Hant");
    apply(document);
    // 浏览器标签页 / Edge --app 窗口标题也走字典，否则切到简体或英文后
    // 系统窗口栏仍显示初始化时的繁体名。
    const appName = t("status.appName");
    if (appName) document.title = appName;
    for (const fn of _afterLang) {
      try { fn(LOCALE); } catch (e) { console.warn("[i18n] afterLang hook failed", e); }
    }
    return LOCALE;
  }

  window.I18N = { setLang, normalize, onAfterLang, t, L, toSimplified, toTraditional, apply, renderMsg,
                  get locale() { return LOCALE; },
                  get msgKeys() { return Object.keys(M); },
                  get tKeys() { return Object.keys(D); },
                  hasMsg(k) { return Object.prototype.hasOwnProperty.call(M, k); } };
})();
