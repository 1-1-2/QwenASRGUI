/* ============================================================
   i18n.js — 介面語言（繁體中文 / 简体中文 / English）
   用法：在 HTML 元素掛 data-i18n="key"（textContent）、
        data-i18n-html（innerHTML）、data-i18n-ph（placeholder）、
        data-i18n-title（title）。呼叫 window.I18N.setLang(uiLang) 切換。
   字典僅涵蓋靜態介面字串；後端動態訊息（進度/錯誤）維持原語言。
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
        "file.previewOk": ["切分合理", "切分合理", "Good split"],
    "file.streamingNote": ["可選增強：字級時間軸、說話人分離；失敗不影響字幕", "可选增强：词级时间轴、说话人分离；失败不影响字幕", "Optional: word timing & speakers. Failures never affect subtitles"],
    "file.enrichMissing": ["模型未安裝，請到模型頁下載", "模型未安装，请到模型页下载", "Model not installed — see the Model page"],
    "file.previewWarn": ["建議調整", "建议调整", "Adjust suggested"],
    "file.previewTruncated": ["只顯示前 %1 條字幕，完整字幕已存檔：", "只显示前 %1 条字幕，完整字幕已存档：", "Showing the first %1 subtitles; full transcript saved to:"],
    "set.segMoved": ["VAD 靈敏度、每段最長秒數、切分預檢與 5 分鐘試跑已移至「音檔」頁，與實際任務放在一起。", "VAD 灵敏度、每段最长秒数、切分预检与 5 分钟试跑已移至「音频」页，与实际任务放在一起。", "VAD sensitivity, segment length, segmentation preview and the 5-minute trial now live on the Audio page, next to the actual task."],
    "common.langAuto": ["語言 自動", "语言 自动", "Lang Auto"],
    "common.spkAuto": ["人數 自動", "人数 自动", "Count Auto"],

    "batch.add": ["加入檔案", "添加文件", "Add files"],
    "batch.run": ["全部開始", "全部开始", "Start all"],
    "welcome.text": ["初次使用：請先在「模型與裝置」頁選擇並下載辨識模型，完成後即可開始轉寫。",
                     "初次使用：请先在「模型与设备」页选择并下载识别模型，完成后即可开始转写。",
                     "First time here: pick and download a recognition model on the Model page, then start transcribing."],
    "welcome.go": ["前往模型頁", "前往模型页", "Go to Model page"],
    "file.refine": ["跨段標點修正", "跨段标点修正", "Refine punctuation"],
    "file.refineHint": ["另存 *.punct.srt，補齊跨段標點，不修改原始字幕",
                        "另存 *.punct.srt，补齐跨段标点，不修改原始字幕",
                        "Save a *.punct.srt copy with cross-segment punctuation; the original is untouched"],
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

    "record.mic": ["麥克風", "麦克风", "Microphone"],
    "record.refresh": ["重新整理", "刷新", "Refresh"],
    "record.liveTitle": ["即時字幕", "实时字幕", "Live captions"],
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

    "model.health": ["系統自檢", "系统自检", "System check"],
    "model.recheck": ["重新檢查", "重新检查", "Re-check"],
    "model.pick": ["選擇模型", "选择模型", "Choose a model"],
    "model.modeBasic": ["基礎", "基础", "Basic"],
    "model.modeAdv": ["進階", "进阶", "Advanced"],
    "model.core": ["推理核心", "推理核心", "Inference core"],
    "model.model": ["模型", "模型", "Model"],
    "model.load": ["下載並載入模型", "下载并载入模型", "Download & load model"],
    "model.loading": ["下載／載入中…", "下载／载入中…", "Downloading / loading…"],
    "model.goto": ["前往語音轉文字", "前往语音转文字", "Go to transcription"],
    "model.needRestart": ["切換核心需重新啟動", "切换核心需重启", "Switching core needs restart"],
    "model.nModels": ["{n} 種模型可選", "{n} 种模型可选", "{n} models"],
    "model.devices": ["偵測到的裝置", "检测到的设备", "Detected devices"],
    "status.ready": ["模型已就緒", "模型已就绪", "Model ready"],
    "status.loading": ["載入模型中…", "载入模型中…", "Loading model…"],
    "status.needModel": ["尚未載入模型", "尚未载入模型", "No model loaded"],
    "model.accel": ["推理加速版本", "推理加速版本", "Acceleration build"],
    "model.diag": ["診斷", "诊断", "Diagnostics"],

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
  };

  function idx() { return LOCALE === "hans" ? 1 : (LOCALE === "en" ? 2 : 0); }
  function t(key, vars) {
    const e = D[key];
    if (!e) return null;
    let out = e[idx()] || e[0];
    if (vars) for (const k in vars) out = out.split("{" + k + "}").join(String(vars[k]));
    return out;
  }

  // ── H6/H7: localise backend-generated strings ────────────────────────
  // The backend emits Chinese labels for model cards, health rows, progress
  // status, arch tags and profile descriptions. They used to stay Chinese
  // after switching the UI to English. Rule:
  //   * hant -> verbatim (source of truth)
  //   * hans -> per-character Traditional->Simplified mapping
  //   * en   -> phrase dictionary; unlisted proper nouns / model names stay
  //            verbatim (project convention: generated names are not translated)
  // A keyHint wins over both, for strings that have a proper dictionary entry.
  const S2S_PAIRS = (
    "體体灣湾國国語语簡简轉转換换檔档錄录製制設设選选項项預预檢检測测試试處处" +
    "識识別别說说話话數数據据載载優优準准確确錯错誤误於于後后裡里當当顯显" +
    "開开啟启閉闭關关連连線线網网絡络傳传輸输儲储敗败狀状態态執执紀纪總总" +
    "進进餘余時时間间聲声頻频長长組组邊边續续從从頭头結结純纯講讲義义標标" +
    "點点導导驗验證证碼码鏡镜來来備备內内積积報报讀读寫写刪删變变數数邏逻" +
    "輯辑緒绪隊队復复異异過过濾滤對对齊齐詞词級级軸轴豐丰構构圖图參参離离" +
    "類类聚聚憶忆積积環环裝装權权問问題题許许應应負責责實实際际產产業业" +
    "課课學学習习溫温會会議议訪访談谈質质詢询講讲座座場场景景適适適适" +
    "優优異异發发現现處处檔档歷历經经驗验靜静音音辯辩認认讀读寫写" +
    "隨随機机變变數数續续傳传統统檔档案案內内建建置置預预覽览" +
    "釐厘釐厘覆复蓋盖範范圍围滾滚動动緩缓衝冲裁裁切切極极短短佔占" +
    "預预估估駐驻體体積积執执緒绪瀏浏覽览器器窗窗口口標标題题欄栏" +
    "配配顏颜淺浅深深主主題题圖图示示切切換换裝装幀帧縮缩放放" +
    "檢检查查更更新新版版本本說说明明檔档案案清清單单匯汇入入校校" +
    "驗验完完成成失失敗败取取消消儲储存存續续跑跑斷断點点" +
    "詞词彙汇轉转換换詞词級级時时間间軸轴逐逐字字高高亮亮" +
    "說说話话者者分分離离著著色色聚聚類类自自動动偵侦測测");
  const S2S = {};
  for (let i = 0; i + 1 < S2S_PAIRS.length; i += 2) S2S[S2S_PAIRS[i]] = S2S_PAIRS[i + 1];
  function toSimplified(text) {
    let out = "";
    for (const ch of text) out += (S2S[ch] || ch);
    return out;
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
  function L(text, keyHint) {
    if (!text) return "";
    if (keyHint) { const v = t(keyHint); if (v) return v; }
    if (LOCALE === "hant") return String(text);
    if (LOCALE === "hans") return toSimplified(String(text));
    return EN_MAP[String(text)] || String(text);
  }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach(el => { const v = t(el.dataset.i18n); if (v != null) el.textContent = v; });
    root.querySelectorAll("[data-i18n-html]").forEach(el => { const v = t(el.dataset.i18nHtml); if (v != null) el.innerHTML = v; });
    root.querySelectorAll("[data-i18n-ph]").forEach(el => { const v = t(el.dataset.i18nPh); if (v != null) el.setAttribute("placeholder", v); });
    root.querySelectorAll("[data-i18n-title]").forEach(el => { const v = t(el.dataset.i18nTitle); if (v != null) el.title = v; });
  }

  function setLang(uiLang) {
    LOCALE = normalize(uiLang);
    document.documentElement.lang = LOCALE === "en" ? "en" : (LOCALE === "hans" ? "zh-Hans" : "zh-Hant");
    apply(document);
    return LOCALE;
  }

  window.I18N = { setLang, normalize, t, L, toSimplified, apply,
                  get locale() { return LOCALE; } };
})();
