/* ============================================================
   bridge.js — 統一前端橋接層 window.QwenAPI
   三版共用同一前端的關鍵：UI 只呼叫 QwenAPI.*，由本層偵測並路由：
     • web  : 本機/區網 HTTP（桌面 = 本機 server + Edge；端點 = LAN）
              方法打 /api/*，進度經 SSE /api/events 推回
     • mock : 純展示（file:// 或無後端，回範例資料供設計預覽）
   進度等長時間事件透過內建 event bus；web 模式由伺服器以 SSE 推送，
   mock 模式自行模擬 —— UI 兩者皆以 QwenAPI.on('progress', …) 接收。
   ============================================================ */
(function () {
  "use strict";

  // ── event bus ───────────────────────────────────────────
  const listeners = {};
  function on(ev, fn) { (listeners[ev] ||= new Set()).add(fn); return () => off(ev, fn); }
  function off(ev, fn) { listeners[ev]?.delete(fn); }
  function emit(ev, payload) { listeners[ev]?.forEach(fn => { try { fn(payload); } catch (e) { console.error(e); } }); }

  let MODE = "mock";
  const KEY = new URLSearchParams(location.search).get("k") || "";
  const isHttp = () => location.protocol === "http:" || location.protocol === "https:";

  // HTTP 時探測 /health：有後端 → web；純靜態/無 API → 降級 mock。
  async function probeHttp() {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 1500);
      const r = await fetch("/health", { signal: ctrl.signal, headers: authHeaders() });
      clearTimeout(t);
      return r.ok;
    } catch { return false; }
  }

  const ready = new Promise(resolve => {
    async function settle() {
      MODE = (isHttp() && await probeHttp()) ? "web" : "mock";
      console.info("[QwenAPI] transport =", MODE);
      if (MODE === "web") connectSSE();
      resolve(MODE);
    }
    if (document.readyState === "complete") setTimeout(settle, 0);
    else window.addEventListener("load", () => setTimeout(settle, 0), { once: true });
  });

  // ── web：SSE 進度/狀態串流 ──────────────────────────────
  function connectSSE() {
    try {
      const es = new EventSource("/api/events" + (KEY ? "?k=" + encodeURIComponent(KEY) : ""));
      es.onmessage = e => {
        try { const m = JSON.parse(e.data); if (m && m.event) emit(m.event, m.payload); }
        catch { /* 心跳/註解行，略過 */ }
      };
      es.onerror = () => { /* EventSource 會自動重連 */ };
    } catch (e) { console.warn("[QwenAPI] SSE 無法建立", e); }
  }

  // ── web：fetch 輔助 ─────────────────────────────────────
  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (KEY) h["Authorization"] = "Bearer " + KEY;
    return h;
  }
  function withKey(path) { return KEY ? path + (path.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(KEY) : path; }
  async function apiGet(path) {
    const r = await fetch(withKey(path), { headers: authHeaders() });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(withKey(path), {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();
  }

  // ════════════════════════════════════════════════════════
  // 公開 API（web 打 /api/*；mock 回範例資料）
  // 桌面獨有能力（pickFile）在 web/mock 回 null → UI 改用 <input> fallback
  // ════════════════════════════════════════════════════════
  const api = {
    on, off, _emit: emit,
    get mode() { return MODE; },
    ready,

    async getStatus() {
      if (MODE === "web") return apiGet("/api/status");
      return MOCK.status;
    },

    // Wave 2: model catalog / offline download list / local import.
    async getModelCatalog() {
      if (MODE !== "web") return MOCK.modelCatalog || { bundles: [], complete: [], incomplete: [] };
      return apiGet("/api/model-catalog");
    },
    async verifyBundle(id) {
      if (MODE !== "web") return { ok: true, message: "(preview)", checked: 0, total: 0 };
      return apiPost("/api/catalog/verify", { id });
    },
    async openBundleDir(id) {
      if (MODE !== "web") return { ok: true, path: "(preview mode)" };
      return apiPost("/api/catalog/open", { id });
    },
    async exportDownloadManifest(bundles) {
      if (MODE !== "web") return { ok: true, path: "(preview mode)", bytes: 0, bundle_count: 0 };
      return apiPost("/api/download-manifest", { bundles: bundles || [] });
    },
    async importLocalModel(dir, bundle) {
      if (MODE !== "web") return { ok: false, message: "preview mode" };
      return apiPost("/api/import-model", { dir: dir || "", bundle: bundle || "" });
    },

    async pickFile() { return null; },            // 瀏覽器環境一律用 <input type=file>
    async loadHintTxt() { return null; },

    // opts: {file, language, diarize, nSpeakers, align, hint, trial}
    async transcribe(opts) {
      if (MODE === "web") return webTranscribe(opts);
      return mockTranscribe(opts);
    },
    // Dry-run segmentation on the first 5 minutes. No ASR is executed.
    async vadPreview(opts) {
      if (MODE !== "web") return mockVadPreview(opts);
      if (!opts || !opts.file) throw new Error("請先選擇檔案");
      const fd = new FormData();
      fd.append("file", opts.file, opts.file.name);
      if (opts.vadThreshold) fd.append("vad_threshold", String(opts.vadThreshold));
      const r = await fetch(withKey("/api/vad-preview"), {
        method: "POST", body: fd, headers: authHeaders(),
      });
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { msg = (await r.json()).error?.message || msg; } catch {}
        throw new Error(msg);
      }
      return r.json();
    },
    async cancel() {
      if (MODE === "web") { try { await apiPost("/api/cancel", {}); } catch {} return true; }
      MOCK.cancelled = true; return true;
    },

    async openOutputDir() {
      if (MODE === "web") { try { await apiPost("/api/open-output", {}); } catch {} return true; }
      return false;
    },
    // 清掉 %TEMP% 下本程式遺留的上傳副本（崩潰/被強制結束留下來的）。
    async cleanupTemp() {
      if (MODE !== "web") return { ok: true, removed: 0, freed: 0, kept: 0, failed: 0 };
      return apiPost("/api/cleanup-temp", {});
    },
    async checkUpdate() {
      if (MODE === "web") { try { return await apiPost("/api/check-update", {}); } catch {} }
      window.open("https://github.com/1-1-2/QwenASRGUI/releases/latest", "_blank");
      return { ok: true };
    },

    async listDevices() {
      if (MODE === "web") return apiGet("/api/devices");
      return MOCK.devices;
    },
    // CrispASR 推理加速版本（Vulkan / CUDA / CPU）：偵測硬體 → 給推薦 + 選單
    async getAccel() {
      if (MODE === "web") return apiGet("/api/accel");
      return MOCK.accel;
    },
    async setAccel(variant) {
      if (MODE === "web") return apiPost("/api/accel", { variant });
      MOCK.accel.selected = variant;
      return { ok: true, variant, restartRequired: true,
               message: "（展示模式）已記住加速版本，重新啟動後套用" };
    },
    async setBackend(id) {
      if (MODE === "web") return apiPost("/api/backend", { index: id });
      return true;
    },
    async getModelOptions() {
      if (MODE === "web") return apiGet("/api/model-options");
      return MOCK.modelOptions;
    },
    // 基礎模式：用途清單（後端已套用硬體建議）
    async getBasicProfiles() {
      if (MODE === "web") return apiGet("/api/basic-profiles");
      return MOCK.basicProfiles;
    },
    async setModel(core, model) {
      if (MODE === "web") return apiPost("/api/model", { core, model });
      return { ok: true, core, model, arch: "（mock）", restartRequired: false, canLoadNow: true,
               message: `（展示模式）已選「${core} · ${model}」，點「下載並載入模型」開始` };
    },
    // 首次選定模型 → 就地下載並載入（進度走 SSE "progress"，完成走 "status"）
    async startLoad() {
      if (MODE === "web") return apiPost("/api/load", {});
      // mock：模擬載入完成
      setTimeout(() => { MOCK.status.modelReady = true; emit("status", { modelReady: true }); }, 1200);
      return { ok: true, loading: true };
    },
    async getLanguages() {
      if (MODE === "web") return apiGet("/api/languages");
      return MOCK.languages;
    },
    async getHealthCheck() {
      if (MODE === "web") return apiGet("/api/health-check");
      return MOCK.health;
    },

    async getSettings() {
      if (MODE === "web") return apiGet("/api/settings");
      return MOCK.settings;
    },
    async setSettings(patch) {
      if (MODE === "web") return apiPost("/api/settings", patch);
      Object.assign(MOCK.settings, patch); return MOCK.settings;
    },

    async getEndpoint() {
      if (MODE === "web") return apiGet("/api/endpoint");
      return MOCK.endpoint;
    },
    async toggleEndpoint(on_, port) {
      if (MODE === "web") return apiPost("/api/endpoint", { action: on_ ? "start" : "stop", port });
      MOCK.endpoint.running = on_; if (port) MOCK.endpoint.port = +port; return MOCK.endpoint;
    },
    async regenKey() {
      if (MODE === "web") return apiPost("/api/endpoint", { action: "regen" });
      MOCK.endpoint.key = "k_" + Math.random().toString(36).slice(2, 14);
      MOCK.endpoint.url = `http://${MOCK.endpoint.host}:${MOCK.endpoint.port}/?k=${MOCK.endpoint.key}`;
      return MOCK.endpoint;
    },

    // 對外臨時網址（Cloudflare）— 狀態/網址另經 SSE "tunnel" 事件即時推送
    async getTunnel() {
      if (MODE === "web") return apiGet("/api/tunnel");
      return MOCK.tunnel;
    },
    async toggleTunnel(on_) {
      if (MODE === "web") return apiPost("/api/tunnel", { action: on_ ? "start" : "stop" });
      MOCK.tunnel.running = on_;
      MOCK.tunnel.url = on_ ? "https://demo-fluffy-cloud.trycloudflare.com/?k=•••••" : "";
      MOCK.tunnel.status = on_ ? "ready" : "";
      setTimeout(() => emit("tunnel", MOCK.tunnel), 600);   // 模擬非同步就緒
      return { running: on_, url: "", status: on_ ? "建立中…" : "" };
    },
    // QR 圖網址：web 走後端 /api/qr（segno）；mock 無後端 → 空字串（前端顯示佔位）
    qrSrc(data) {
      if (!data || MODE !== "web") return "";
      return withKey("/api/qr?d=" + encodeURIComponent(data));
    },

    async getBatch() {
      if (MODE === "web") {
        // Wave 11：以前這裡在請求失敗時回 items:[]，等於把「一次網路抖動」
        // 放大成「整個佇列被清空」。曾因 keep-alive 殘留 body 讓
        // GET /api/batch 得到 501，前端 8 個檔瞬間歸零。現在寧可拋錯，
        // 由 refreshBatch() 保留既有狀態，絕不用空陣列覆蓋佇列。
        return await apiGet("/api/batch");
      }
      return MOCK.batch;
    },
    // Wave 14：上傳版 batchAdd 已移除。批次一律走 batchAddPath（本機路徑
    // 直引，不複製、不上傳）；後端 /api/batch/add 亦已停用（410）。
    // Wave 11：貼上本機絕對路徑，後端直接引用，不產生任何副本。
    async batchAddPath(path, opts) {
      if (MODE !== "web") return { ok: true, item: null, mock: true };
      return apiPost("/api/batch/add-path", { path, opts: opts || {} });
    },
    // Wave 12: native OS multi-select picker -> absolute paths (no copy).
    async pickFiles() {
      if (MODE !== "web") return { ok: true, paths: [], supported: false, mock: true };
      return apiPost("/api/pick-files", {});
    },
    async batchStart(opts) {
      if (MODE !== "web") return { ok: true, mock: true };
      // opts = 開始當下音檔頁的設定快照；後端據此鎖定整條佇列的執行參數，
      // 之後即使使用者改動滑桿也不會讓同一批檔案跑在兩套參數上。
      return apiPost("/api/batch/start", { opts: opts || {} });
    },
    async batchCancel() {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/batch/cancel", {});
    },
    async batchRemove(id) {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/batch/remove", { id });
    },
    async batchRetry(id) {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/batch/retry", { id });
    },
    // Queue ordering: only pending items can be moved (see batch_queue.move).
    async batchMove(id, index) {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/batch/move", { id, index });
    },
    async batchPromote(id) {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/batch/move", { id, promote: true });
    },
    async batchClear() {
      if (MODE !== "web") return { ok: true, removed: 0, mock: true };
      return apiPost("/api/batch/clear", {});
    },
    async addBatchFiles() { return this.getBatch(); },
    async runBatch() { return true; },
    // H1: server-side run log (bounded ring buffer, SSE "log" for live rows).
    async getLogs(limit) {
      if (MODE !== "web") return { items: [], total: 0, capacity: 300 };
      try { return await apiGet("/api/logs" + (limit ? "?limit=" + limit : "")); }
      catch { return { items: [], total: 0, capacity: 300 }; }
    },
    async clearLogs() {
      if (MODE !== "web") return { ok: true, mock: true };
      return apiPost("/api/logs/clear", {});
    },
    // Wave 12: manual model unload (free the compiled weights between runs).
    async releaseModel() {
      if (MODE !== "web") return { ok: true, released: true, mock: true };
      return apiPost("/api/release-model", {});
    },
    // C1 / C2: optional exports from a finished SRT.
    async refinePunctuation(path) {
      if (MODE !== "web") return { ok: false, message: "preview mode" };
      return apiPost("/api/refine-punct", { path });
    },
    async exportHandout(path, format) {
      if (MODE !== "web") return { ok: false, message: "preview mode" };
      return apiPost("/api/export-handout", { path, format: format || "md" });
    },
  };

  // ── web 轉錄：POST /api/transcribe（進度走 SSE）──────────
  //  兩種來源：
  //   · opts.file → multipart 上傳（拖放。瀏覽器沙箱只給 File 物件，
  //     服務端必須把整份內容複製到 %TEMP%，請求結束再刪）
  //   · opts.path → 本機路徑直引（點擊選檔走原生對話框。零複製、零等待，
  //     也不會有「上傳到一半」這種狀態）
  async function webTranscribe(opts) {
    if (!opts.file && !opts.path) throw new Error("請先選擇檔案");
    if (!opts.file) {
      emit("progress", { pct: 1, status: "準備中…" });
      return apiPost("/api/transcribe-path", {
        path: opts.path,
        name: opts.name || null,
        language: opts.language || null,
        align: !!opts.align,
        diarize: !!opts.diarize,
        nSpeakers: opts.nSpeakers || null,
        hint: opts.hint || "",
        trial: !!opts.trial,
        vadThreshold: opts.vadThreshold || null,
      });
    }
    const fd = new FormData();
    fd.append("file", opts.file, opts.file.name);
    if (opts.language) fd.append("language", opts.language);
    fd.append("align", opts.align ? "1" : "0");
    fd.append("diarize", opts.diarize ? "1" : "0");
    if (opts.nSpeakers && opts.nSpeakers !== "auto") fd.append("n_speakers", String(opts.nSpeakers));
    if (opts.hint) fd.append("hint", opts.hint);
    if (opts.trial) fd.append("trial", "1");
    emit("progress", { pct: 3, status: "上傳中…" });
    const r = await fetch(withKey("/api/transcribe"), { method: "POST", body: fd, headers: authHeaders() });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();   // {segments, srtPath}
  }

  // ════════════════════════════════════════════════════════
  // mock：純展示資料（讓設計稿在瀏覽器中完全可互動）
  // ════════════════════════════════════════════════════════
  // 与后端 ui_text.msg() 同形的结构：k=稳定键，v=变量，t=繁体兜底。
  const M = (k, v) => ({ k, v: v || {}, t: (window.I18N ? I18N.t(k, v) : k) || k });

  const MOCK = {
    cancelled: false,
    status: { modelReady: true, backend: "GPU · CRISPASR（Vulkan）", device: "NVIDIA GeForce RTX", version: "webview 0.1", appName: "聲音辨識小工具", hasAnyModel: false, selectedReady: false },
    settings: { scale: 100, format: "srt", vocab: "s2twp", mirror: "", ffmpeg: "", theme: "light", uiLang: "繁體中文", vad: 0.5, chunkSecs: 0 },
    endpoint: { running: true, host: "192.168.1.20", port: 11435, key: "k_8x2pf3qd7m1c", url: "" },
    tunnel: { running: false, url: "", status: "" },
    basicProfiles: {
      tier: "gpu", accel: "Vulkan",
      hardware: M("hw.discrete", { names: "NVIDIA GeForce RTX 4070" }),
      current: { core: "Qwen", model: "Qwen3-ASR-1.7B Q8 (CRISPASR)" },
      profiles: [
        { key: "zh", title: M("profile.zh.title"), desc: M("profile.zh.desc"),
          note: null, core: "Qwen", model: "Qwen3-ASR-1.7B Q8 (CRISPASR)",
          arch: "GPU · CRISPASR（Vulkan）", present: true, selected: true },
        { key: "tea", title: M("profile.tea.title"), desc: M("profile.tea.desc"),
          note: "⚠️ 背景配樂較重的段落或歌曲可能整段無輸出。", core: "Qwen",
          model: "TEA-ASR-1.1 台灣國語 Q8 (CRISPASR)", arch: "GPU · CRISPASR（Vulkan）",
          present: false, selected: false },
        { key: "ja", title: M("profile.ja.title"), desc: M("profile.ja.desc"), note: null,
          core: "Qwen", model: "Qwen3-ASR-1.7B 日語動漫 Q8 (CRISPASR)",
          arch: "GPU · CRISPASR（Vulkan）", present: false, selected: false },
        { key: "twmix", title: M("profile.twmix.title"), desc: M("profile.twmix.desc"),
          note: null, core: "Whisper (Breeze)", model: "Breeze Q8 (精確)",
          arch: "GPU · CRISPASR（Vulkan）", present: false, selected: false },
      ],
    },
    accel: {
      ok: true, selected: "vulkan", recommended: "vulkan",
      reason: M("accel.reason.nvidia", { names: "NVIDIA GeForce RTX 4070", cuda: "CUDA 13" }),
      latest: "0.8.32", installed: { version: "0.8.32", variant: "vulkan" }, needsDownload: false,
      hardware: { gpus: [{ vendor: "nvidia", name: "NVIDIA GeForce RTX 4070", vram_mb: 8192 }] },
      options: [
        { key: "vulkan", label: M("accel.opt.vulkan.label"), size_mb: 34, recommended: true, suitable: true, note: null,
          desc: M("accel.opt.vulkan.desc") },
        { key: "cuda13", label: M("accel.opt.cuda13.label"), size_mb: 484, recommended: false, suitable: true, note: null,
          desc: M("accel.opt.cuda13.desc") },
        { key: "cuda12", label: M("accel.opt.cuda12.label"), size_mb: 690, recommended: false, suitable: true,
          note: null, desc: M("accel.opt.cuda12.desc") },
        { key: "cpu", label: M("accel.opt.cpu.label"), size_mb: 8, recommended: false, suitable: true, note: null,
          desc: M("accel.opt.cpu.desc") },
        { key: "cpu-legacy", label: M("accel.opt.cpu-legacy.label"), size_mb: 8, recommended: false,
          suitable: true, note: null, desc: M("accel.opt.cpu-legacy.desc") },
      ],
    },
    devices: {
      devices: [
        { kind: "cpu", name: "AMD Ryzen 5 9600X 6-Core", note: "使用中" },
        { kind: "gpu", name: "NVIDIA GeForce RTX 4070", note: "8.0 GB 可用" },
      ],
      diag: { level: "info", text: "（展示資料）GPU 由 CrispASR 偵測；實機會列出可用的獨立顯示卡與可用記憶體。" },
    },
    modelOptions: {
      cores: [
        { label: "Qwen", models: [
          { label: "Qwen3-ASR-0.6B", backend: "openvino", arch: "CPU · OpenVINO INT8", note: "" },
          { label: "Qwen3-ASR-1.7B INT8", backend: "openvino", arch: "CPU · OpenVINO INT8", note: "" },
          { label: "Qwen3-ASR-1.7B Q4 (CRISPASR/Vulkan)", backend: "crispasr", arch: "GPU · CRISPASR（Vulkan）", note: "" },
          { label: "Qwen3-ASR-1.7B Q8 (CRISPASR/Vulkan)", backend: "crispasr", arch: "GPU · CRISPASR（Vulkan）", note: "" },
          { label: "Qwen3-ASR-1.7B Q8（Vulkan · 相容）", backend: "chatllm", arch: "GPU · chatllm Vulkan", note: "⚠️ chatllm 核心在部分 AMD 內顯／APU 有已知相容問題，且核心二進位未隨安裝包提供（保留供既有使用者向下相容）；若遇當機或無輸出，建議改用「Qwen · CRISPASR/Vulkan」核心。" },
        ]},
        { label: "Whisper (Breeze)", models: [
          { label: "Breeze Q4 (輕量)", backend: "crispasr", arch: "GPU · CRISPASR（Vulkan）" },
          { label: "Breeze Q5 (標準)", backend: "crispasr", arch: "GPU · CRISPASR（Vulkan）" },
          { label: "Breeze Q8 (精確)", backend: "crispasr", arch: "GPU · CRISPASR（Vulkan）" },
        ]},
      ],
      current: { core: "Qwen", model: "Qwen3-ASR-0.6B" },
      activeArch: "CPU · OpenVINO INT8",
    },
    languages: {
      languages: [
        { label: "自動偵測", value: "" }, { label: "Chinese", value: "Chinese" },
        { label: "English", value: "English" }, { label: "Japanese", value: "Japanese" },
        { label: "Korean", value: "Korean" }, { label: "Cantonese", value: "Cantonese" },
      ],
    },
    // 自檢資料的形狀必須跟 webview_backend.health_check() 一致：label / detail
    // 都是 {k, v, t} 結構化訊息，不是字串。先前這裡放的是純字串，離屏渲染
    // 因此看不到「英文介面仍顯示中文」這個真實缺陷（真實後端早就回傳結構化）。
    health: {
      summary: { red: 0, yellow: 3, ok: true }, activeBackend: "openvino",
      cores: [
        { label: M("health.group.ov"), backend: "openvino", items: [
          { key: "model", label: M("health.ov.model", { size: "0.6B" }), status: "green",
            detail: M("state.downloaded") },
          { key: "vad", label: M("health.ov.vad"), status: "green", detail: M("state.builtin") },
          { key: "fa", label: M("health.ov.fa"), status: "yellow", detail: M("state.notDownloadedFa") },
          { key: "diar", label: M("health.ov.diar"), status: "yellow", detail: M("state.notDownloadedDiar") },
        ]},
        { label: M("health.group.crisp", { variant: "Vulkan" }), backend: "crispasr", items: [
          { key: "core", label: M("health.crisp.core", { version: "v0.3.1", variant: "Vulkan" }), status: "green", detail: M("state.downloaded") },
          { key: "breeze", label: M("health.crisp.breeze", { quant: "Q5" }), status: "green",
            detail: M("state.downloaded") },
          { key: "qwen", label: M("health.crisp.qwen", { quant: "Q8" }), status: "yellow",
            detail: M("state.notDownloaded") },
          { key: "fa", label: M("health.crisp.fa", { quant: "Q5" }), status: "green",
            detail: M("state.downloaded") },
          { key: "diar", label: M("health.crisp.diar"), status: "yellow", detail: M("state.notDownloaded") },
        ]},
      ],
      shared: [
        { key: "ffmpeg", label: M("health.shared.ffmpeg"), status: "green",
          detail: { k: "", v: {}, t: "已偵測：C:/ffmpeg/ffmpeg.exe" } },
        { key: "diar_shared", label: M("health.shared.diar"), status: "yellow",
          detail: M("health.shared.diarMissing") },
      ],
    },
    batch: {
      summary: { done: 3, total: 7 },
      items: [
        { name: "ep01_intro.mp3", status: "done", progress: 1 },
        { name: "ep02_guest.m4a", status: "done", progress: 1 },
        { name: "會議_0612.wav", status: "done", progress: 1 },
        { name: "podcast_long.mp4", status: "running", progress: 0.42 },
        { name: "lecture_3.wav", status: "pending", progress: 0 },
        { name: "interview.m4a", status: "pending", progress: 0 },
        { name: "broken_amd.wav", status: "failed", progress: 0, error: "模型輸出異常，此核心可能不相容，建議改用 CPU(OpenVINO) 核心。" },
      ],
    },
    segments: [
      { start: 3.0, end: 7.2, speaker: 1, text: "大家好，今天我們來聊聊本地語音辨識這個主題。" },
      { start: 7.4, end: 11.8, speaker: 2, text: "對啊，最吸引我的是資料完全不用上傳雲端，隱私有保障。" },
      { start: 12.0, end: 16.5, speaker: 1, text: "沒錯，而且這套支援說話者分離，會議記錄整理起來特別方便。" },
      { start: 16.8, end: 21.0, speaker: 2, text: "時間軸對齊也很實用，字幕可以直接匯出成 SRT 或純文字。" },
      { start: 21.3, end: 26.0, speaker: 1, text: "如果顯卡相容，用 GPU 核心速度會再快上好幾倍。" },
      { start: 26.2, end: 31.0, speaker: 2, text: "那我們下半段就來實際示範一次完整的轉錄流程吧。" },
    ],
  };
  MOCK.endpoint.url = `http://${MOCK.endpoint.host}:${MOCK.endpoint.port}/?k=${MOCK.endpoint.key}`;

  // mock 字級：把每行文字（去標點）平均灑進該行時間，近似 FA 字級時間軸，
  // 讓卡拉OK模式在純展示（file:// / 無後端）下也能完整預覽。真實後端回的
  // words 為 FA 不等距時間，但前端渲染只讀 words → mock 與實機同一條路徑。
  MOCK.segments.forEach(s => {
    const chars = [...s.text].filter(c => !"，。？！；：、…—·".includes(c));
    const dur = chars.length ? (s.end - s.start) / chars.length : 0;
    s.words = chars.map((ch, i) => ({
      start: s.start + i * dur, end: s.start + (i + 1) * dur, text: ch,
    }));
  });

  async function mockTranscribe() {
    MOCK.cancelled = false;
    const steps = [
      [12, "載入音訊…"], [25, "語音分段（VAD）…"], [44, "辨識中 · 第 3/8 段"],
      [68, "辨識中 · 第 5/8 段"], [86, "時間軸對齊…"], [100, "完成"],
    ];
    for (const [pct, status] of steps) {
      if (MOCK.cancelled) throw new Error("已取消");
      await sleep(420);
      emit("progress", { pct, status });
    }
    return {
      segments: MOCK.segments, totalSegments: MOCK.segments.length,
      previewLimit: 200, previewTruncated: false,
      srtPath: "subtitles/B.srt", trial: false, truncated: false,
      duration: 31.0,
      peaks: Array.from({ length: 200 }, (_, i) =>
        Math.round((0.25 + 0.7 * Math.abs(Math.sin(i / 7))) * 1000) / 1000),
    };
  }
  async function mockVadPreview() {
    emit("progress", { pct: 50, status: "切分預檢 50%（展示模式）" });
    await sleep(300);
    return {
      cancelled: false, previewSeconds: 300, previewMinutes: 5,
      totalFrames: 9375, groupCount: 42,
      lengths: { p50: 4.2, p90: 8.6, p99: 11.4, max: 12.1 },
      overLimitRatio: 0.024, shortRatio: 0.048, silenceRatio: 0.31,
      estimatedAsrCalls: 42, estimatedAsrCallsPerHour: 504,
      maxChunkSecs: 10, threshold: 0.5,
      effectiveChunkSecs: 10, modelCeilingSecs: 10, verdict: "green",
    };
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  window.QwenAPI = api;
})();
