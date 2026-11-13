/* ============================================================
   app.js — UI 邏輯（與後端解耦，全程只透過 window.QwenAPI）
   ============================================================ */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const API = window.QwenAPI;
  // i18n 取字（含 {n} 等簡單插值）；無字典或無此鍵時回退預設值 def。
  function T(key, def, vars) {
    let s = (window.I18N && I18N.t(key)) || def || key;
    if (vars) for (const k in vars) s = s.replace("{" + k + "}", vars[k]);
    return s;
  }

  // ── 導覽切換 ────────────────────────────────────────────
  // 標題一律現查字典（view.<name>），不在模組載入時求值 —— 否則切換語言後
  // 標題列還是舊語言。字典缺鍵時退回空字串，由 HTML 的預設值接手。
  let _curView = "file";
  function viewTitle(name) { return (window.I18N && I18N.t("view." + name)) || ""; }
  function refreshViewTitle() { $("#view-title").textContent = viewTitle(_curView); }
  function switchView(name) {
    _curView = name;
    $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("active", v.dataset.view === name));
    $("#view-title").textContent = viewTitle(name);
    $("#view-ctx").textContent = "";
    if (name === "batch") renderBatch();
    if (name === "model") { renderModel(); renderDownloadCenter(); }
    // 設定頁的斷點條數會隨任務變化，每次進來重讀一次，避免顯示開機當下的舊值。
    if (name === "settings") refreshCheckpoints();
    if (name === "record") { enumerateMics(); refreshMicPerm(); }   // 列舉裝置 + 麥克風權限狀態
  }

  /* ── Download center ──────────────────────────────────────────────────
     Renders models.json status. Unknown byte sizes / hashes are shown as
     "unknown" on purpose; size_hint values are estimates and never used for
     progress math. */
  function fmtBytes(v) {
    if (v === null || v === undefined) return T("common.unknown", "未知");
    if (v < 1024) return v + " B";
    const u = ["KiB", "MiB", "GiB"];
    let n = v, i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < 2);
    return n.toFixed(1) + " " + u[i];
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  async function renderDownloadCenter() {
    const host = $("#catalog-list");
    if (!host) return;
    try {
      const st = await API.getModelCatalog();
      const bundles = st.bundles || [];
      const doneN = st.complete ? st.complete.length : 0;

      // 摘要只留兩項「真的會拿來判斷」的資訊：完成度、磁碟佔用。
      //  · 「待補 m 組」= total − done，與「已完整」是同一件事的兩面 → 刪
      //  · 「根目錄」太長又不可操作 → 移到 title（hover 才看）
      //  · 「鏡像」是狀態且需要能改 → 做成右上角可點 chip，點了跳設定頁
      const summary = $("#catalog-summary");
      if (summary) {
        summary.textContent = T("dl.summaryShort", null,
          { done: doneN, total: bundles.length, used: fmtBytes(st.total_bytes_on_disk) });
        summary.title = st.model_root || "";
      }
      const mirrorChip = $("#catalog-mirror");
      if (mirrorChip) {
        if (st.mirror) {
          mirrorChip.hidden = false;
          mirrorChip.textContent = T("dl.mirror", null, { name: st.mirror });
          mirrorChip.title = T("dl.mirrorJump", "前往設定修改鏡像");
          mirrorChip.style.cursor = "pointer";
        } else mirrorChip.hidden = true;
      }

      const sel = $("#catalog-import-bundle");
      if (sel) {
        sel.innerHTML = bundles.map(b =>
          `<option value="${esc(b.id)}">${esc(window.I18N ? I18N.L(b.label) : b.label)}</option>`).join("");
      }
      host.innerHTML = bundles.map(b => {
        const rt = b.runtimeReady;
        const missN = b.total_count - b.present_count;
        const badge = (rt === true || (rt == null && b.complete))
          ? `<span class="chip" style="color:var(--ok,#1a7f45)">${T("dl.badgeComplete", "完整")}</span>`
          : (rt === false || b.complete === false)
            ? `<span class="chip chip-muted">${T("dl.badgeMissing", null,
                { missing: missN, total: b.total_count })}</span>`
            : "";
        const rtNote = (rt != null && b.runtimeNote)
          ? `<div class="hint" style="margin-top:4px;color:var(--ink-2)">${esc(I18N.L(b.runtimeNote))}</div>`
          : "";
        const hint = v => {
          const t = String(v || "").trim().replace(/^[约約]\s*/, "");
          return t ? T("dl.estimated", null, { size: esc(t) }) : "";
        };
        const approx = b.size_hint ? ` <span class="chip chip-muted">${hint(b.size_hint)}</span>` : "";
        const files = b.files.map(f => {
          const size = f.size_bytes !== null && f.size_bytes !== undefined
            ? fmtBytes(f.size_bytes)
            : (f.size_hint ? hint(f.size_hint) : T("dl.sizeUnknown", "大小未知"));
          const sha = f.sha256_expected
            ? (f.sha256_ok === true ? T("dl.shaOk", null)
               : (f.sha256_ok === false ? T("dl.shaBad", null) : T("dl.shaUnverified", null)))
            : T("dl.shaUnknown", null);
          const state = f.present ? "" : ("✗ " + esc(f.problem || T("dl.missing", "缺失")));
          return `<div style="display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:12px;font-family:var(--font-mono)">
            <span style="width:14px;color:${f.present ? "var(--ok,#1a7f45)" : "var(--warn,#b26a00)"}">${f.present ? "✓" : "✗"}</span>
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
            <span style="color:var(--ink-2)">${size}</span>
            <span style="color:var(--ink-2);min-width:96px;text-align:right">${sha}</span>
            <span style="color:var(--ink-2);min-width:150px;text-align:right">${state}</span>
          </div>`;
        }).join("");
        // 缺失時直接說「還差幾個、怎麼補」，取代原本的「同自檢判斷」標籤。
        const missLine = missN > 0
          ? `<div class="hint" style="margin:6px 0 0">${T("dl.needFiles", null,
              { n: missN, size: b.size_hint ? hint(b.size_hint) : "" })}</div>`
          : "";
        return `<details class="dl-group"${rt === false ? " open" : ""}>
          <summary>
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(window.I18N ? I18N.L(b.label) : b.label)}</span>
            ${approx}${badge}<span class="chev" aria-hidden="true">›</span>
          </summary>
          ${rtNote}
          <div style="margin-top:6px;color:var(--ink-2);font-size:12px">${T("dl.target", null, {
            dir: esc(b.target_dir), provider: esc(b.provider || ""),
            optional: b.optional ? T("dl.optional", " · 選用") : "" })}</div>
          <div class="dl-actions">
            <button class="btn btn-ghost btn-sm" data-dl-open="${esc(b.id)}">${T("dl.openDir", "開啟目錄")}</button>
            <button class="btn btn-ghost btn-sm" data-dl-verify="${esc(b.id)}">${T("dl.verify", "校驗檔案")}</button>
            <span class="hint" data-dl-msg="${esc(b.id)}"></span>
          </div>
          ${missLine}
          ${files}
        </details>`;
      }).join("") || `<div style="color:var(--ink-2)">${T("dl.empty", "無資料")}</div>`;
    } catch (e) {
      host.innerHTML = `<div style="color:var(--warn,#b26a00)">${T("dl.readFailed", null, { err: esc(e.message || e) })}</div>`;
    }
  }

  // 下載組的兩個實用入口：開目錄、真正校驗（SHA256）。
  // 校驗是顯式動作——它要讀完整個檔案，不能塞進每次刷新。
  $("#catalog-list") && $("#catalog-list").addEventListener("click", async e => {
    const o = e.target.closest("[data-dl-open]");
    if (o) {
      const r = await API.openBundleDir(o.dataset.dlOpen);
      if (r && r.ok === false) logLine("✕ " + (r.message || "open failed"));
      return;
    }
    const v = e.target.closest("[data-dl-verify]");
    if (!v) return;
    const id = v.dataset.dlVerify;
    const tip = document.querySelector(`[data-dl-msg="${id}"]`);
    v.disabled = true;
    if (tip) tip.textContent = T("dl.verifying", "校驗中…");
    try {
      const r = await API.verifyBundle(id);
      if (tip) {
        tip.textContent = r.ok
          ? T("dl.verifyOk", null, { n: r.checked })
          : T("dl.verifyBad", null,
              { n: (r.bad || []).length, m: (r.missing || []).length });
      }
      if (r.ok) await renderDownloadCenter();
    } catch (err) {
      if (tip) tip.textContent = String(err.message || err);
    } finally {
      v.disabled = false;
    }
  });

  // 原本的「查看系統自檢」跳轉已移除：兩塊都在同一頁，跳過去還要再展開一次，
  // 實際幫助有限；改為每組各自的「開啟目錄 / 校驗檔案」。
  $("#btn-catalog-refresh") && $("#btn-catalog-refresh").addEventListener("click", renderDownloadCenter);
  $("#btn-catalog-manifest") && $("#btn-catalog-manifest").addEventListener("click", async () => {
    const msg = $("#catalog-msg");
    try {
      const r = await API.exportDownloadManifest([]);
      if (msg) msg.textContent = T("dl.manifestWritten", null,
        { path: r.path, size: fmtBytes(r.bytes), count: r.bundle_count });
    } catch (e) { if (msg) msg.textContent = T("dl.manifestFailed", null, { err: e.message || e }); }
  });
  $("#btn-catalog-import") && $("#btn-catalog-import").addEventListener("click", async () => {
    const msg = $("#catalog-msg");
    const dir = ($("#catalog-import-dir") || {}).value || "";
    const bundle = ($("#catalog-import-bundle") || {}).value || "";
    if (!dir) { if (msg) msg.textContent = T("dl.needDir", "請先填入手動下載的資料夾路徑"); return; }
    try {
      const r = await API.importLocalModel(dir, bundle);
      if (msg) msg.textContent = r.ok
        ? T("dl.imported", null, { msg: r.message, n: r.copied })
        : T("dl.importRejected", null, { msg: r.message });
      await renderDownloadCenter();
    } catch (e) { if (msg) msg.textContent = T("dl.importFailed", null, { err: e.message || e }); }
  });
  $("#nav").addEventListener("click", e => {
    const btn = e.target.closest(".nav-item");
    if (btn) switchView(btn.dataset.view);
  });

  // ── 狀態列 ──────────────────────────────────────────────
  async function refreshStatus() {
    const s = await API.getStatus();
    _lastStatus = s;
    // 批次頁的「本次執行設定」要顯示模型與推理方式，兩者只在 status 裡；
    // 這裡補一次重繪，值沒變時 renderBatchConfig 自己會跳過重建。
    try { renderBatchConfig(); } catch (e) {}
    const el = $("#model-status");
    el.classList.toggle("loading", !s.modelReady);
    $(".t", el).textContent = s.modelReady ? T("status.ready", "模型已就緒")
      : (s.loading ? T("status.loading", "載入模型中…") : T("status.needModel", "尚未載入模型"));
    // 版本徽章：純數字版本前綴 v（如 v1.0.9）；已含文字者（如 webview 0.1）原樣顯示
    if (s.version) $("#app-version").textContent = /^\d/.test(s.version) ? "v" + s.version : s.version;
    // J5: a page reload must still show a TLS downgrade that happened earlier.
    if (s.sslInsecure) showGlobalWarning(I18N.t("warn.ssl"));
    // G5: first-run guidance, shown once until the user dismisses it.
    const hint = $("#first-run-hint");
    if (hint && s.firstRun && !localStorage.getItem("qwenasr_welcomed")) {
      hint.hidden = false; hint.style.display = "flex"; I18N.apply(hint);
    }
  }

  function showGlobalWarning(msg) {
    const box = $("#global-warning"), text = $("#global-warning-text");
    if (!box || !text) return;
    text.textContent = msg;
    box.hidden = false;
    box.style.display = "flex";
  }
  function dismissWelcome() {
    const hint = $("#first-run-hint");
    if (hint) { hint.hidden = true; hint.style.display = "none"; }
    try { localStorage.setItem("qwenasr_welcomed", "1"); } catch {}
  }
  $("#btn-welcome-go") && $("#btn-welcome-go").addEventListener("click", () => {
    dismissWelcome(); switchView("model");
  });
  $("#btn-welcome-close") && $("#btn-welcome-close").addEventListener("click", dismissWelcome);
  $("#btn-warning-dismiss") && $("#btn-warning-dismiss").addEventListener("click", () => {
    const box = $("#global-warning"); if (box) { box.hidden = true; box.style.display = "none"; }
  });
  // Backend warnings carry a structured key+vars payload (ui_text.py); the
  // verbatim Hant text is kept in the run log for diagnostics.
  API.on("warning", p => {
    if (!p) return;
    const msg = I18N.L(p.message);
    if (msg) showGlobalWarning(msg);
  });

  // ════════════════════════════════════════════════════════
  // 音檔轉字幕
  // ════════════════════════════════════════════════════════
  // ── H2: one button state machine for the whole UI ───────────────────
  // Previously every handler set .disabled by hand; a second long task could
  // leave buttons enabled and let the user start a competing transcription.
  // UI_BUSY holds the active scopes; syncActionButtons() is the only writer.
  const UI_BUSY = new Set();       // "transcribe" | "model" | "preview" | "batch"
  let batchItems = [];             // declared here so syncActionButtons is safe
  let batchRunning = false, batchMockRunning = false, batchActiveIdx = -1;
  // 本輪佇列在「開始」時鎖定的執行參數（由後端 snapshot 帶回）。執行中要
  // 顯示的就是它，而不是使用者此刻可能已經改過的滑桿值。
  let batchRunOpts = {};
  let _lastStatus = null;          // 最近一次 getStatus，供同步渲染的地方取用
  // 同名檔案策略的目前值（設定頁寫入、批次頁唯讀顯示）。
  let _outNaming = "suffix";
  // 佇列級耗時（後端從按下「開始」起算，續跑會累加）。與單檔 elapsedSec 不同。
  let batchElapsed = 0;
  let batchDragId = null;          // 正在被拖動的項目 id（null = 沒在拖）
  let logExpanded = false;   // Wave 14：上傳通道已移除，不再有 batchUploadErrors
  let lastBatchDetail = null;      // Wave 11：批次頁目前任務的進度明細

  function markBusy(scope, on) {
    if (on) UI_BUSY.add(scope); else UI_BUSY.delete(scope);
    syncActionButtons();
  }

  // ── Wave 16：關窗前攔截 ─────────────────────────────────────────────
  // 關掉視窗會讓 launcher 走 os._exit(0)，正在跑的辨識被硬切；雖然檢查點
  // 每 16 秒寫一次、續跑不會從頭，但使用者不知道會丟掉最後一小段。
  // 這裡只在「真的有任務在跑」時攔，空閒關窗保持零摩擦。
  // "model"（載入/下載模型）刻意不攔：那類操作本身可恢復，且下載常跑很久，
  // 每次關窗都彈一次確認會變成噪音。
  const _GUARD_SCOPES = ["transcribe", "batch"];
  function _busyScopes() {
    return [...UI_BUSY].filter(s => _GUARD_SCOPES.includes(s));
  }
  window.addEventListener("beforeunload", e => {
    if (!_busyScopes().length) return;
    // 標準寫法：preventDefault + returnValue。Chromium 只會顯示固定文案，
    // 無法自訂——能說清的內容放在關窗後端的優雅取消裡（保存檢查點與 rich）。
    e.preventDefault();
    e.returnValue = "";
    return "";
  });
  function syncActionButtons() {
    const busy = UI_BUSY.size > 0;
    const run = $("#btn-run");
    if (run) {
      // 執行中：同一顆按鈕變「取消」且保持可按 —— 單檔轉錄動輒數十分鐘，
      // 沒有中止入口時使用者只能結束程式（斷點雖然會存，但當下等於卡死）。
      const canceling = UI_BUSY.has("transcribe");
      run.disabled = canceling ? false : (busy || !picked);
      run.dataset.mode = canceling ? "cancel" : "run";
      run.classList.toggle("is-cancel", canceling);
      const span = run.querySelector("[data-i18n]");
      if (span && window.I18N) {
        span.dataset.i18n = canceling ? "file.cancel" : "file.run";
        I18N.apply(run);
      }
    }
    const prev = $("#btn-vad-preview");
    if (prev && !UI_BUSY.has("preview")) prev.disabled = busy || !picked;
    const loadTxt = $("#btn-load-txt"); if (loadTxt) loadTxt.disabled = busy;
    // Wave 14：「加入檔案」按鈕已移除（點拖放框即選檔），此處不再需要 disable。
    const clear = $("#btn-batch-clear");
    if (clear) clear.disabled = busy || !batchItems.some(
      i => ["done", "failed", "canceled"].includes(i.status));
    const batchRun = $("#btn-batch-run");
    if (batchRun) {
      batchRun.disabled = UI_BUSY.has("transcribe") || UI_BUSY.has("model");
      const span = batchRun.querySelector("[data-i18n]");
      if (span && window.I18N) {
        span.dataset.i18n = batchRunning ? "batch.cancel" : "batch.run";
        I18N.apply(batchRun);
      }
      batchRun.title = batchRunning ? T("batch.cancelAll", "取消佇列")
                              : T("batch.runAll", "全部開始");
    }
  }

  let picked = null;             // {path?, name, file?} 或 null
  const drop = $("#drop"), hiddenFile = $("#hidden-file");

  function setFile(f) {
    picked = f;
    if (!f) { drop.classList.remove("has-file"); renderDropEmpty(); syncActionButtons(); return; }
    drop.classList.add("has-file");
    const meta = f.sizeSec ? T("batch.seconds", null, { n: f.sizeSec })
      : (f.size ? (f.size / 1048576).toFixed(1) + " MB" : "");
    // 模式標籤：兩種來源都能接續，差別在「要不要重傳」。
    //   有 path（原生選檔）＝直接引用本機檔案，重新選同一個檔案即可接續；
    //   只有 File 物件（拖放／瀏覽器選檔）＝暫存副本，靠內容哈希認得同一個
    //   檔案，但重新拖入時整份要再上傳一次。
    const byPath = !f.file && !!f.path;
    const tag = byPath ? T("file.modeLocal", "本機直引") : T("file.modeDragChip", "拖放副本");
    const tagNote = byPath ? T("file.modeLocalNote", "可中斷接續")
                           : T("file.modeDragNote", "可接續 · 需重新上傳");
    drop.innerHTML = `<span class="file-chip">${escapeHtml(f.name)}
      ${meta ? `<span class="meta">${meta}</span>` : ""}
      <span class="chip-mode ${byPath ? "is-ok" : "is-warn"}">${escapeHtml(tag)} · ${escapeHtml(tagNote)}</span>
      <button class="x" title="${T("batch.remove", "移除")}">✕</button></span>`;
    drop.querySelector(".x").addEventListener("click", ev => { ev.stopPropagation(); setFile(null); });
    syncActionButtons();
  }
  function renderDropEmpty() {
    drop.innerHTML = `<div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg></div>
      <div class="big">${T("file.dropBigHtml", "拖曳音訊到此，或<b>點擊選擇檔案</b>")}</div>
      <div class="sub">${T("file.dropSubHtml", "支援 mp3 / wav / m4a / 影片音軌")}</div>
      <div class="drop-note">${T("file.modeHint", "中斷後可接續 · 拖放需重新拖入（會重新上傳）")}</div>`;
  }

  // 桌面：原生對話框（零複製）；非 Windows / 預覽模式：fallback 到 <input type=file>
  //   這條路徑的差別值得說清楚：
  //     · 點擊 → 原生對話框回傳絕對路徑 → /api/transcribe-path 直接引用，
  //       不複製、不上傳、不會在 %TEMP% 留下整份音檔副本。
  //     · 拖放 → 瀏覽器只給 File 物件（Chromium 不交出真實路徑），只能整份
  //       上傳到 %TEMP%\asr_web_*，請求結束再刪。
  drop.addEventListener("click", async () => {
    if (drop.classList.contains("has-file")) return;
    if (API.mode !== "web") { hiddenFile.click(); return; }
    let r = null;
    try { r = await API.pickFiles(); } catch (e) { r = null; }
    if (!r || !r.supported) { hiddenFile.click(); return; }  // 非 Windows 才回退
    if (!r.paths || !r.paths.length) return;                 // 使用者取消，不要再彈一次
    const p = r.paths[0];
    setFile({ name: p.split(/[\\/]/).pop() || p, path: p, native: true });
  });
  hiddenFile.addEventListener("change", e => { if (e.target.files[0]) setFile(e.target.files[0]); });
  ["dragover", "dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hot"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hot"); }));
  drop.addEventListener("drop", e => { const f = e.dataTransfer.files[0]; if (f) setFile(f); });

  // 讀入 TXT：瀏覽器沙箱用隱藏 <input type=file> + FileReader 讀文字進提示框
  const _txtInput = document.createElement("input");
  _txtInput.type = "file"; _txtInput.accept = ".txt,text/plain"; _txtInput.style.display = "none";
  document.body.appendChild(_txtInput);
  _txtInput.addEventListener("change", () => {
    const f = _txtInput.files && _txtInput.files[0]; if (!f) return;
    const r = new FileReader();
    r.onload = () => { $("#hint-box").value = String(r.result || ""); };
    r.readAsText(f, "utf-8");
    _txtInput.value = "";
  });
  $("#btn-load-txt").addEventListener("click", () => _txtInput.click());

  // 轉錄
  let running = false;
  // 使用者是否按了「取消」：用來把「已取消」講成正常結束，而不是紅字錯誤。
  let cancelRequested = false;
  $("#btn-run").addEventListener("click", async () => {
    // 執行中同一顆按鈕變「取消」——與批次頁「全部開始 → 取消佇列」一致，
    // 不新增第二顆按鈕。取消會讓 Runner 在語音組邊界停下來並保存斷點。
    if ($("#btn-run").dataset.mode === "cancel") {
      if (cancelRequested) return;
      cancelRequested = true;
      const span = $("#btn-run").querySelector("[data-i18n]");
      if (span) { span.dataset.i18n = "file.canceling"; I18N.apply($("#btn-run")); }
      logLine(T("file.cancelSent", "已送出取消，正在保存已完成的部分…"));
      try { await API.cancel(); } catch (e) { logLine("✕ " + (e.message || e)); }
      return;
    }
    if (!picked || running) return;
    running = true;
    cancelRequested = false;
    markBusy("transcribe", true);
    $("#result").hidden = true;
    showProgress(0, T("run.preparing", "準備中…"));
    resetLive();
    logLine(T("run.started", null, { name: picked.name }));
    try {
      // 原生選檔（有 path、沒有 File）→ 本機路徑直引，不複製；
      // 拖放（File 物件）→ multipart 上傳到 %TEMP%，請求結束即刪。
      const byPath = !picked.file && !!picked.path;
      const res = await API.transcribe({
        path: byPath ? picked.path : null,
        name: picked.name,
        file: byPath ? null : (picked.file || picked),
        language: $("#sel-lang").value || null,
        ...diarOpts($("#sel-diar") ? $("#sel-diar").value : "off"),
        align: $("#sw-align").checked,
        hint: $("#hint-box").value.trim(),
        trial: $("#sw-trial") ? $("#sw-trial").checked : false,
      });
      renderResult(res);
      logLine(T("run.finished", null, {
        n: res.totalSegments ?? res.segments.length,
        srt: res.srtPath ? T("run.finishedSrt", null, { path: res.srtPath }) : "",
      }));
      if (res.previewTruncated) {
        logLine(I18N.t("file.previewTruncated").replace("%1", res.previewLimit) + " " + res.srtPath);
      }
      if (res.trial) logLine(T("run.trialNote"));
      // 「開啟輸出資料夾」不需要等有輸出才亮：資料夾本來就在，
      // 使用者常常是先想確認上次的檔在哪才轉檔。
      $("#btn-save-sub").disabled = false;
    } catch (err) {
      showProgress(0, "");
      $("#progress").hidden = true;
      if (cancelRequested) {
        // 取消不是失敗。能不能接續由後端依來源回話（點選＝可接續；
        // 拖放＝副本已刪、重拖從頭），前端不再自行加一句以免兩邊說法不一。
        logLine("⏹ " + (err.message || err));
      } else {
        logLine("✕ " + (err.message || err));
      }
    } finally {
      running = false;
      cancelRequested = false;
      markBusy("transcribe", false);
    }
  });

  // 進度分派：模型就地載入 → 模型頁；服務端批次 → 批次頁自己的進度區；
  // mock 批次 → 當前批次列；否則 → 單檔進度條。
  // Wave 11／問題 8：後端現在會給批次任務的進度事件標 scope="batch"，
  // 這樣即使前端 batchRunning 尚未同步（start 後 SSE 還沒到），也不會
  // 把批次進度誤畫到「音檔轉字幕」頁。
  API.on("progress", ({ pct, status, scope }) => {
    const isBatch = scope === "batch" || batchRunning;
    if (_modelLoading) {
      setModelProg(pct, status);
    } else if (isBatch) {
      applyBatchProgress(status);
    } else if (batchMockRunning && batchActiveIdx >= 0) {
      batchItems[batchActiveIdx].progress = pct / 100;
      updateBatchRow(batchActiveIdx);
    } else {
      showProgress(pct, status);
    }
  });

  // Capabilities arrive before a transcription starts. In long-audio streaming
  // mode word-level alignment and diarization are unavailable, so disable the
  // switches instead of silently ignoring checked boxes.
  API.on("capabilities", (cap) => {
    // Word timing and speaker labels are optional post-processing, not
    // discarded features. Only grey a switch when its model is genuinely
    // absent; an omitted field leaves the control untouched (no "undefined").
    [["#sw-align", cap.alignAvailable], ["#sel-diar", cap.diarAvailable]]
      .forEach(([sel, available]) => {
        const el = $(sel);
        if (!el || available === undefined) return;
        el.disabled = !available;
        // 開關的容器是 label，下拉的容器是 .ctrl —— 兩者都要能降透明度
        const box = el.closest("label") || el.closest(".ctrl");
        if (box) box.style.opacity = available ? "" : "0.45";
        if (!available && box) box.title = I18N.t("file.enrichMissing");
      });
    // Text is a data-i18n node so it follows later language switches; the
    // event only controls visibility (I18N.apply() refreshes the wording).
    const note = $("#cap-note");
    if (note) { note.hidden = false; I18N.apply(note.parentElement || document); }
    // Older / partial backends may omit these numbers. Without guards the UI
    // used to render "undefineds" and "NaN GB", which looks like a broken build.
    const eff = Number(cap.effectiveChunkSecs);
    const ceil = Number(cap.modelCeilingSecs);
    const ram = Number(cap.estimatedRamMB);
    if ($("#seg-effective")) {
      $("#seg-effective").textContent =
        (Number.isFinite(eff) ? eff + "s" : "—") +
        T("run.modelCeiling", null, { n: Number.isFinite(ceil) ? ceil : "—" });
    }
    if ($("#seg-ram")) {
      $("#seg-ram").textContent = Number.isFinite(ram)
        ? (ram / 1024).toFixed(1) + " GB" : "—";
    }
  });

  // Wave 7: 进度文案在渲染时就把语言定死了，切语言后若不重绘，"已處理"
  // 会留在简体/英文界面上。这里记住最近一次的原始输入，setLang 之后由
  // redrawProgressText() 用新语言重新渲染。
  let lastProgress = { pct: null, status: null };
  let lastProgressDetail = null;
  let lastModelProgressStatus = null;   // 提前声明：redrawProgressText() 在初始化期也可能被调用
  function showProgress(pct, status) {
    $("#progress").hidden = false;
    lastProgress.pct = pct;
    if (status !== undefined) lastProgress.status = status;
    $("#prog-bar").style.width = pct + "%";
    $("#prog-pct").textContent = Math.round(pct) + "%";
    if (status != null) $("#prog-status").textContent =
      window.I18N ? I18N.L(status) : status;
  }
  function redrawProgressText() {
    if (lastProgress.status != null) {
      $("#prog-status").textContent = window.I18N
        ? I18N.L(lastProgress.status) : lastProgress.status;
    }
    if (lastProgressDetail) {
      if (lastProgressDetail.scope === "batch" || batchRunning)
        renderBatchDetail(lastProgressDetail);
      else renderProgressDetail(lastProgressDetail);
    }
    const mps = $("#model-prog-status");
    if (mps && lastModelProgressStatus != null) {
      mps.textContent = window.I18N ? I18N.L(lastModelProgressStatus) : lastModelProgressStatus;
    }
    // 辨識語言下拉的選項是執行期建立的，不掛 data-i18n，切換語言後不會自己更新。
    // 這裡就地重繪，避免「自動偵測」在簡體／英文介面上留著繁體。
    ["#sel-lang", "#rec-lang"].forEach(id => {
      const sel = $(id); if (!sel) return;
      [...sel.options].forEach(o => {
        if (o._literal) { o.textContent = o._literal; return; }   // 語言專名不翻譯
        if (o._src != null) o.textContent = I18N.L(o._src);
        else if (o.dataset.src) o.textContent = I18N.L(o.dataset.src);
      });
    });
    // 批量列表與狀態 chip 也是執行期產生的，切換語言後要整批重繪。
    if (typeof renderBatch === "function") renderBatch();
    // Wave 11：摺疊面板的摘要是翻譯字串，切語言要跟著換。
    if (typeof syncSegSummary === "function") syncSegSummary();
    // 自檢面板、端點頁狀態等內容來自後端（走 I18N.L 動態翻譯），
    // 不重抓就永遠停在切換前的語言。這裡在下一輪事件循環重繪，避免
    // 使用者連點語言下拉時互相打斷。
    if (typeof _relocalizeAsync === "function") setTimeout(_relocalizeAsync, 0);
  }

  // 切換語言後，重新渲染「資料來自後端、文字由 I18N.L 決定」的區塊。
  async function _relocalizeAsync() {
    try { await renderHealth(); } catch (e) {}
    try { await renderEndpoint(); } catch (e) {}
  }
  // 所有語言切換（含測試直接呼叫 I18N.setLang）都走這裡，避免漏掉重繪。
  if (window.I18N && I18N.onAfterLang) {
    I18N.onAfterLang(() => {
      redrawProgressText();
      relocalizeMics();
      if (typeof renderBasic === "function") renderBasic();
      // 佇列列與「本次執行設定」卡片都是 JS 拼出來的字串，靜態的 data-i18n
      // 掃不到，語言一換就得自己重建。
      if (typeof renderBatch === "function") renderBatch();
      _relocalizeAsync();
    });
  }

  /* ── Wave 4: two progress views + live subtitles ───────────────────────
     progress-detail carries processed/total audio seconds, group count, ETA
     and phase. The percentage bar above stays as-is; this fills the time /
     segment ratio below it and also drives the batch queue summary. */
  function fmtDur(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const pad = n => String(n).padStart(2, "0");
    return (h ? h + ":" : "") + pad(m) + ":" + pad(r);
  }
  // Wave 11／問題 1：進度條上下方不再重複。
  // 上方 .meta 的 prog-status 由後端 progress 事件給「辨識中 · 第 5/8 段」，
  // 已經包含階段與段數；舊版下方又畫 prog-phase「識別中」、prog-groups
  // 「5 段」，等於把同一句話講兩次。現在下方只保留上方沒有的數值
  // （已處理時間 / 剩餘時間），PHASE 與段數不再另畫。
  function renderProgressDetail(d) {
    const total = Number(d.totalSec) || 0, done = Number(d.doneSec) || 0;
    const elTime = $("#prog-time"), elEta = $("#prog-eta");
    if (elTime) elTime.textContent = T("run.processed", null,
      { done: fmtDur(done), total: total ? fmtDur(total) : "—" });
    if (elEta) elEta.textContent = d.etaSec == null
      ? T("run.remainingUnknown", "剩餘 —")
      : T("run.remaining", null, { eta: fmtDur(d.etaSec) });
  }
  // Wave 13：批次頁「目前檔案」區塊的資料來源。
  // longform_runner._emit() 現在附 elapsedSec / speedX，這裡直接用實測值，
  // 不再靠前端用 Date.now() 猜耗時與倍速。
  let batchRunStartAt = 0;      // 本機觀察到的佇列起始（後端明細缺席時才用）
  let batchActiveId = null;
  function trackBatchTiming() {
    const a = batchItems.find(i => i.status === "running");
    if (a) {
      if (batchActiveId !== a.id) {
        batchActiveId = a.id;
        if (!batchRunStartAt) batchRunStartAt = Date.now();
      }
    } else {
      batchActiveId = null;
      if (!batchRunning) batchRunStartAt = 0;
    }
  }
  function batchElapsedSec(d) {
    if (d && Number(d.elapsedSec) > 0) return Number(d.elapsedSec);
    return batchRunStartAt ? (Date.now() - batchRunStartAt) / 1000 : 0;
  }
  /** 把同一份 progress-detail 畫在批次頁自己的進度區（Wave 11／問題 8）。 */
  function renderBatchDetail(d) {
    lastBatchDetail = d || null;
    // 後端 streaming 事先不知道「待處理檔」的時長；目前檔的總音訊秒數
    // 記回本地 item，讓佇列列與「總音訊」統計至少累積已跑過的檔案。
    if (d && Number(d.totalSec) > 0) {
      const i = batchItems.findIndex(x => x.status === "running");
      if (i >= 0) batchItems[i].durationSec = Number(d.totalSec);
    }
    // Wave 14／需求 3：續跑時把「本輪起點已完成的段數」記在 item 上，
    // 換檔／取消後佇列列仍能顯示正確的累計段數，不會回退成 0。
    if (d && Number(d.segments) > 0) {
      const i = batchItems.findIndex(x => x.status === "running");
      if (i >= 0) batchItems[i].segmentsDone = Number(d.segments);
    }
    refreshBatchActiveText();
    renderCurrentFile();
    batchSummary();
    // Wave 13：state 事件會先清空 lastBatchDetail 並重繪佇列，明細是之後
    // 才到的；若不在這裡補刷「處理中」那一列，它的已用／剩餘會一直停在 —。
    const i = batchItems.findIndex(x => x.status === "running");
    if (i >= 0) updateBatchRow(i);
  }
  function renderCurrentFile() {
    const box = $("#batch-cur"); if (!box) return;
    const active = batchItems.find(i => i.status === "running");
    const d = lastBatchDetail;
    if (!active && !d) { box.hidden = true; return; }
    box.hidden = false;
    const pct = active
      ? Math.round((active.progress || 0) * 100)
      : (d && Number(d.totalSec) > 0
         ? Math.round((Number(d.doneSec) || 0) / Number(d.totalSec) * 100) : 0);
    const bar = $("#batch-cur-bar"), pctEl = $("#batch-cur-pct"),
          timeEl = $("#batch-cur-time"), detailEl = $("#batch-cur-detail");
    if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    if (pctEl) pctEl.textContent = pct + "%";
    if (timeEl) {
      const total = d ? Number(d.totalSec) || 0 : 0;
      timeEl.textContent = total
        ? fmtDur(Number(d.doneSec) || 0) + " / " + fmtDur(total) : "—";
    }
    if (detailEl) {
      const parts = [];
      // Wave 14／需求 3：斷點續跑的第一個進度事件只帶「已完成的段數」，
      // 若照舊拿 groups（本輪新增段數）會顯示成「已識別 0 段」。
      const segDone = Number(d && d.segments);
      if (segDone > 0) parts.push(T("batch.groups", null, { n: segDone }));
      else if (d && d.groups != null) parts.push(T("batch.groups", null, { n: d.groups }));
      const el = batchElapsedSec(d);
      if (el > 0) parts.push(T("batch.fileElapsed", null, { t: fmtDur(el) }));
      // 兩個倍率並列：全程＝累計已處理÷累計牆鐘（含開頭載模型，偏保守）；
      // 近 5 分鐘＝滑動窗口（反映當下）。後端在執行未滿 5 分鐘時不給
      // speed5m，這裡就不顯示 —— 不拿短窗口冒充「5 分鐘」。
      if (d && Number(d.speedX) > 0)
        parts.push(T("batch.rateLong", null, { x: Number(d.speedX).toFixed(1) }));
      if (d && Number(d.speed5m) > 0)
        parts.push(T("batch.rate5m", null, { x: Number(d.speed5m).toFixed(1) }));
      if (d && d.etaSec != null)
        parts.push(T("batch.fileRemaining", null, { t: fmtDur(d.etaSec) }));
      detailEl.textContent = parts.length ? parts.join(" · ")
        : T("batch.noDetail", "—");
    }
  }
  API.on("progress-detail", (d) => {
    if (!d) return;
    lastProgressDetail = d;
    // scope 由後端標記：批次任務的進度只進批次頁，不污染單檔進度條。
    if (d.scope === "batch" || batchRunning) renderBatchDetail(d);
    else renderProgressDetail(d);
    // Queue view: keep the overall percentage list in sync without a second
    // polling loop. The batch page renders the same number in its summary.
    if (!batchRunning && batchMockRunning && batchActiveIdx >= 0 && batchItems[batchActiveIdx]) {
      batchItems[batchActiveIdx].detail = d;
      updateBatchRow(batchActiveIdx);
      batchSummary();
    }
  });

  // 批次任務的 progress 事件：只更新「目前處理中」那一列的狀態文字，
  // 百分比仍以服務端 batch SSE 的 item.progress 為準（後端這些廣播常是
  // pct=0 的階段提示，直接拿來覆寫會把進度條打回 0）。
  function applyBatchProgress(status) {
    if (status == null) return;
    const idx = batchItems.findIndex(i => i.status === "running");
    if (idx < 0) return;
    batchItems[idx].statusText = status;
    updateBatchRow(idx);
    batchSummary();
  }

  const LIVE_MAX = 200;
  let liveFollow = true, liveCount = 0;
  function resetLive() {
    const panel = $("#live-panel"), list = $("#live-list");
    if (list) { list.innerHTML = ""; }
    if (panel) panel.hidden = false;
    liveFollow = true; liveCount = 0;
  }
  $("#btn-live-follow") && $("#btn-live-follow").addEventListener("click", () => {
    liveFollow = true;
    const list = $("#live-list"); if (list) list.scrollTop = list.scrollHeight;
  });
  const liveListEl = $("#live-list");
  if (liveListEl) {
    liveListEl.addEventListener("scroll", () => {
      const nearBottom = liveListEl.scrollHeight - liveListEl.scrollTop - liveListEl.clientHeight < 40;
      if (!nearBottom) liveFollow = false;
    });
  }
  API.on("segments", (p) => {
    if (!p || !Array.isArray(p.segments) || !liveListEl) return;
    if (batchRunning) return;   // queue items are not the single-file live view
    const frag = document.createDocumentFragment();
    for (const s of p.segments) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:10px;padding:3px 2px;border-bottom:1px solid var(--line)";
      const t = document.createElement("span");
      t.style.cssText = "color:var(--ink-2);font-family:var(--font-mono);white-space:nowrap;font-size:11px";
      t.textContent = fmtDur(s.start);
      const x = document.createElement("span");
      x.style.cssText = "flex:1;min-width:0;word-break:break-word";
      x.textContent = s.text;
      row.append(t, x);
      frag.appendChild(row);
      liveCount++;
    }
    liveListEl.appendChild(frag);
    // Bound the DOM: a 5-hour lecture must not recreate the renderer OOM.
    while (liveListEl.childElementCount > LIVE_MAX) liveListEl.removeChild(liveListEl.firstElementChild);
    if (liveFollow) liveListEl.scrollTop = liveListEl.scrollHeight;
    const panel = $("#live-panel"); if (panel) panel.hidden = false;
  });

  // ════ Signature：波形 + 播放 + 分段標註（Suno 式）════
  //   波形峰值由後端在串流 PCM 時增量算出（200 個數字），前端不再解碼整份音檔；
  //   <audio> 負責播放，播放頭跟走、已播段標色；波形下方對齊時間軸的字幕區塊
  //   與字幕卡隨播放高亮，皆可點選 seek。
  const WAVE_N = 200;
  let audioEl = null, audioUrl = null, waveDur = 0, curSegs = [];

  let lastSrtPath = null;
  async function renderResult(res) {
    $("#result").hidden = false;
    lastSrtPath = res.srtPath || null;
    const rp = $("#btn-refine-punct"), eh = $("#btn-export-handout");
    if (rp) rp.disabled = !lastSrtPath;
    if (eh) eh.disabled = !lastSrtPath;
    const segments = res.segments || [];
    curSegs = segments;
    _karaCache = null;
    cleanupAudio();

    // The backend builds the 200 waveform bars incrementally while streaming
    // the PCM. Decoding the whole file here with decodeAudioData() used to
    // allocate GBs in the WebView2 renderer and crash it with "Out of memory"
    // right after a successful long-audio run.
    let peaks = (res.peaks && res.peaks.length) ? res.peaks : null;
    waveDur = res.duration || (segments.length ? segments[segments.length - 1].end : 0) || 60;

    const file = picked && (picked.file || (picked instanceof Blob ? picked : null));
    if (file instanceof Blob) {
      // <audio> decodes lazily / streams; it never materialises the full PCM.
      audioUrl = URL.createObjectURL(file);
      audioEl = new Audio(audioUrl);
    }

    $("#wave-dur").textContent = fmtClock(waveDur);
    drawWave($("#wave"), peaks, segments, waveDur);
    renderSegStrip(segments, waveDur);
    renderSubs(segments);
    wireAudio();
    syncStickyOffset();
  }

  // 波形面板為 sticky 釘在頂端 → 捲動容器需保留等高的頂部空間，
  // 否則 scrollIntoView 自動捲到的字幕卡會被面板遮住。
  function syncStickyOffset() {
    const panel = $(".wave-panel"), host = $(".view-host");
    if (!panel || !host) return;
    host.style.scrollPaddingTop = (panel.offsetHeight + 12) + "px";
  }
  window.addEventListener("resize", syncStickyOffset);

  function drawWave(container, peaks, segments, dur) {
    const n = peaks ? peaks.length : WAVE_N;
    let bars = "";
    for (let i = 0; i < n; i++) {
      const amp = peaks ? peaks[i] : 0.12;
      bars += `<div class="b" style="height:${Math.max(3, amp * 64).toFixed(1)}px"></div>`;
    }
    let marks = "";                            // 分段邊界標記
    segments.forEach(s => {
      marks += `<div class="seg-mark" style="left:${((s.start / dur) * 100).toFixed(2)}%"></div>`;
    });
    container.innerHTML = `<div class="playhead" id="playhead" style="left:0%"></div>${marks}${bars}`;
  }

  // 波形下方：依時間對齊的字幕區塊（Suno 式，可點選 seek）
  function renderSegStrip(segments, dur) {
    let host = $("#seg-strip");
    if (!host) { host = document.createElement("div"); host.id = "seg-strip"; host.className = "seg-strip"; $(".wave-panel").appendChild(host); }
    host.innerHTML = "";
    segments.forEach((s, i) => {
      const left = (s.start / dur) * 100, w = Math.max(0.6, ((s.end - s.start) / dur) * 100);
      const blk = document.createElement("div");
      blk.className = "seg-block" + (s.speaker ? " spk-" + (((s.speaker - 1) % 3) + 1) : "");
      blk.style.left = left.toFixed(2) + "%"; blk.style.width = w.toFixed(2) + "%";
      blk.dataset.seg = i; blk.title = s.text;
      blk.textContent = s.text;
      blk.addEventListener("click", () => seekTo(s.start));
      host.appendChild(blk);
    });
  }

  function renderSubs(segments) {
    const host = $("#subs"); host.innerHTML = "";
    segments.forEach((s, i) => host.appendChild(subCard(s, i)));
  }
  function subCard(s, idx) {
    const el = document.createElement("div");
    // spkc-N：依說話者標示左側外框色（與 chip 同色系，不污染卡片背景）
    el.className = "sub-card" + (s.speaker ? " spkc-" + (((s.speaker - 1) % 3) + 1) : "");
    el.dataset.seg = idx;
    const spk = s.speaker ? `<span class="chip spk-${((s.speaker - 1) % 3) + 1}">${T("run.speaker", null, { n: s.speaker })}</span>` : "";
    el.innerHTML = `<span class="tc">${fmtClock(s.start)} → ${fmtClock(s.end)}</span>
      <div class="body"><div class="spk">${spk}</div><div class="txt">${escapeHtml(s.text)}</div></div>
      <button class="sub-edit" title="${T("run.editLine", "編輯此行")}" aria-label="${T("run.editLine", "編輯此行")}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
      </button>`;
    el.addEventListener("click", e => {
      if (e.target.closest(".sub-edit") || el.classList.contains("editing")) return;
      seekTo(s.start);
    });
    el.querySelector(".sub-edit").addEventListener("click", e => { e.stopPropagation(); beginEdit(el, idx); });
    return el;
  }

  // 行內校對：筆 → 該行可編輯，Enter 儲存 / Esc 取消；同步回 curSegs 與波形對齊區塊。
  // curSegs 為下載字幕的資料來源，故編輯後存檔即反映校對結果。
  function beginEdit(card, idx) {
    if (card.classList.contains("editing")) return;
    card.classList.add("editing");
    const txt = card.querySelector(".txt");
    const orig = curSegs[idx].text;
    txt.contentEditable = "true"; txt.spellcheck = false;
    txt.focus();
    const range = document.createRange(); range.selectNodeContents(txt);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    function finish(save) {
      txt.removeEventListener("keydown", onKey);
      txt.removeEventListener("blur", onBlur);
      txt.contentEditable = "false";
      card.classList.remove("editing");
      const val = (txt.textContent || "").trim();
      if (save && val && val !== orig) {
        curSegs[idx].text = val; txt.textContent = val;
        curSegs[idx].words = reflowWords(curSegs[idx], val);  // 卡拉OK字級跟著修正
        _karaCache = null;                                    // 失效快取 → 下次重繪該行
        const blk = $(`.seg-block[data-seg="${idx}"]`);
        if (blk) { blk.textContent = val; blk.title = val; }
      } else {
        txt.textContent = orig;               // 取消或清空 → 還原
      }
      txt.blur();
    }
    function onKey(e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    }
    function onBlur() { finish(true); }
    txt.addEventListener("keydown", onKey);
    txt.addEventListener("blur", onBlur);
  }

  // ── 播放接線 ────────────────────────────────────────────
  function wireAudio() {
    const wave = $("#wave"), play = $("#wave-play");
    const bars = $$(".b", wave);
    if (audioEl) {
      audioEl.addEventListener("timeupdate", onTime);
      audioEl.addEventListener("play", () => setPlayIcon(true));
      audioEl.addEventListener("pause", () => setPlayIcon(false));
      audioEl.addEventListener("ended", () => { setPlayIcon(false); });
    }
    // 點波形 seek
    wave.onclick = e => {
      const r = wave.getBoundingClientRect();
      seekTo(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * waveDur);
    };
    if (play) play.onclick = () => { if (!audioEl) return; audioEl.paused ? audioEl.play() : audioEl.pause(); };
    // 停止：暫停並回到開頭（像播放器的 stop）
    const stop = $("#wave-stop");
    if (stop) stop.onclick = () => { if (!audioEl) return; audioEl.pause(); audioEl.currentTime = 0; onTime(); };

    function onTime() {
      const t = audioEl.currentTime, ratio = waveDur ? t / waveDur : 0;
      const ph = $("#playhead"); if (ph) ph.style.left = (ratio * 100).toFixed(2) + "%";
      const playedIdx = Math.floor(ratio * bars.length);
      bars.forEach((b, i) => b.classList.toggle("played", i <= playedIdx));
      const cur = curSegs.findIndex(s => t >= s.start && t < s.end);
      $$(".sub-card").forEach(c => c.classList.toggle("playing", +c.dataset.seg === cur));
      $$(".seg-block").forEach(c => c.classList.toggle("playing", +c.dataset.seg === cur));
      const tm = $("#wave-cur"); if (tm) tm.textContent = fmtClock(t);
      // 自動捲動到播放中的字幕卡
      if (cur >= 0) { const el = $(`.sub-card[data-seg="${cur}"]`); if (el && playFollow) el.scrollIntoView({ block: "nearest" }); }
    }
  }
  let playFollow = true;
  function setPlayIcon(playing) {
    const b = $("#wave-play"); if (!b) return;
    b.innerHTML = playing
      ? `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>`
      : `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
  }
  function seekTo(sec) { if (audioEl) { audioEl.currentTime = sec; audioEl.play().catch(() => {}); } }
  function cleanupAudio() {
    try { if (audioEl) { audioEl.pause(); audioEl.src = ""; } } catch (e) {}
    if (audioUrl) { try { URL.revokeObjectURL(audioUrl); } catch (e) {} audioUrl = null; }
    audioEl = null;
  }

  // ════ 卡拉OK模式（逐字高亮放大跳動，由字級 words 驅動）════
  //   另開 requestAnimationFrame 迴圈（60fps）讀 audioEl.currentTime，逐字更新，
  //   比 <audio> timeupdate（~4fps）順得多。資料源：curSegs[].words（後端 FA 字級；
  //   無對齊時 wordsForSeg 以行時間平均內插）。
  let karaokeOn = false, karaokeRAF = 0, _karaCache = null;   // _karaCache: {idx, words, spans}

  /**
   * 卡拉OK逐字狀態：給「該行字級 words」與「目前播放秒數 t」，回傳與 words 等長的
   * 狀態陣列，每元素 { sung, active, scale }：
   *   sung   已唱過（t 已過該字 end）→ CSS .sung 填主題色
   *   active 正在唱（t 落在該字 [start,end)）→ CSS .active 高亮
   *   scale  1=原始大小；>1=放大（updateKaraoke 會映射成 transform：放大 + 微上抬）
   *
   * 目前 sung/active 已實作（KTV 式逐字填色已可運作）。放大跳動曲線留給你 ——
   * 這是「逐字放大跳動」效果的核心，也是最有設計選擇的一段。
   */
  function karaokeCharStates(words, t) {
    return words.map(w => {
      const sung = t >= w.end;
      const active = t >= w.start && t < w.end;
      let scale = 1;
      // ── TODO（你來寫）：放大跳動曲線（5~8 行）──────────────────────────
      //   目標：字被唱到的瞬間「彈」一下再回落（Apple Music 動態歌詞風）。
      //   手上的量：w.start / w.end（該字時間窗）、t（目前秒數）、active。
      //   設計空間（任選或自創）：
      //     • 進度 p = (t - w.start) / (w.end - w.start)，clamp 0..1
      //     • 起跳即最大、唱完歸位： if (active) scale = 1 + 0.25 * (1 - p)
      //     • 預備動作：在 w.start 前 ~0.1s 先微幅放大（anticipation）
      //     • 建議上限 scale ≤ 1.3，避免整行版面抖動過猛
      //   （把你的曲線寫進 scale 即可，其餘渲染/映射都已接好。）
      // ──────────────────────────────────────────────────────────────────
      return { sung, active, scale };
    });
  }

  // 卡拉OK高亮單位：去空白與標點（與後端 FA words 慣例一致，標點不單獨成一拍）。
  const _KARA_PUNCT = "，。？！；：、…—·,.!?;:";
  function karaokeUnits(text) {
    return [...(text || "")].filter(c => c.trim() && !_KARA_PUNCT.includes(c));
  }
  // 把某段 chars 依行時間平均內插成 words（無 FA、或編輯後字數改變時用）。
  function interpWords(seg, units) {
    const dur = units.length ? (seg.end - seg.start) / units.length : 0;
    return units.map((ch, i) => ({ start: seg.start + i * dur, end: seg.start + (i + 1) * dur, text: ch }));
  }
  // 取某段字級 words；後端未對齊（words 空）→ 行時間平均內插（時間為估計值）。
  function wordsForSeg(seg) {
    if (seg.words && seg.words.length) return seg.words;
    return interpWords(seg, karaokeUnits(seg.text));
  }
  // 行內校對後同步卡拉OK字級：字數不變（多為單字修正）→ 保留原 FA 時間只換字；
  // 字數改變 → 該行 FA 已不對應，改以行時間平均重灑。回傳新 words。
  function reflowWords(seg, newText) {
    const units = karaokeUnits(newText);
    const old = seg.words || [];
    if (old.length && old.length === units.length) {
      return old.map((w, i) => ({ start: w.start, end: w.end, text: units[i] }));
    }
    return interpWords(seg, units);
  }

  // 把某段渲染成一排 .kara-char span，回傳 {words, spans}（spans 與 words 等長）。
  function renderKaraokeLine(idx) {
    const seg = curSegs[idx], line = $("#kara-line");
    line.innerHTML = "";
    const words = wordsForSeg(seg), spans = [];
    words.forEach(w => {
      const span = document.createElement("span");
      span.className = "kara-char"; span.textContent = w.text;
      line.appendChild(span); spans.push(span);
      if (/[a-z]/i.test(w.text)) {                 // 拉丁詞後補空白格
        const sp = document.createElement("span"); sp.className = "kara-char space"; line.appendChild(sp);
      }
    });
    const next = curSegs[idx + 1];
    $("#kara-next").textContent = next ? next.text : "";
    return { words, spans };
  }

  // 每幀更新：定位目前段 → （換段才重繪）→ 算每字狀態 → 套用 transform。
  function updateKaraoke(t) {
    if (!curSegs.length) return;
    let idx = curSegs.findIndex(s => t >= s.start && t < s.end);
    if (idx < 0) idx = _karaCache ? _karaCache.idx : 0;   // 空隙：停在上一段（唱完狀態）
    if (!_karaCache || _karaCache.idx !== idx) {
      const built = renderKaraokeLine(idx);
      _karaCache = { idx, words: built.words, spans: built.spans };
    }
    const { words, spans } = _karaCache;
    const states = karaokeCharStates(words, t);          // ← 使用者實作的高亮曲線
    for (let i = 0; i < spans.length; i++) {
      const st = states[i] || {};
      spans[i].classList.toggle("sung", !!st.sung);
      spans[i].classList.toggle("active", !!st.active);
      const sc = +st.scale || 1;
      // scale→transform：放大同時微微上抬（跳動感）。視覺映射固定於此，曲線由 states 決定。
      spans[i].style.transform = sc === 1 ? "" : `translateY(${((sc - 1) * -12).toFixed(1)}px) scale(${sc.toFixed(3)})`;
    }
  }

  function karaokeTick() {
    if (!karaokeOn) return;
    updateKaraoke(audioEl ? audioEl.currentTime : 0);
    karaokeRAF = requestAnimationFrame(karaokeTick);
  }
  function setKaraoke(on) {
    karaokeOn = on;
    $("#karaoke").hidden = !on;
    $("#subs").hidden = on;
    $("#btn-karaoke").classList.toggle("kara-on", on);
    cancelAnimationFrame(karaokeRAF);
    if (on) { _karaCache = null; karaokeTick(); }
  }
  $("#btn-karaoke") && $("#btn-karaoke").addEventListener("click", () => setKaraoke(!karaokeOn));

  // 三處「開啟輸出資料夾」共用同一個後端呼叫（單檔頁 / 批次頁 / 設定頁）。
  // 全部都常亮：目錄是固定的使用者資料夾，與有沒有產出無關。
  ["#btn-open-dir", "#btn-batch-open-dir", "#btn-open-dir-set"].forEach(sel => {
    const b = $(sel);
    if (b) b.addEventListener("click", () => API.openOutputDir());
  });

  // ── 字幕存檔：由記憶體中的 curSegs 在前端組檔，下載到使用者指定位置 ──
  //   （瀏覽器沙箱不暴露來源檔真實路徑，無法寫回來源資料夾，故走下載。
  //    SRT/TXT 格式比照 subtitle_lines，含「說話者N：」前綴。）
  function srtTs(s) {
    const ms = Math.round(s * 1000);
    const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
    const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
    const sec = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
    return `${h}:${m}:${sec},${String(ms % 1000).padStart(3, "0")}`;
  }
  function buildSrt(segs) {
    return segs.map((s, i) => {
      const spk = s.speaker ? T("run.speakerPrefix", null, { n: s.speaker }) : "";
      return `${i + 1}\n${srtTs(s.start)} --> ${srtTs(s.end)}\n${spk}${s.text}\n`;
    }).join("\n");
  }
  function buildTxt(segs) {
    return segs.some(s => s.speaker)
      ? segs.map(s => (s.speaker ? T("run.speakerPrefix", null, { n: s.speaker }) : "") + s.text).join("\n")
      : segs.map(s => s.text).join("");
  }
  async function saveSubtitle() {
    if (!curSegs.length) return;
    let fmt = "srt";
    try { fmt = (await API.getSettings()).format || "srt"; } catch (e) {}
    const text = fmt === "txt" ? buildTxt(curSegs) : buildSrt(curSegs);
    const base = (picked && picked.name) ? picked.name.replace(/\.[^.]+$/, "")
      : T("run.defaultSubtitleName", "字幕");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = base + (fmt === "txt" ? ".txt" : ".srt");
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    logLine(T("run.savedSubtitle", null, { name: a.download }));
  }
  $("#btn-save-sub").addEventListener("click", saveSubtitle);

  // ── C1 / C2: optional post-processing exports ───────────────────────
  $("#btn-refine-punct") && $("#btn-refine-punct").addEventListener("click", async () => {
    if (!lastSrtPath) return;
    try {
      const r = await API.refinePunctuation(lastSrtPath);
      logLine(I18N.t("file.refineDone", {path: r.path, lines: r.lines}));
    } catch (e) { logLine(I18N.t("file.refineFailed", {error: e.message || e})); }
  });
  $("#btn-export-handout") && $("#btn-export-handout").addEventListener("click", async () => {
    if (!lastSrtPath) return;
    try {
      const r = await API.exportHandout(lastSrtPath, "md");
      logLine(I18N.t("file.handoutDone", {path: r.path, lines: r.lines, bytes: r.bytes}));
    } catch (e) { logLine(I18N.t("file.handoutFailed", {error: e.message || e})); }
  });

  // ── H1: collapsible runtime log ─────────────────────────────────────
  // Browser-side actions go through logLine(); backend events arrive as SSE
  // "log" and a reload restores history from GET /api/logs. Log text is kept
  // verbatim on purpose - diagnostic messages must not be rewritten by i18n.
  const LOG_MAX = 300;
  function appendLogRow(entry) {
    const el = $("#file-log"); if (!el) return;
    const d = document.createElement("div"); d.className = "ln";
    // Wave 11／問題 7：一條訊息 = 一行純文字，去裝飾、去換行。
    // 後端診斷（ffmpeg 路徑、WinError 5 說明）常含多行；舊版
    // white-space:pre-wrap 會把它折成好幾行，看起來像裝飾過的段落而不是
    // 逐行輸出。這裡把內嵌換行壓成「 · 」，並去掉舊版 [ts] 的方括號裝飾，
    // 時間戳改以淡色前綴固定顯示（有就一定顯示）。
    const level = entry && entry.level;
    if (level === "error") d.classList.add("lv-error");
    else if (level === "warn") d.classList.add("lv-warn");
    const msg = String(entry && entry.message != null ? entry.message : entry)
      .replace(/\r\n|\r|\n/g, " · ")
      .replace(/[ \t]{2,}/g, " ")
      .trim();
    const ts = entry && entry.ts ? String(entry.ts) : "";
    if (ts) {
      const s = document.createElement("span"); s.className = "ts";
      s.textContent = ts + "  ";
      d.appendChild(s);
    }
    d.appendChild(document.createTextNode(msg));
    el.appendChild(d);
    while (el.childElementCount > LOG_MAX) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
    const cnt = $("#log-count");
    if (cnt) cnt.textContent = el.childElementCount ? `(${el.childElementCount})` : "";
  }
  function logLine(msg) {
    const el = $("#file-log"); if (!el) return;
    el.hidden = false; logExpanded = true;
    const toggle = $("#btn-log-toggle"), chevron = $("#log-chevron");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    if (chevron) chevron.textContent = "▾";
    appendLogRow({ ts: new Date().toTimeString().slice(0, 8),
                   level: String(msg).startsWith("✕") ? "error" : "info",
                   message: msg });
  }
  API.on("log", entry => { if (entry) appendLogRow(entry); });
  $("#btn-log-toggle") && $("#btn-log-toggle").addEventListener("click", () => {
    const el = $("#file-log"); if (!el) return;
    logExpanded = !logExpanded; el.hidden = !logExpanded;
    $("#btn-log-toggle").setAttribute("aria-expanded", String(logExpanded));
    const c = $("#log-chevron"); if (c) c.textContent = logExpanded ? "▾" : "▸";
  });
  $("#btn-log-clear") && $("#btn-log-clear").addEventListener("click", async () => {
    const el = $("#file-log"); if (el) el.innerHTML = "";
    const cnt = $("#log-count"); if (cnt) cnt.textContent = "";
    try { await API.clearLogs(); } catch {}
  });

  // ════════════════════════════════════════════════════════
  // ════════════════════════════════════════════════════════
  // 批次（B4）：服務端持久佇列。檔案上傳一次、背景單 worker 逐一處理，
  // 狀態由 SSE「batch」事件推送；重新整理頁面也不會掉佇列。
  // mock（離屏預覽）模式保留原本的前端序列迴圈，讓靜態展示不需後端。
  // ════════════════════════════════════════════════════════
  // 狀態標籤刻意存 [key, fallback] 而不是求值後的字串：模組載入時求值會把
  // 當下語言「凍結」住，之後切到簡體／英文，批次列仍是原本那套字。
  const ST = {
    done: "batch.stDone", running: "batch.stRunning", pending: "batch.stPending",
    failed: "batch.stFailed", canceled: "batch.stCanceled", uploading: "batch.stUploading",
  };
  const ST_CLS = {
    done: "chip-ok", running: "chip-accent", pending: "chip-muted",
    failed: "chip-live", canceled: "chip-muted", uploading: "chip-muted",
  };
  function stLabel(status) {
    return [T(ST[status] || ST.pending), ST_CLS[status] || "chip-muted"];
  }
  // batchItems / batchRunning / batchMockRunning / batchActiveIdx /
  // batchUploadErrors are declared with the H2 state machine above.

  // 隱藏的多檔選擇器（瀏覽器原生）
  const batchInput = document.createElement("input");
  batchInput.type = "file"; batchInput.accept = "audio/*,video/*";
  batchInput.multiple = true; batchInput.style.display = "none";
  document.body.appendChild(batchInput);

  // 批次執行參數＝音檔頁當下的設定，逐項對齊：
  //   language / diarize+nSpeakers / align / hint / vadThreshold / chunkSecs
  // 不繼承「試跑前 5 分鐘」：那只是單檔預覽，套到整條佇列會產出一堆被截斷
  // 的字幕。除此之外沒有第二套規則 —— 這個函式的產出就是批次會用的值，
  // 批次頁的設定卡顯示的也是同一份。
  function currentBatchOpts() {
    return {
      language: $("#sel-lang") ? $("#sel-lang").value : null,
      ...diarOpts($("#sel-diar") ? $("#sel-diar").value : "off"),
      align: $("#sw-align") ? $("#sw-align").checked : false,
      hint: $("#hint-box") ? $("#hint-box").value.trim() : "",
      // 切分參數：新版一併在「開始」時鎖進佇列（RUN_OPT_KEYS），
      // 不再讓每個檔開跑時現讀 settings —— 否則同一批會有兩套時間軸。
      vadThreshold: $("#seg-vad") ? Number($("#seg-vad").value) : null,
      chunkSecs: $("#seg-chunk") ? Number($("#seg-chunk").value) : null,
    };
  }

  // Wave 14：上傳路由已徹底移除。這個隱藏 input 只在無後端的預覽模式
  // （mock）裡用來把 File 物件放進前端假佇列；web 模式一律走本機絕對路徑，
  // 絕不讀 content、絕不產生 batch_uploads 副本。
  batchInput.addEventListener("change", e => {
    const files = Array.from(e.target.files || []);
    batchInput.value = "";
    if (!files.length || API.mode === "web") return;
    for (const f of files) batchItems.push({
      id: "m" + Math.random().toString(36).slice(2, 9), file: f,
      name: f.name, status: "pending", progress: 0, localPath: true,
    });
    renderBatch();
  });

  /** 從服務端拉全量快照；服務端是唯一事實源。
   *  Wave 11：失敗時「保留現有佇列」而非清空——以前 getBatch() 降級成
   *  items:[] 一次，8 個檔就瞬間歸零，使用者看到「開始後只剩一個」。 */
  async function refreshBatch() {
    if (API.mode !== "web") return;
    let snap;
    try { snap = await API.getBatch(); }
    catch (err) {
      // 不清空、不覆寫；保留上一次的正確快照。
      console.warn("[batch] refreshBatch failed, keeping existing queue", err);
      return;
    }
    if (!snap || !Array.isArray(snap.items)) return;   // 髒資料不覆寫
    batchItems = snap.items.map(it => Object.assign({}, it, { file: null }));
    batchRunOpts = (snap && snap.runOpts) || {};
    batchRunning = !!(snap.summary && snap.summary.running);
    if (snap.summary && snap.summary.elapsedSec != null)
      batchElapsed = Number(snap.summary.elapsedSec) || 0;
    renderBatch();
    syncActionButtons();
  }

  API.on("batch", payload => {
    if (API.mode !== "web" || !payload) return;
    // 換任務／全量快照時丟掉上一支檔案的進度明細，避免顯示舊時長。
    if (payload.type === "start" || payload.type === "state" ||
        payload.type === "added" || payload.type === "retry")
      lastBatchDetail = null;
    if (payload.type === "state" && Array.isArray(payload.items)) {
      // Wave 14：`state` 是全量快照，前端的本地補充欄位（segmentsDone）
      // 要一併保留，否則每次快照都會把續跑的段數抹掉。
      const prev = new Map(batchItems.map(i => [i.id, i]));
      batchItems = payload.items.map(it => Object.assign(
        {}, prev.get(it.id) || {}, it, { file: null }));
    } else if (payload.type === "duration" && payload.item) {
      // Wave 14／需求 8：入隊後背景探測到的時長。只補欄位、不重繪整個列表，
      // 避免 10 個檔案同時探測完成時把佇列刷 10 次。
      const k = batchItems.findIndex(i => i.id === payload.item.id);
      if (k >= 0) {
        batchItems[k].durationSec = payload.item.durationSec;
        batchItems[k].etaSec = payload.item.etaSec;
        updateBatchRow(k);
        batchSummary();
      }
      if (payload.summary) batchRunning = !!payload.summary.running;
      syncActionButtons();
      return;
    } else if (payload.item) {
      // Wave 11：單筆 item 事件（start/progress/done/error）只做「更新或新增」。
      // 若本地陣列是空的、但服務端 summary 說還有 N 筆，代表我們狀態落後，
      // 別只 push 這一筆（那會讓佇列看起來「只剩第一個」），直接補拉全量。
      const serverTotal = payload.summary && payload.summary.total;
      if (!batchItems.length && serverTotal > 1) {
        refreshBatch();
        return;
      }
      const idx = batchItems.findIndex(i => i.id === payload.item.id);
      if (idx >= 0) batchItems[idx] = Object.assign({}, batchItems[idx], payload.item);
      else batchItems.push(Object.assign({}, payload.item, { file: null }));
    }
    if (payload.summary) batchRunning = !!payload.summary.running;
    else if (typeof payload.running === "boolean") batchRunning = payload.running;
    if (payload.summary && payload.summary.elapsedSec != null)
      batchElapsed = Number(payload.summary.elapsedSec) || 0;
    renderBatch();
    syncActionButtons();
  });

  /** 「目前第 i/n 個 · 正在識別：name」；明細移到下面的第二層進度條。 */
  function refreshBatchActiveText() {
    const el = $("#batch-prog-active"); if (!el) return;
    const idx = batchItems.findIndex(i => i.status === "running");
    if (idx < 0) { el.textContent = T("batch.noActive", "—"); return; }
    el.textContent = T("batch.currentFile", null,
      { i: idx + 1, n: batchItems.length, name: batchItems[idx].name });
  }

  /** 佇列級剩餘時間：把「本檔還沒跑完的音訊 + 所有等待中檔案的音訊」
      一起除以目前有效倍率。與檔案級的「本檔剩餘」是兩個不同問題。
      任一等待中檔案還沒探測到時長就回 null（顯示 —），不估算。 */
  function queueEtaSec() {
    if (!batchRunning) return null;
    const d = lastBatchDetail || {};
    const rate = Number(d.speed5m) || Number(d.speedX) || 0;
    if (!(rate > 0)) return null;
    let rest = 0;
    if (Number(d.totalSec) > 0)
      rest += Math.max(0, Number(d.totalSec) - (Number(d.doneSec) || 0));
    const pend = batchItems.filter(i => i.status === "pending");
    for (const it of pend) {
      const dur = Number(it.durationSec);
      if (!(dur > 0)) return null;      // 時長未探測完 → 不給假數字
      rest += dur;
    }
    return rest > 0 ? rest / rate : null;
  }

  function batchSummary() {
    const total = batchItems.length;
    const done = batchItems.filter(i => i.status === "done").length;
    const failed = batchItems.filter(i => i.status === "failed").length;
    const pending = batchItems.filter(i => i.status === "pending").length;
    const active = batchItems.find(i => i.status === "running");
    const avg = total
      ? batchItems.reduce((a, b) => a + (b.progress || 0), 0) / total : 0;
    const pct = Math.round(avg * 100);

    // ── 佇列級 ─────────────────────────────────────────────
    const bar = $("#batch-prog-bar"), pctEl = $("#batch-prog-pct"),
          statusEl = $("#batch-prog-status"), countEl = $("#batch-prog-count");
    if (bar) bar.style.width = pct + "%";
    if (pctEl) pctEl.textContent = pct + "%";
    if (statusEl) {
      // 狀態只講「處於什麼狀態」，檔案名交給下面的檔案級區塊，避免同一句
      // 話在兩層各說一遍（舊版 status 與 active 都帶 name）。
      if (active) statusEl.textContent = T("batch.stRunning", "辨識中");
      else if (batchRunning) statusEl.textContent = T("batch.progPaused", null, { n: pending });
      else if (!total) statusEl.textContent = T("batch.progEmpty", "尚未加入檔案");
      else if (done + failed === total) statusEl.textContent = T("batch.progDone", "全部完成");
      else statusEl.textContent = T("batch.idle", "待處理");
    }
    if (countEl) {
      let c = T("batch.progCount", null, { done, total });
      if (failed) c += " · " + T("batch.progFailed", null, { n: failed });
      countEl.textContent = c;
    }
    // 佇列級「已耗時」放 meta；「預計剩餘」放 detail 的尾端，兩者互不重複。
    const metaEl = $("#batch-prog-meta");
    if (metaEl) {
      metaEl.textContent = batchElapsed > 0
        ? T("batch.elapsed", null, { t: fmtDur(batchElapsed) }) : "";
    }
    const qEtaEl = $("#batch-prog-eta");
    if (qEtaEl) {
      const t = queueEtaSec();
      qEtaEl.hidden = t == null;
      qEtaEl.textContent = t == null ? "" : T("batch.queueEta", null, { t: fmtDur(t) });
    }
    // 佇列標題只留「檔數 + 總音訊」；本檔剩餘屬檔案級，已移到上面那層。
    const qEl = $("#batch-queue-meta");
    if (qEl) {
      const sumSec = batchItems.reduce(
        (a, b) => a + (Number(b.durationSec) || 0), 0);
      qEl.textContent = T("batch.queueMeta", null,
        { n: total, audio: sumSec ? fmtDur(sumSec) : "—" });
    }
    refreshBatchActiveText();
    renderCurrentFile();
  }

  /** 本次執行設定卡：逐項列出音檔頁的控制項與它「現在實際會用的值」。
      執行中顯示開始那一刻鎖定的值（batchRunOpts），並標明已鎖定；
      閒置時直接讀控制項，等同「按下去會用什麼」。 */
  let _batchCfgSig = "";
  function renderBatchConfig() {
    const host = $("#batch-cfg-grid"); if (!host) return;
    const locked = batchRunning && Object.keys(batchRunOpts || {}).length > 0;
    const src = locked ? batchRunOpts : currentBatchOpts();
    const note = $("#batch-cfg-note");
    if (note) {
      note.textContent = T(locked ? "batch.cfgLocked" : "batch.cfgLive");
      note.classList.toggle("is-locked", locked);
    }
    const auto = T("batch.cfgAuto", "自動");
    // 模型 / 推理方式不在 runOpts 裡（它們是核心設定，不是每次任務參數），
    // 但要影響執行結果，所以在這裡一併列出，值取自最近一次 status。
    const stt = _lastStatus || {};
    const modelLabel = stt.selectedModel || auto;
    const backendLabel = stt.backend || auto;
    const diar = src.diarize
      ? (String(src.nSpeakers || "auto") === "auto"
         ? auto : T("batch.cfgSpeakers", null, { n: src.nSpeakers }))
      : T("batch.cfgOff", "關閉");
    const hintLen = String(src.hint || "").trim().length;
    const vad = (src.vadThreshold === null || src.vadThreshold === undefined ||
                 src.vadThreshold === "")
      ? "—" : Number(src.vadThreshold).toFixed(2);
    // 每段最長：顯示**實際生效值**。填 30 而模型上限 20 時，真正跑的是 20；
    // 只顯示使用者填的值等於給出一個不存在的承諾。
    const chunkN = Number(src.chunkSecs);
    const effN = Number(stt.chunkSecsEffective) || 0;
    let chunk;
    if (chunkN > 0 && effN > 0 && effN !== chunkN) {
      chunk = T("batch.cfgSecsClamped", null, { eff: effN, want: chunkN });
    } else if (chunkN > 0) {
      chunk = T("batch.cfgSecs", null, { n: chunkN });
    } else {
      chunk = effN > 0 ? T("batch.cfgSecs", null, { n: effN }) : auto;
    }
    const rows = [
      [T("batch.cfgModel", "模型"), modelLabel, false],
      [T("batch.cfgBackend", "推理方式"), backendLabel, false],
      [T("batch.cfgLang", "辨識語言"), src.language || auto, false],
      [T("batch.cfgDiar", "說話者分離"), diar, false],
      [T("batch.cfgAlign", "詞級優化"),
       src.align ? T("batch.cfgOn", "開") : T("batch.cfgOff", "關"), false],
      [T("batch.cfgHint", "辨識提示"),
       hintLen ? T("batch.cfgHintOn", null, { n: hintLen }) : T("batch.cfgNone", "無"), false],
      [T("batch.cfgVad", "VAD 靈敏度"), vad, false],
      [T("batch.cfgChunk", "每段最長"), chunk, false],
      // 批次最容易撞名（多個資料夾都有 001.mp4），所以把策略顯示出來，
      // 免得使用者以為檔案沒產出、其實是被同名的另一支蓋掉了。
      [T("batch.cfgNaming", "同名檔案"),
       T(_outNaming === "overwrite" ? "set.nameOverwrite"
         : (_outNaming === "hash" ? "set.nameHash" : "set.nameSuffix")), false],
      // 試跑只對單檔有意義，明確標「不適用」而不是默默忽略。
      [T("batch.cfgTrial", "試跑前 5 分鐘"), T("batch.cfgNa", "不適用"), true],
    ];
    const sig = JSON.stringify(rows);
    if (sig === _batchCfgSig) return;     // 佇列事件很密，值沒變就不重建 DOM
    _batchCfgSig = sig;
    host.innerHTML = rows.map(([k, v, na]) =>
      `<div class="cfg-item"><span class="k">${esc(k)}</span>` +
      `<span class="v${na ? " is-na" : ""}">${esc(v)}</span></div>`).join("");
  }
  function renderBatch() {
    trackBatchTiming();
    closeBatchMenu();          // 佇列重繪會移除舊按鈕，選單跟著關掉
    batchSummary();
    renderBatchConfig();
    const host = $("#batch-list"); if (!host) return;
    host.innerHTML = "";
    if (!batchItems.length) {
      // Wave 14／需求 5：舊版把整句繁中寫死在 innerHTML 裡，切到簡體／English
      // 也不變。改走 T() 三語字典；語言切換時會重繪佇列（見 setLang 監聽）。
      const box = document.createElement("div");
      box.className = "card";
      box.style.cssText = "text-align:center;color:var(--muted);padding:28px";
      box.textContent = T("batch.empty");
      host.appendChild(box);
      return;
    }
    batchItems.forEach((it, idx) => host.appendChild(batchRow(it, idx)));
    // 「可拖曳」提示只在真的能拖时出现（≥2 个 pending），否则是噪音。
    const hint = $("#batch-move-hint");
    if (hint) hint.hidden = batchItems.filter(x => x.status === "pending").length < 2;
  }

  /* ── 佇列排序 ──────────────────────────────────────────────────────────
     執行順序 = items 的列表順序（後端 _run() 取第一個 pending），所以「重排」
     就是改列表順序，不需要額外的優先級欄位。
     只有 pending 的列可以拖：正在跑的那列位置代表「進度」，拖走會讓人以為
     它被取消了；已完成／失敗的列是時間序，拖動會破壞「剛才是這樣跑的」印象。
     置頂同理，目標是「第一個 pending 之前」，不會蓋過執行中的那一列。 */
  function batchCanMove(it) { return it && it.status === "pending"; }

  async function moveBatchItem(id, toIndex) {
    const from = batchItems.findIndex(x => x.id === id);
    if (from < 0) return;
    if (!batchCanMove(batchItems[from])) {
      logLine("x " + T("batch.moveOnlyPending"));
      return;
    }
    if (API.mode !== "web") {
      // mock：本地 splice，讓離屏驗收也能驗到重排
      const [it] = batchItems.splice(from, 1);
      const dst = toIndex > from ? Math.max(0, toIndex - 1) : toIndex;
      batchItems.splice(Math.min(dst, batchItems.length), 0, it);
      renderBatch();
      return;
    }
    const r = await API.batchMove(id, toIndex);
    if (r && r.ok === false) logLine("x " + T("batch.moveOnlyPending"));
    // 後端已透過 _broadcast() 推 snapshot；這裡不樂觀更新，避免與 SSE 打架。
  }

  async function promoteBatchItem(id) {
    const it = batchItems.find(x => x.id === id);
    if (!batchCanMove(it)) {
      logLine("x " + T("batch.moveOnlyPending"));
      return;
    }
    if (API.mode !== "web") {
      const i = batchItems.findIndex(x => x.id === id);
      const [x] = batchItems.splice(i, 1);
      const first = batchItems.findIndex(y => y.status === "pending");
      batchItems.splice(first < 0 ? 0 : first, 0, x);
      renderBatch();
      return;
    }
    const r = await API.batchPromote(id);
    if (r && r.ok === false) logLine("x " + T("batch.moveOnlyPending"));
  }

  function bindBatchRowDrag(row, it, idx) {
    // 非 pending 也接 dragover：否則拖到已完成那列上方時不會有放置指示，
    // 使用者會以為整份列表都不能放。真正的擋下在 move() 裡。
    row.addEventListener("dragover", e => {
      if (!batchDragId || batchDragId === it.id) return;
      e.preventDefault(); e.stopPropagation();
      try { e.dataTransfer.dropEffect = "move"; } catch (err) {}
      const r = row.getBoundingClientRect();
      const after = (e.clientY - r.top) > r.height / 2;
      row.classList.toggle("drop-after", after);
      row.classList.toggle("drop-before", !after);
    });
    row.addEventListener("dragleave", () => {
      row.classList.remove("drop-before", "drop-after");
    });
    row.addEventListener("drop", async e => {
      if (!batchDragId) return;
      e.preventDefault(); e.stopPropagation();
      const r = row.getBoundingClientRect();
      const after = (e.clientY - r.top) > r.height / 2;
      const id = batchDragId;
      clearBatchDropMarks();
      batchDragId = null;
      if (id === it.id) return;         // 放回自己身上 = 什麼都不做
      await moveBatchItem(id, idx + (after ? 1 : 0));
    });
    if (it.status !== "pending") return;
    row.draggable = true;
    row.addEventListener("dragstart", e => {
      batchDragId = it.id;
      row.classList.add("dragging");
      try {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", it.id);   // Firefox 需要
      } catch (err) {}
      e.stopPropagation();              // 不要讓拖放區塊的 dragenter 亮起來
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      clearBatchDropMarks();
      batchDragId = null;
    });
  }
  function clearBatchDropMarks() {
    document.querySelectorAll("#batch-list .b-row").forEach(r =>
      r.classList.remove("drop-before", "drop-after", "dragging"));
  }
  /** Wave 11：每一列自己的進度文字 —— 本機路徑標記 + 後端 statusText。 */
  function batchRowSub(it) {
    const bits = [];
    if (it.localPath) bits.push(T("batch.localPath", "本機路徑 · 不複製"));
    if (it.statusText) bits.push(window.I18N ? I18N.L(it.statusText) : String(it.statusText));
    return bits.join(" · ");
  }
  /** Wave 13：每一列的時長／已用／剩餘，只填後端真的給過的數值。 */
  function batchRowStats(it) {
    const isActive = it.status === "running";
    const d = isActive ? lastBatchDetail : null;
    return {
      isActive,
      dur: Number(it.durationSec) || (d && Number(d.totalSec)) || 0,
      used: d ? batchElapsedSec(d) : 0,
      left: d && d.etaSec != null ? Number(d.etaSec) : null,
      pct: Math.round((it.progress || 0) * 100),
    };
  }
  function batchRow(it, idx) {
    const [label, cls] = stLabel(it.status);
    const s = batchRowStats(it);
    const row = document.createElement("div");
    row.className = "b-row" + (s.isActive ? " is-active" : "");
    row.dataset.idx = idx;
    row.dataset.id = it.id || "";
    const sub = batchRowSub(it);
    const moreTitle = T("batch.menuTitle", "More actions");
    row.innerHTML = `
      <span class="ic">${s.isActive ? "▶" : ""}</span>
      <div class="nm"><div class="f">${escapeHtml(it.name)}</div>
        ${sub ? `<div class="s ${it.localPath ? "local" : ""}">${escapeHtml(sub)}</div>` : ""}
        ${it.error ? `<div class="e">${escapeHtml(it.error)}</div>` : ""}</div>
      <span class="dur">${s.dur ? fmtDur(s.dur) : "—"}</span>
      <span class="st chip ${cls}">${label}${s.isActive ? " " + s.pct + "%" : ""}</span>
      <span class="used">${s.isActive && s.used > 0
        ? T("batch.used", null, { t: fmtDur(s.used) }) : "—"}</span>
      <span class="left">${s.isActive && s.left != null
        ? T("batch.left", null, { t: fmtDur(s.left) })
        : (it.status === "pending" ? T("batch.etaPending", "ETA —") : "—")}</span>
      <div class="pbar"><div class="bar"><i style="width:${s.pct}%"></i></div></div>
      <button class="more" title="${moreTitle}" aria-label="${moreTitle}">⋯</button>`;
    row.querySelector(".more").addEventListener("click", e => {
      e.stopPropagation();
      openBatchMenu(row.querySelector(".more"), it);
    });
    bindBatchRowDrag(row, it, idx);
    return row;
  }
  function updateBatchRow(idx) {
    const it = batchItems[idx];
    const row = $(`.b-row[data-idx="${idx}"]`); if (!row || !it) return;
    const [label, cls] = stLabel(it.status);
    const s = batchRowStats(it);
    const bi = row.querySelector(".pbar i");
    if (bi) bi.style.width = s.pct + "%";
    const chip = row.querySelector(".chip");
    if (chip) {
      // Wave 13：保留 .st（grid-area: st），否則局部更新會把狀態欄擠出網格。
      chip.className = "st chip " + cls;
      chip.textContent = label + (s.isActive ? " " + s.pct + "%" : "");
    }
    const dur = row.querySelector(".dur");
    if (dur) dur.textContent = s.dur ? fmtDur(s.dur) : "—";
    const used = row.querySelector(".used");
    if (used) used.textContent = s.isActive && s.used > 0
      ? T("batch.used", null, { t: fmtDur(s.used) }) : "—";
    const left = row.querySelector(".left");
    if (left) left.textContent = s.isActive && s.left != null
      ? T("batch.left", null, { t: fmtDur(s.left) }) : "—";
    let sub = row.querySelector(".nm .s");
    const text = batchRowSub(it);
    if (text) {
      if (!sub) {
        sub = document.createElement("div"); sub.className = "s";
        const nm = row.querySelector(".nm");
        nm.insertBefore(sub, nm.querySelector(".e") || null);
      }
      sub.className = "s" + (it.localPath ? " local" : "");
      sub.textContent = text;
    } else if (sub) sub.remove();
    batchSummary();
  }

  // Wave 13／三點選單：舊版在 batchRunning 時對 running/pending 列兩個分支
  // 都不執行，點了完全沒反應；後端 remove() 也拒絕 running 項。現在任何
  // 狀態都會彈出可執行動作，running 項的移除先自動停止佇列。
  let batchMenuCleanup = null;
  function closeBatchMenu() {
    $$(".b-menu").forEach(m => m.remove());
    if (batchMenuCleanup) {
      document.removeEventListener("click", batchMenuCleanup, true);
      batchMenuCleanup = null;
    }
  }
  function openBatchMenu(btn, it) {
    closeBatchMenu();
    const terminal = ["failed", "canceled"].includes(it.status);
    const running = it.status === "running";
    const acts = [["menuOpenOut", T("batch.menuOpenOut"), () => API.openOutputDir()]];
    // 置頂：只有「等待中」且不是隊首 pending 的項目才有意義
    //（執行中的那一列已經在下一個處理，不需要也不能被它蓋過）。
    const _firstPending = batchItems.findIndex(x => x.status === "pending");
    if (it.status === "pending" && batchItems.indexOf(it) > _firstPending)
      acts.push(["menuPromote", T("batch.menuPromote"), () => promoteBatchItem(it.id)]);
    if (terminal && API.mode === "web")
      acts.push(["menuRetry", T("batch.menuRetry"), () => API.batchRetry(it.id)]);
    if (running && batchRunning && API.mode === "web") {
      acts.push(["menuSkip", T("batch.menuSkip"), async () => {
        await API.batchCancel();
        await API.batchRemove(it.id);
        await API.batchStart();
      }]);
      acts.push(["menuStop", T("batch.menuStop"), () => API.batchCancel()]);
    }
    acts.push(["menuRemove", T("batch.menuRemove"), async () => {
      if (API.mode !== "web") {
        if (!batchMockRunning) {
          const i = batchItems.findIndex(x => x.id === it.id);
          if (i >= 0) batchItems.splice(i, 1);
          renderBatch();
        }
        return;
      }
      if (running) await API.batchCancel();
      const r = await API.batchRemove(it.id);
      if (r && r.ok === false)
        logLine("x " + T("batch.removeBusy", "Item is running; queue was stopped."));
    }]);
    const menu = document.createElement("div");
    menu.className = "b-menu";
    menu.innerHTML = acts.map(([k, label]) =>
      `<button type="button" data-k="${k}">${escapeHtml(label)}</button>`).join("");
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    const w = menu.offsetWidth || 200, h = menu.offsetHeight || 140;
    menu.style.left = Math.max(8, Math.min(window.innerWidth - w - 8,
                                           r.right - w)) + "px";
    menu.style.top = (r.bottom + 6 + h > window.innerHeight
                      ? Math.max(8, r.top - h - 6) : r.bottom + 6) + "px";
    menu.addEventListener("click", async e => {
      const b = e.target.closest("button[data-k]"); if (!b) return;
      const found = acts.find(a => a[0] === b.dataset.k); if (!found) return;
      closeBatchMenu();
      try { await found[2](); }
      catch (err) { logLine("x " + (err.message || err)); }
      if (API.mode === "web") await refreshBatch();
    });
    batchMenuCleanup = e => {
      if (!menu.contains(e.target) && e.target !== btn) closeBatchMenu();
    };
    setTimeout(() => {
      if (batchMenuCleanup) document.addEventListener("click", batchMenuCleanup, true);
    }, 0);
  }

  // Wave 14／需求 2、6、7：批次頁只保留「本機路徑直引」這一條路 ——
  // 拖放框、點擊拖放框、貼路徑三者最終都呼叫後端原生選檔 → add_path，
  // 不複製、不上傳、不產生 batch_uploads 副本。/api/batch/add 上傳路由
  // 已在後端刪除，因此這裡也不再有任何 File 物件回退分支。
  function pickBatchFiles() {
    // 兩種情況都用得到系統選檔：
    //   · web：後端提供原生 GetOpenFileName，能拿到絕對路徑 → add_path
    //   · 預覽（mock）：沒有後端，退化成瀏覽器 <input type=file>，只把 File
    //     物件放進前端假佇列（記憶體），絕不上傳、不落地。
    // 舊版在 mock 模式直接 return，導致「點拖放框沒反應」—— 看起來像壞掉。
    if (API.mode !== "web") { batchInput.click(); return Promise.resolve(); }
    return API.pickFiles().then(async r => {
      const paths = (r && r.supported && Array.isArray(r.paths)) ? r.paths : null;
      if (paths === null) {
        // 原生選檔不可用（非 Windows / 精簡環境）：不要靜默失敗，
        // 也不退化成上傳，直接引導使用者改用「貼上本機路徑」。
        showPathMsg(T("batch.pickUnsupported"), true);
        pathInput && pathInput.focus();
        return;
      }
      if (!paths.length) return;                     // 使用者取消
      const opts = currentBatchOpts();
      let ok = 0; const errs = [];
      for (const p of paths) {
        try { await API.batchAddPath(p, opts); ok++; }
        catch (e) { errs.push(`${p}: ${e.message || e}`); }
      }
      if (errs.length) showPathMsg(T("batch.pathPartial", null,
        { ok, fail: errs.length, err: errs.join("; ") }), true);
      else if (ok) showPathMsg(T("batch.dropAdded", null, { n: ok }), false);
      await refreshBatch();
    }).catch(() => {
      showPathMsg(T("batch.pickUnsupported"), true);
      pathInput && pathInput.focus();
    });
  }

  // Wave 16：拖放框及其 handler 已整個移除（見 index.html 的 batch-nodrop）。
  // 原因：拖入的檔案只有 File 物件、拿不到絕對路徑，而批次流程只引用路徑不複製，
  // 沒有路徑就無從引用。留一個拖了必然失敗的熱區只會誤導使用者，故取消並在
  // 原位置說明原因。「加入檔案」走後端原生多選框，回傳絕對路徑。
  $("#btn-batch-add") && $("#btn-batch-add").addEventListener("click", () => {
    pickBatchFiles();
  });
  // Wave 11／問題 3：本機路徑直引。<input type=file> 出於瀏覽器沙箱拿不到
  // 絕對路徑，所以補一條「貼路徑」通道：後端 add_path() 直接引用、不複製、
  // 不上傳；item 標 ownedPath=false，任何清理都不會動到使用者的原檔。
  // Wave 16：此區改為常顯，不再需要展開／關閉按鈕。
  const pathPanel = $("#batch-paths"), pathInput = $("#batch-path-input"),
        pathMsg = $("#batch-path-msg");
  function showPathMsg(msg, bad) {
    if (!pathMsg) return;
    pathMsg.hidden = false;
    pathMsg.textContent = msg;
    pathMsg.style.color = bad ? "var(--danger, #c0392b)" : "var(--ok, #1C8C5A)";
  }
  $("#btn-batch-path-add") && $("#btn-batch-path-add").addEventListener("click", async () => {
    const lines = String(pathInput ? pathInput.value : "")
      .split(/\r?\n/).map(s => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
    if (!lines.length) {
      showPathMsg(T("batch.pathEmpty", "請先貼上至少一個檔案路徑"), true); return;
    }
    const opts = currentBatchOpts();
    let ok = 0; const errs = [];
    for (const line of lines) {
      if (API.mode === "web") {
        try { await API.batchAddPath(line, opts); ok++; }
        catch (err) { errs.push(`${line}：${err.message || err}`); }
      } else {
        batchItems.push({
          id: "m" + Math.random().toString(36).slice(2, 9), file: null,
          name: line.split(/[\\/]/).pop() || line,
          status: "pending", progress: 0, localPath: true,
        });
        ok++;
      }
    }
    if (errs.length) showPathMsg(T("batch.pathPartial", null,
      { ok, fail: errs.length, err: errs.join("；") }), true);
    else showPathMsg(T("batch.pathAdded", null, { n: ok }), false);
    if (ok && pathInput) pathInput.value = "";
    if (API.mode === "web") await refreshBatch(); else renderBatch();
  });
  $("#btn-batch-run").addEventListener("click", runBatch);
  $("#btn-batch-clear") && $("#btn-batch-clear").addEventListener("click", async () => {
    if (API.mode === "web") { await API.batchClear(); await refreshBatch(); }
    else { batchItems = batchItems.filter(i => !["done", "failed", "canceled"].includes(i.status)); renderBatch(); }
  });

  async function runBatch() {
    if (API.mode === "web") {
      try {
        // 開始／續跑都把音檔頁「此刻」的設定帶給後端，整條佇列鎖定同一組值。
        // 「跳過目前檔案」的重新啟動不帶（見選單），沿用已鎖定的那一份。
        if (batchRunning) await API.batchCancel();
        else await API.batchStart(currentBatchOpts());
      } catch (err) { logLine("✕ " + (err.message || err)); }
      await refreshBatch();
      return;
    }
    // ── mock 預覽：沿用前端序列迴圈（無後端可用）──
    if (batchMockRunning) return;
    if (!batchItems.some(i => i.status !== "done")) return;
    batchMockRunning = true; syncActionButtons(); resetLive();
    for (let i = 0; i < batchItems.length; i++) {
      const it = batchItems[i];
      if (it.status === "done") continue;
      batchActiveIdx = i; it.status = "running"; it.progress = 0; it.error = null;
      renderBatch();
      try {
        const res = await API.transcribe(Object.assign({ file: it.file }, currentBatchOpts()));
        it.status = "done"; it.progress = 1; it.srtPath = res.srtPath;
      } catch (err) {
        it.status = "failed"; it.progress = 0;
        it.error = (err.message || String(err)).replace(/^⚠\s*/, "");
      }
      renderBatch();
    }
    batchActiveIdx = -1; batchMockRunning = false; markBusy("batch", false); renderBatch();
  }

  // 進入批次頁或首次連上後端時，從服務端還原佇列（重新整理也不掉）。
  API.ready.then(async () => {
    if (API.mode !== "web") return;
    refreshBatch();
    syncActionButtons();
    try {
      const snap = await API.getLogs(200);
      (snap.items || []).forEach(appendLogRow);
    } catch {}
  });

  // ════════════════════════════════════════════════════════
  // 模型與裝置（核心 + 模型下拉 + 架構標籤）
  // ════════════════════════════════════════════════════════
  let _modelOpt = null;     // 最近一次 getModelOptions 結果
  let _curCore = null;      // 目前選中的核心標籤

  // ── 系統自檢：每核心 × 每能力，顯示 Green/Yellow/Red ──────────────
  // 自檢一列：名稱 + 圓形感嘆號（hover 才出長說明）+ 短值。
  // 舊版把 detail 直接排在右側，一行常常要塞 40+ 字，窄窗就換行、把行距撐開；
  // 而絕大多數情況下那句話對「現在這個後端」根本不相關。
  function healthRow(it, dot) {
    const row = document.createElement("div"); row.className = "hc-item";
    const tip = it.hint ? I18N.L(it.hint) : "";
    const val = it.value ? I18N.L(it.value)
      : T(it.status === "red" ? "health.missing" : "health.notReady");
    row.innerHTML = dot(it.status) +
      `<span class="hi-label">${escapeHtml(I18N.L(it.label))}</span>` +
      (tip ? `<button class="hi-info" type="button" aria-label="${escapeHtml(tip)}" data-tip="${escapeHtml(tip)}">i</button>` : "") +
      `<span class="hi-value">${escapeHtml(val)}</span>`;
    return row;
  }

  // 觸屏沒有 hover：點感嘆號切換浮層，點別處關閉。
  document.addEventListener("click", e => {
    const b = e.target.closest(".hi-info");
    document.querySelectorAll(".hi-info.tip-on").forEach(x => { if (x !== b) x.classList.remove("tip-on"); });
    if (b) b.classList.toggle("tip-on");
  });

  async function renderHealth() {
    let h;
    try { h = await API.getHealthCheck(); } catch (e) { return; }
    const sum = $("#health-sum");
    if (sum && h.summary) {
      if (h.summary.red > 0) { sum.className = "chip chip-live"; sum.textContent = T("catalog.itemsToFix", null, { n: h.summary.red }); }
      else if (h.summary.yellow > 0) { sum.className = "chip chip-accent"; sum.textContent = T("catalog.coreReady", null, { n: h.summary.yellow }); }
      else { sum.className = "chip chip-ok"; sum.textContent = T("catalog.allReady", "全部就緒"); }
    }
    // 預設收起；只有紅燈（缺且不能自動補）才主動展開，其餘尊重使用者手動收起。
    const fold = $("#health-fold");
    if (fold && h.summary && h.summary.red > 0) fold.open = true;

    const host = $("#health-panel"); host.innerHTML = "";
    const dot = s => `<span class="hdot hdot-${s}"></span>`;
    const mkGroup = g => {
      const card = document.createElement("div"); card.className = "health-core";
      const active = g.backend && g.backend === h.activeBackend;
      card.innerHTML = `<div class="hc-title">${escapeHtml(I18N.L(g.label))}` +
        (active ? ` <span class="chip chip-ok" style="font-size:10px">${T("catalog.inUse", "使用中")}</span>` : ``) + `</div>`;
      (g.items || []).forEach(it => card.appendChild(healthRow(it, dot)));
      return card;
    };

    const groups = [...(h.cores || [])];
    const live = groups.filter(g => !g.inactive);
    const other = groups.filter(g => g.inactive);
    live.forEach(g => host.appendChild(mkGroup(g)));
    if (h.shared && h.shared.length) {
      host.appendChild(mkGroup({ label: T("catalog.shared", "共用元件"), items: h.shared }));
    }

    // 沒在用的後端折成一行：既不佔版面，也不把它們的缺失算進摘要。
    if (other.length) {
      const missing = other.reduce(
        (n, g) => n + (g.items || []).filter(i => i.status !== "green").length, 0);
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "hc-inactive";
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = `<span>${T("health.otherBackends", "其他後端（未啟用）")}</span>` +
        `<span class="chip chip-muted" style="font-size:10px">${T("health.otherMissing", null, { n: missing })}</span>` +
        `<span class="chev" aria-hidden="true">›</span>`;
      const box = document.createElement("div");
      box.hidden = true;
      box.style.gridColumn = "1 / -1";
      box.style.display = "grid"; box.style.gap = "12px";
      other.forEach(g => box.appendChild(mkGroup(g)));
      btn.addEventListener("click", () => {
        const open = box.hidden;
        box.hidden = !open;
        btn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      host.appendChild(btn); host.appendChild(box);
    }
  }
  $("#btn-health-recheck") && $("#btn-health-recheck").addEventListener("click", renderHealth);

  // ① 當前狀態列：回答「現在到底用哪個模型、載入了沒」。
  //    舊版要滾到頁尾下載中心才知道在用哪個；模型頁反而沒有這條總結。
  function paintModelStatus(s) {
    const cur = (_modelOpt && _modelOpt.current) || {};
    const name = $("#ms-model"), be = $("#ms-backend"), st = $("#ms-state");
    if (name) name.textContent = cur.model || "—";
    if (be) be.textContent = (s && s.backend) || "—";
    if (st) {
      const ready = !!(s && s.modelReady);
      st.textContent = ready ? T("model.loaded", "已載入") : T("model.notLoaded", "未載入");
      st.style.color = ready ? "var(--ok,#1a7f45)" : "var(--muted)";
    }
    // 自動釋放已移到設定頁（全域偏好）；這裡只做「目前實際行為」的唯讀展示。
    // abs 值是後端算好的生效值，不是 settings 的原始勾選狀態。
    const ar = $("#auto-release-state");
    if (ar) {
      const eff = !!(s && s.autoReleaseModel);
      ar.textContent = eff
        ? T("model.autoReleaseOn", "轉檔後自動釋放：開")
        : T("model.autoReleaseOff", "轉檔後自動釋放：關（此核心常駐）");
      ar.title = T("model.autoReleaseWhere", "可在「設定 → 效能與記憶體」修改");
    }
  }

  async function renderModel() {
    renderHealth();
    // 核心卡 + 模型下拉
    try {
      _modelOpt = await API.getModelOptions();
      _curCore = _modelOpt.current.core;
      renderCoreCards();
      populateModels(_curCore, _modelOpt.current.model);
      // 已記住的選擇 vs 實際載入的架構不同 → 提示重啟
      const curArch = selectedArch();
      if (_modelOpt.activeArch && curArch && _modelOpt.activeArch !== curArch) {
        modelMsg(I18N.t("model.restartPending", {
          core: _curCore, model: $("#model-select").value,
          arch: curArch, active: _modelOpt.activeArch}), "info");
      } else modelMsg("", null);
      updateLoadBtn();              // 設定「下載並載入／前往轉錄」按鈕初始狀態
      try { paintModelStatus(await API.getStatus()); } catch (e) {}
    } catch (e) {}
    // 偵測裝置 + 診斷
    const d = await API.listDevices();
    const host = $("#dev-list"); host.innerHTML = "";
    d.devices.forEach(dev => {
      const cpu = dev.kind === "cpu";
      const el = document.createElement("div"); el.className = "dev";
      el.innerHTML = `<span class="ic">${cpu ? ICON_CPU : ICON_GPU}</span>
        <span class="nm">${dev.kind === "cpu" ? "CPU · " : "GPU · "}${escapeHtml(dev.name)}</span>
        <span class="vram">${escapeHtml(I18N.L(dev.note || ""))}</span>`;
      host.appendChild(el);
    });
    await renderAccel();
    await renderBasic();
    const diag = $("#gpu-diag");
    if (d.diag && d.diag.text) {
      diag.hidden = false;
      diag.className = "banner " + (d.diag.level === "warn" ? "banner-warn" : "banner-info");
      const txt = diag.querySelector("div"); if (txt) txt.innerHTML = escapeHtml(I18N.L(d.diag.text));
    } else diag.hidden = true;
  }

  function coreObj(label) {
    const cs = _modelOpt && _modelOpt.cores || [];
    return cs.find(c => c.label === label) || cs[0];
  }
  function renderCoreCards() {
    const host = $("#core-cards"); host.innerHTML = "";
    (_modelOpt.cores || []).forEach(core => {
      const card = document.createElement("label");
      card.className = "radio-card" + (core.label === _curCore ? " sel" : "");
      card.dataset.core = core.label;
      card.innerHTML = `<span class="rd"></span><div class="info">
        <div class="t">${escapeHtml(I18N.L(core.label))}</div>
        <div class="d">${escapeHtml(T("model.nModels", `${core.models.length} 種模型可選`, { n: core.models.length }))}</div></div>`;
      host.appendChild(card);
    });
  }
  function populateModels(coreLabel, modelLabel) {
    const core = coreObj(coreLabel); if (!core) return;
    const sel = $("#model-select"); sel.innerHTML = "";
    core.models.forEach(m => {
      const o = document.createElement("option");
      o.value = m.label;                       // canonical id, never localised
      o.textContent = I18N.L(m.label);         // display only
      o.dataset.arch = m.arch;
      o.dataset.note = m.note || "";
      if (m.label === modelLabel) o.selected = true;
      sel.appendChild(o);
    });
    if (!core.models.some(m => m.label === modelLabel) && core.models[0]) sel.value = core.models[0].label;
    updateArch();
  }
  function selectedArch() { const o = $("#model-select").selectedOptions[0]; return o ? o.dataset.arch : ""; }
  function updateArch() {
    const o = $("#model-select").selectedOptions[0];
    $("#model-arch").textContent = (o && o.dataset.arch) || "";
    const note = o && o.dataset.note, el = $("#model-note");   // chatllm AMD 等提醒
    if (note) { el.hidden = false; el.textContent = note; } else { el.hidden = true; el.textContent = ""; }
  }

  // ── 模型頁模式：基礎（用途導向）／進階（核心＋模型全手動）──────────
  //   兩模式共用同一顆載入鈕與 set_model／request_load 管道，故不會狀態不一致；
  //   基礎模式只是「幫使用者依硬體挑好那一顆」而已。
  let _modelMode = "basic";
  function applyModelMode(mode, persist) {
    _modelMode = (mode === "advanced") ? "advanced" : "basic";
    const adv = _modelMode === "advanced";
    const bp = $("#basic-pane"), ap = $("#advanced-pane"),
          ax = $("#advanced-extra") || $("#accel-card");
    if (bp) bp.hidden = adv;
    if (ap) ap.hidden = !adv;
    if (ax) ax.hidden = !adv;          // 裝置／加速版本／診斷屬進階
    $$("#model-mode button").forEach(b => b.classList.toggle("on", b.dataset.v === _modelMode));
    if (persist) { try { API.setSettings({ modelMode: _modelMode }); } catch (e) {} }
  }
  $("#model-mode") && $("#model-mode").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    applyModelMode(b.dataset.v, true);
  });

  async function renderBasic() {
    let d;
    try { d = await API.getBasicProfiles(); } catch { return; }
    const host = $("#basic-profiles");
    if (!d || !host) return;
    const hw = $("#basic-hw");
    if (hw) {
      const t = hw.querySelector("div");
      if (t) t.textContent = I18N.t("model.hwSummary", {
        hardware: I18N.L(d.hardware), accel: I18N.L(d.accel)});
    }
    host.innerHTML = "";
    (d.profiles || []).forEach(p => {
      const card = document.createElement("label");
      card.className = "radio-card" + (p.selected ? " sel" : "");
      card.dataset.key = p.key;
      card.dataset.core = p.core; card.dataset.model = p.model;
      card.dataset.note = p.note || "";
      const state = p.present
        ? `<span class="chip chip-muted" style="font-weight:400">${T("catalog.downloaded", "已下載")}</span>`
        : `<span class="chip chip-muted" style="font-weight:400">${T("catalog.needsDownload", "需下載")}</span>`;
      card.innerHTML = `<span class="rd"></span><div class="info">
        <div class="t">${escapeHtml(I18N.L(p.title))}　${state}</div>
        <div class="d">${escapeHtml(I18N.L(p.desc || ""))}</div>
        <div class="d" data-brand="model" style="margin-top:4px;font-family:var(--font-mono);font-size:11.5px">
          ${T("catalog.suggested", null, { model: escapeHtml(p.model) })}</div></div>`;
      host.appendChild(card);
    });
    basicNote((d.profiles || []).find(p => p.selected));
  }
  function basicNote(p) {
    const el = $("#basic-note"); if (!el) return;
    const txt = p && p.note;
    if (!txt) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false; el.textContent = window.I18N ? I18N.L(txt) : txt;
  }
  $("#basic-profiles") && $("#basic-profiles").addEventListener("click", async e => {
    const card = e.target.closest(".radio-card"); if (!card) return;
    $$(".radio-card", $("#basic-profiles")).forEach(c => c.classList.toggle("sel", c === card));
    basicNote({ note: card.dataset.note });
    // 走與進階模式完全相同的套用路徑（會處理「需重啟」等狀態）
    _curCore = card.dataset.core;
    if (_modelOpt) populateModels(_curCore, card.dataset.model);
    await applyModel(card.dataset.core, card.dataset.model);
    await renderBasic();
  });

  // ── 推理加速版本（CrispASR 的 Vulkan / CUDA / CPU build）───────────
  //   後端依 WMI + nvidia-smi 偵測硬體後給推薦；這裡只負責呈現與回寫選擇。
  //   實際下載發生在下次載入模型時（換版本＝換核心，需重啟）。
  async function renderAccel() {
    let a;
    try { a = await API.getAccel(); } catch { return; }
    const host = $("#accel-cards");
    if (!a || !a.ok || !host) { if (host) host.innerHTML = ""; return; }
    host.innerHTML = "";
    (a.options || []).forEach(op => {
      const card = document.createElement("label");
      const sel = op.key === a.selected;
      card.className = "radio-card" + (sel ? " sel" : "");
      card.dataset.variant = op.key;
      if (!op.suitable) { card.style.opacity = ".55"; }
      const tags = [];
      if (op.recommended) tags.push(I18N.t("accel.tagRecommended"));
      if (a.installed && a.installed.variant === op.key
          && a.installed.version === a.latest) tags.push(I18N.t("accel.tagInstalled"));
      const tagHtml = tags.length
        ? ` <span class="chip chip-muted" style="font-weight:400">${tags.join(" · ")}</span>` : "";
      const noteHtml = op.note ? `　<span style="color:var(--warn,#b26a00)">${escapeHtml(I18N.L(op.note))}</span>` : "";
      card.innerHTML = `<span class="rd"></span><div class="info">
        <div class="t">${escapeHtml(I18N.L(op.label))}　<span style="font-weight:400;color:var(--muted)">${escapeHtml(I18N.t("accel.sizeMb", {size: op.size_mb}))}</span>${tagHtml}</div>
        <div class="d">${escapeHtml(I18N.L(op.desc || ""))}${noteHtml}</div></div>`;
      host.appendChild(card);
    });
    const rb = $("#accel-reason");
    if (rb) {
      if (a.reason) {
        rb.hidden = false;
        const t = rb.querySelector("div"); if (t) t.textContent = I18N.L(a.reason);
      } else rb.hidden = true;
    }
    accelMsg(a.needsDownload
      ? I18N.t("accel.needsDownload", {latest: a.latest}) : "", "info");
  }
  function accelMsg(text, level) {
    const el = $("#accel-msg"); if (!el) return;
    if (!text) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false; el.style.marginTop = "10px";
    el.className = "banner " + (level === "warn" ? "banner-warn" : "banner-info");
    el.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg><div>${escapeHtml(window.I18N ? I18N.L(text) : text)}</div>`;
  }
  const _accelHost = $("#accel-cards");
  if (_accelHost) _accelHost.addEventListener("click", async e => {
    const card = e.target.closest(".radio-card"); if (!card) return;
    $$(".radio-card", _accelHost).forEach(c => c.classList.toggle("sel", c === card));
    try {
      const res = await API.setAccel(card.dataset.variant);
      if (res && res.message) accelMsg(res.message, res.restartRequired ? "warn" : "info");
      await renderAccel();
    } catch (err) { accelMsg(String(err), "warn"); }
  });

  // 點核心卡 → 切核心、模型回該核心首項並套用
  $("#core-cards").addEventListener("click", async e => {
    const card = e.target.closest(".radio-card"); if (!card) return;
    _curCore = card.dataset.core;
    $$(".radio-card", $("#core-cards")).forEach(c => c.classList.toggle("sel", c === card));
    const core = coreObj(_curCore);
    const first = core && core.models[0] ? core.models[0].label : "";
    populateModels(_curCore, first);
    await applyModel(_curCore, first);
  });
  // 換模型 → 套用
  $("#model-select").addEventListener("change", async e => {
    updateArch();
    await applyModel(_curCore, e.target.value);
  });
  let _modelLoading = false;       // 模型「就地下載並載入」進行中（進度導到模型頁）
  async function applyModel(core, model) {
    try {
      const res = await API.setModel(core, model);
      if (res && res.message) modelMsg(res.message, res.restartRequired ? "warn" : "info");
      else modelMsg("", null);
      updateLoadBtn(res);
    } catch (err) { modelMsg(I18N.t("model.applyFailed", {error: err.message || err}), "warn"); }
  }
  // 依 setModel 結果 + 目前載入狀態，決定「下載並載入」按鈕的文字/可用性
  async function updateLoadBtn(res) {
    const btn = $("#btn-model-load"); if (!btn) return;
    let st = {};
    try { st = await API.getStatus(); } catch (e) {}
    btn.classList.remove("btn-ghost");
    if (_modelLoading || st.loading) {
      btn.disabled = true; btn.textContent = T("model.loading", "下載／載入中…"); return;
    }
    if (st.modelReady && !(res && res.restartRequired)) {
      // 已就緒：主按鈕不再兼任導航——「前往語音轉文字」已移到頁首狀態列，
      // 一顆按鈕同時扮演「載入」與「跳頁」會讓使用者猜不出按下去會發生什麼。
      btn.disabled = true; btn.textContent = T("model.loaded2", "已載入"); btn.dataset.act = ""; return;
    }
    if (res && res.restartRequired) {
      // 已載入其他核心、切換需重啟 → 按鈕反白（不可就地載入）
      btn.disabled = true; btn.textContent = T("model.needRestart", "切換核心需重新啟動"); btn.dataset.act = ""; return;
    }
    // 尚未載入 → 可就地下載並載入
    btn.disabled = false; btn.textContent = T("model.load", "下載並載入模型"); btn.dataset.act = "load";
  }
  $("#btn-goto-transcribe") && $("#btn-goto-transcribe").addEventListener("click", () => switchView("file"));
  $("#catalog-mirror") && $("#catalog-mirror").addEventListener("click", () => {
    switchView("settings");
    const el = $("#set-mirror");
    if (el) { try { el.focus(); el.scrollIntoView({ block: "center" }); } catch (e) {} }
  });
  $("#btn-model-load") && $("#btn-model-load").addEventListener("click", async () => {
    const btn = $("#btn-model-load");
    if (btn.dataset.act === "goto") { switchView("file"); return; }
    _modelLoading = true; markBusy("model", true); btn.textContent = T("model.loading", "下載／載入中…");
    $("#model-progress").hidden = false;
    setModelProg(0, T("catalog.prepareLoad", "準備載入…"));
    modelMsg(I18N.t("model.loadingHint"), "info");
    try { await API.startLoad(); }
    catch (err) { _modelLoading = false; markBusy("model", false); modelMsg(I18N.t("model.loadFailed", {error: err.message || err}), "warn"); updateLoadBtn(); }
  });
  function setModelProg(pct, status) {
    if (status !== undefined) lastModelProgressStatus = status;
    $("#model-prog-bar").style.width = pct + "%";
    $("#model-prog-pct").textContent = Math.round(pct) + "%";
    if (status != null) $("#model-prog-status").textContent = I18N.L(status);
  }
  function modelMsg(text, level) {
    const el = $("#model-msg");
    if (!text) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false; el.style.marginTop = "10px";
    el.className = "banner " + (level === "warn" ? "banner-warn" : "banner-info");
    el.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg><div>${escapeHtml(window.I18N ? I18N.L(text) : text)}</div>`;
  }
  const ICON_CPU = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/></svg>`;
  const ICON_GPU = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 18v2M18 18v2"/></svg>`;

  // ── 辨識語言下拉（依已載入引擎；OpenVINO 用 processor 清單）────────
  async function loadLanguages() {
    try {
      const { languages } = await API.getLanguages();
      // 音檔頁與錄製頁各有自己的語言下拉，皆依目前引擎填同一份清單。
      ["#sel-lang", "#rec-lang"].forEach(id => {
        const sel = $(id); if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = "";
        languages.forEach(l => {
          const o = document.createElement("option");
          o.value = l.value;
          // 后端按 ui_text 契约返回结构化 label：{k:"msg.lang.auto",v:{},t:"語言 自動"}。
          // 早期实现把它塞进 dataset（字符串化后变成 "[object Object]"）再在切换语言时
          // 走 I18N.L，默认项因此显示成 object object —— 这里保持原对象引用，
          // 由 I18N.L 解析 k/v 或退到 t，语言切换时再重跑一次即可正确本地化。
          if (l.label && typeof l.label === "object") {
            o.dataset.src = JSON.stringify(l.label);
          } else {
            o.dataset.src = l.label || "";
          }
          // Wave 14：語言名（Chinese / Japanese …）與自動偵測不同 —— 前者是
          // 專有名詞，走 I18N.L 會被漢字轉換成「汉语」，英文界面也仍是中文。
          // 自動偵測（結構化 label）照舊本地化；具名語言固定顯示原文。
          const auto = l.value === "" || (l.label && typeof l.label === "object");
          o.dataset.auto = auto ? "1" : "";
          o._src = auto ? l.label : null;
          o._literal = auto ? "" : String(l.label || "");
          o.textContent = auto
            ? (window.I18N ? I18N.L(l.label) : (l.label && l.label.t) || "")
            : (l.value || l.label);
          sel.appendChild(o);
        });
        if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
      });
    } catch (e) {}
  }

  // ════════════════════════════════════════════════════════
  // 端點
  // ════════════════════════════════════════════════════════
  async function renderEndpoint() {
    const e = await API.getEndpoint();
    $("#sw-endpoint").checked = e.running;
    setEpState(e.running);
    $("#ep-port").value = e.port;
    $("#ep-url").textContent = e.running ? maskKeyInUrl(e.url) : T("ep.notRunning", "（服務未啟動）");
    $("#ep-url").dataset.full = e.url;
    $("#ep-key").textContent = "••••••••••••";
    $("#ep-key").dataset.full = e.key;
    applyEpQr(e);
    renderTunnel();
  }
  // 上傳網頁 QR（區網）：服務啟動且有網址才顯示
  function applyEpQr(e) {
    const qr = $("#ep-qr"), src = e.running ? API.qrSrc(e.url) : "";
    if (src) { qr.src = src; qr.hidden = false; } else { qr.hidden = true; }
    $("#ep-qr-hint").textContent = e.running ? T("ep.qrHint", "同網段裝置可掃此 QR 直接開啟上傳頁")
      : T("ep.notRunning", "（服務未啟動）");
  }

  // ── 對外臨時網址（Cloudflare）─────────────────────────────
  async function renderTunnel() { applyTunnelState(await API.getTunnel()); }
  function applyTunnelState(t) {
    $("#sw-tunnel").checked = !!t.running;
    const ready = !!(t.running && t.url);
    const st = $("#cf-state");
    st.className = "chip " + (ready ? "chip-ok" : (t.running ? "chip-accent" : "chip-muted"));
    st.innerHTML = `<span style="font-size:9px">●</span> ${ready ? T("ep.public", "對外中")
      : (t.running ? T("ep.starting", "建立中…") : T("ep.inactive", "未啟用"))}`;
    $("#cf-body").hidden = !t.running;
    if (t.url) {
      $("#cf-url").textContent = maskKeyInUrl(t.url); $("#cf-url").dataset.full = t.url;
      const src = API.qrSrc(t.url), qr = $("#cf-qr");
      if (src) { qr.src = src; qr.hidden = false; } else qr.hidden = true;
    } else {
      $("#cf-url").textContent = T("ep.starting", "（建立中…）"); $("#cf-url").dataset.full = "";
      $("#cf-qr").hidden = true;
    }
    const cs = $("#cf-status");
    cs.className = "cf-status" + (t.error ? " err" : (t.status === "ready" ? " ok" : ""));
    cs.textContent = t.error ? I18N.L(t.status)
      : (t.status === "ready" ? (I18N.t("ep.tunnelReady") || t.status) : (I18N.L(t.status) || ""));
  }
  $("#sw-tunnel").addEventListener("change", async e => {
    const on = e.target.checked;
    if (on && !$("#sw-endpoint").checked) {        // 通道須先有端點服務
      e.target.checked = false;
      const cs = $("#cf-status"); cs.className = "cf-status err"; cs.textContent = T("ep.startFirst", "請先啟動端點服務");
      return;
    }
    applyTunnelState(await API.toggleTunnel(on));
  });
  // 後端建立通道為非同步：狀態/網址就緒時由 SSE "tunnel" 事件推回
  API.on("tunnel", t => applyTunnelState(t));
  function setEpState(on) {
    const c = $("#ep-state");
    c.className = "chip " + (on ? "chip-ok" : "chip-muted");
    c.innerHTML = `<span style="font-size:9px">●</span> ${on ? T("ep.running", "執行中") : T("ep.stopped", "已停止")}`;
  }
  function maskKeyInUrl(url) { return url.replace(/k=[^&]+/, "k=•••••"); }
  $("#sw-endpoint").addEventListener("change", async e => {
    await API.toggleEndpoint(e.target.checked, $("#ep-port").value.trim());
    renderEndpoint();                       // 重新同步埠/網址/金鑰（換埠會重建服務）
  });
  // 監聽埠變更：服務執行中 → 以新埠重建並刷新；未啟動 → 保留輸入值，啟動時才採用
  $("#ep-port").addEventListener("change", async () => {
    if (!$("#sw-endpoint").checked) return;
    await API.toggleEndpoint(true, $("#ep-port").value.trim());
    renderEndpoint();
  });
  $("#btn-reveal-key").addEventListener("click", () => {
    const k = $("#ep-key"); const showing = k.dataset.shown === "1";
    k.textContent = showing ? "••••••••••••" : (k.dataset.full || "");
    k.dataset.shown = showing ? "" : "1";
    $("#btn-reveal-key").textContent = showing ? T("ep.show", "顯示") : T("ep.hide", "隱藏");
  });
  $("#btn-newkey").addEventListener("click", async () => {
    const r = await API.regenKey(); $("#ep-key").dataset.full = r.key;
    $("#ep-key").dataset.shown = ""; $("#ep-key").textContent = "••••••••••••";
    $("#btn-reveal-key").textContent = T("ep.show", "顯示");
    $("#ep-url").dataset.full = r.url;
    $("#ep-url").textContent = r.running ? maskKeyInUrl(r.url)
      : T("ep.notRunning", "（服務未啟動）");
  });
  // 複製按鈕（資料來自 dataset.full）
  document.addEventListener("click", e => {
    const btn = e.target.closest("[data-copy]"); if (!btn) return;
    const t = $(btn.dataset.copy); const val = t.dataset.full || t.textContent;
    navigator.clipboard?.writeText(val); flash(btn, T("ep.copied", "已複製"));
  });
  function flash(btn, txt) { const o = btn.textContent; btn.textContent = txt; setTimeout(() => btn.textContent = o, 1200); }

  // ════════════════════════════════════════════════════════
  // 設定
  // ════════════════════════════════════════════════════════
  async function loadSettings() {
    const s = await API.getSettings();
    $("#set-scale").value = s.scale; $("#set-scale-val").textContent = s.scale + "%";
    applyScale(s.scale);
    segSet("#set-format", s.format); segSet("#set-vocab", s.vocab); segSet("#set-theme", s.theme || "light");
    // 同名檔案策略：後端只認 suffix / overwrite / hash，未知值一律回 suffix。
    segSet("#set-outnaming", s.outNaming || "suffix");
    _outNaming = s.outNaming || "suffix";
    // 輸出資料夾：顯示真實路徑（唯讀）。以前的這一列只有標題沒有值，
    // 使用者無從確認字幕到底寫到哪裡。
    if ($("#set-outputdir-path")) $("#set-outputdir-path").value = s.outputDir || "";
    // 斷點保留天數 + 目前留存的記錄（條數／占用／明細）
    if ($("#set-ckpt-days")) $("#set-ckpt-days").value = (s.checkpointDays ?? 30);
    refreshCheckpoints();
    applyTheme(s.theme || "light");                 // 啟動即套用深/淺色
    $("#set-mirror").value = s.mirror || "";
    $("#set-ffmpeg").value = s.ffmpeg || "";
    if (s.vad != null && $("#seg-vad")) {
      $("#seg-vad").value = s.vad; $("#seg-vad-val").textContent = (+s.vad).toFixed(2);
    }
    // 每段最長秒數：0/未設 → 視為上限（slider 顯示 30，後端依模型夾低）
    const cs = (+s.chunkSecs > 0) ? +s.chunkSecs : 30;
    if ($("#seg-chunk")) { $("#seg-chunk").value = cs; $("#seg-chunk-val").textContent = cs + "s"; }
    // 介面語言：選單回填 + 套用 i18n（含目前視圖標題）
    // B5.2: settings may still hold a legacy Chinese label; normalise to a
    // stable enum on read so the next save migrates it automatically.
    const uiLang = window.I18N ? I18N.normalize(s.uiLang || "hant") : "hant";
    $("#set-lang").value = uiLang;
    if (window.I18N) { I18N.setLang(uiLang); refreshViewTitle(); }
    applyModelMode(s.modelMode || "basic", false);   // 模型頁預設「基礎」
    // Wave 4 (B3): restore transcription-page controls. They used to reset on
    // every launch because only the settings-page keys were persisted.
    if (s.language != null && $("#sel-lang") &&
        [...$("#sel-lang").options].some(o => o.value === s.language)) {
      $("#sel-lang").value = s.language;
    }
    if ($("#sel-diar")) $("#sel-diar").value = diarValue(s);
    if ($("#sw-align")) $("#sw-align").checked = !!s.align;
    if ($("#hint-box") && s.hint != null) $("#hint-box").value = s.hint;
    if ($("#sw-trial")) $("#sw-trial").checked = !!s.trial;
    // 自動釋放已移到設定頁。顯示的是「生效值」：GPU 核心會強制關閉自動釋放
    // （Vulkan context 刻意常駐），此時開關置灰並說明原因，而不是讓它顯示
    // 「開」卻永遠不作用。
    if ($("#sw-auto-release")) {
      const sw = $("#sw-auto-release");
      const na = (s.autoReleaseRequested === true && s.autoRelease === false);
      sw.checked = s.autoRelease !== false;
      sw.disabled = na;
      const why = $("#auto-release-why");
      if (why) {
        why.hidden = !na;
        why.textContent = na
          ? T("set.autoReleaseNa", "目前使用 GPU 核心（常駐設計），不支援自動釋放；改用 CPU 核心時生效。")
          : "";
      }
    }
    // Wave 11：設定回填後同步試跑徽章與摺疊摘要 —— 否則重啟後明明勾著
    // 試跑，徽章卻是隱藏的，又回到「以為會完整轉檔」的老問題。
    syncTrialBadge();
    syncSegSummary();
  }
  // 說話者分離與人數合併成一個下拉：off / auto / 2..6。
  // 舊版是「開關 + 人數下拉」兩個控制項並排，使用者不知道人數隸屬哪個開關；
  // 合併後「開不開」與「幾個人」由同一個控制項表達，語義不再割裂。
  function diarOpts(v) {
    if (!v || v === "off") return { diarize: false, nSpeakers: "auto" };
    return { diarize: true, nSpeakers: v };
  }
  function diarValue(s) {
    if (!s || !s.diarize) return "off";
    const n = s.nSpeakers == null ? "auto" : String(s.nSpeakers);
    return n || "auto";
  }

  // Persist control changes as they happen (no need to press a save button).
  function bindPersisted(sel, key, read) {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("change", () => {
      try { API.setSettings({ [key]: read(el) }); } catch (e) {}
    });
  }
  bindPersisted("#sel-lang", "language", el => el.value);
  // 合併後的下拉要同時寫入 diarize 與 nSpeakers 兩個欄位，故不用 bindPersisted
  {
    const el = $("#sel-diar");
    if (el) {
      el.addEventListener("change", () => {
        try { API.setSettings(diarOpts(el.value)); } catch (e) {}
      });
    }
  }
  bindPersisted("#sw-align", "align", el => !!el.checked);
  bindPersisted("#sw-trial", "trial", el => !!el.checked);
  bindPersisted("#sw-auto-release", "autoRelease", el => !!el.checked);

  // Wave 12 / issue 1: explicit "release model memory" action. The backend
  // also auto-releases after a single-file run; this button is for users who
  // want to free the weights immediately (e.g. before launching another app).
  const releaseBtn = $("#btn-model-release");
  if (releaseBtn) releaseBtn.addEventListener("click", async () => {
    releaseBtn.disabled = true;
    try {
      const r = await API.releaseModel();
      modelMsg(I18N.t(r && r.ok ? "model.releaseDone" : "model.releaseBusy"),
               r && r.ok ? "info" : "warn");
      await refreshStatus();
    } catch (e) {
      modelMsg(I18N.t("model.releaseBusy"), "warn");
    } finally { releaseBtn.disabled = false; }
  });

  // ── Wave 11／問題 5 + 6 ─────────────────────────────────────────────
  // 切分參數改成 <details> 後，收起時只剩一列標題；摘要顯示目前生效的
  // VAD / 段長，讓使用者不必展開也知道設定。試跑開關也藏進面板了，所以
  // 另在「開始轉換」旁常駐一個醒目徽章，勾選時立刻出現，避免誤以為會完整轉檔。
  function syncSegSummary() {
    const el = $("#seg-summary-val"); if (!el) return;
    const vad = $("#seg-vad") ? (+$("#seg-vad").value).toFixed(2) : "0.50";
    const chunk = $("#seg-chunk") ? $("#seg-chunk").value : "30";
    el.textContent = T("file.segSummary", null, { vad, chunk });
  }
  function syncTrialBadge() {
    const sw = $("#sw-trial"), badge = $("#trial-badge"), run = $("#btn-run");
    const on = !!(sw && sw.checked);
    if (badge) badge.hidden = !on;
    if (run) run.classList.toggle("trial-on", on);
  }
  if ($("#sw-trial")) $("#sw-trial").addEventListener("change", syncTrialBadge);
  if ($("#trial-badge")) {
    const badge = $("#trial-badge");
    badge.title = T("file.trial", "試跑前 5 分鐘");
    // 點徽章直接展開面板並聚焦開關：看得到提示，也能馬上改回來。
    badge.addEventListener("click", () => {
      const panel = $("#seg-panel"); if (panel) panel.open = true;
      const sw = $("#sw-trial"); if (sw) sw.focus();
    });
  }
  $("#seg-vad") && $("#seg-vad").addEventListener("input", syncSegSummary);
  $("#seg-chunk") && $("#seg-chunk").addEventListener("input", syncSegSummary);
  syncSegSummary();
  syncTrialBadge();

  const hintEl = $("#hint-box");
  if (hintEl) {
    let hintTimer = null;
    hintEl.addEventListener("input", () => {
      clearTimeout(hintTimer);
      hintTimer = setTimeout(() => {
        try { API.setSettings({ hint: hintEl.value }); } catch (e) {}
      }, 400);
    });
  }
  function segSet(sel, v) { $$(sel + " button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); }
  // 介面縮放：用 CSS zoom（Chromium/WebView2 支援）整體縮放，px 版面也能等比生效。
  function applyScale(pct) { document.body.style.zoom = (Math.max(50, Math.min(200, +pct || 100)) / 100); }

  $("#set-scale").addEventListener("input", e => {
    const v = +e.target.value; $("#set-scale-val").textContent = v + "%"; applyScale(v); API.setSettings({ scale: v });
  });
  // 切分參數已移至識別頁；仍寫回同一組設定鍵，後端與 CLI 相容。
  $("#seg-vad") && $("#seg-vad").addEventListener("input", e => {
    const v = +e.target.value; $("#seg-vad-val").textContent = v.toFixed(2);
    API.setSettings({ vad: v });
    markPreviewStale();
  });
  $("#seg-chunk") && $("#seg-chunk").addEventListener("input", e => {
    const v = +e.target.value; $("#seg-chunk-val").textContent = v + "s";
    API.setSettings({ chunkSecs: v });
    markPreviewStale();
  });

  function markPreviewStale() {
    const el = $("#seg-verdict");
    if (el) { el.textContent = I18N.t("file.previewEmpty"); el.dataset.state = ""; }
  }
  function renderPreviewStat(p) {
    const el = $("#seg-verdict"); if (!el) return;
    if (!p || p.cancelled) { markPreviewStale(); return; }
    const L = p.lengths || {};
    const warn = p.verdict === "yellow";
    el.dataset.state = warn ? "yellow" : "green";
    // One plain-language verdict on screen; the full distribution stays
    // available on hover and in the run log instead of flooding the panel.
    el.innerHTML = `<b>${I18N.t(warn ? "file.previewWarn" : "file.previewOk")}</b> · ` +
                   T("split.stats", null, { groups: p.groupCount || 0, max: L.max ?? "—" });
    el.title = `p50 ${L.p50}s / p90 ${L.p90}s / p99 ${L.p99}s / max ${L.max}s · ` +
               T("split.overLimit", null, { pct: (p.overLimitRatio * 100).toFixed(1) }) + " · " +
               T("split.veryShort", null, { pct: (p.shortRatio * 100).toFixed(1) }) + " · " +
               T("split.silence", null, { pct: (p.silenceRatio * 100).toFixed(0) }) + " · " +
               T("split.asrCalls", null, { n: p.estimatedAsrCallsPerHour });
  }
  API.on("vad-preview", renderPreviewStat);
  $("#btn-vad-preview") && $("#btn-vad-preview").addEventListener("click", async () => {
    if (!picked) return;
    const btn = $("#btn-vad-preview");
    markBusy("preview", true); btn.dataset.label = btn.textContent;
    btn.textContent = T("split.prechecking", "預檢中…");
    try {
      const r = await API.vadPreview({
        file: picked.file || picked,
        vadThreshold: $("#seg-vad") ? $("#seg-vad").value : "",
      });
      renderPreviewStat(r);
      logLine(T("split.precheckOk", null, { n: r.groupCount || 0 }));
    } catch (e) {
      logLine(T("split.precheckFailed", null, { err: e.message || e }));
    } finally {
      markBusy("preview", false);
      btn.textContent = btn.dataset.label || I18N.t("file.previewRun");
    }
  });
  // segmented 控制：點擊切換 + 回寫設定
  [["#set-format", "format"], ["#set-vocab", "vocab"], ["#set-theme", "theme"],
   ["#set-outnaming", "outNaming"]].forEach(([sel, key]) => {
    $(sel).addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      $$(sel + " button").forEach(x => x.classList.toggle("on", x === b));
      API.setSettings({ [key]: b.dataset.v });
      if (key === "theme") applyTheme(b.dataset.v);
      if (key === "outNaming") {      // 批次頁那張表同步更新
        _outNaming = b.dataset.v; renderBatchConfig();
      }
    });
  });
  $("#set-mirror").addEventListener("change", e => API.setSettings({ mirror: e.target.value.trim() }));
  // 外觀主題：淺/深/跟隨系統。system 解析成實際 light/dark 掛到 <html>，並監聽 OS 變化。
  // 視窗標題列深淺由後端（app_webview）依同一設定同步，故只需把偏好寫回 setSettings。
  let _mqHandler = null;
  function resolveTheme(t) {
    if (t === "dark" || t === "light") return t;
    return (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = resolveTheme(t);   // 實際 light/dark
    document.documentElement.dataset.themePref = t;             // 原始偏好
    if (window.matchMedia) {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      if (_mqHandler) { try { mq.removeEventListener("change", _mqHandler); } catch (e) {} _mqHandler = null; }
      if (t === "system") {
        _mqHandler = () => { document.documentElement.dataset.theme = resolveTheme("system"); };
        try { mq.addEventListener("change", _mqHandler); } catch (e) {}
      }
    }
  }
  $("#set-ffmpeg").addEventListener("change", e => API.setSettings({ ffmpeg: e.target.value.trim() }));
  // 介面語言切換：寫回設定 + 即時套用 i18n（含目前視圖標題、模型頁動態文字）
  $("#set-lang").addEventListener("change", e => {
    const v = window.I18N ? I18N.normalize(e.target.value) : e.target.value;
    API.setSettings({ uiLang: v });
    if (window.I18N) { I18N.setLang(v); refreshViewTitle(); }
    // H6/H7: backend-generated labels must be re-rendered on language change.
    if (_curView === "model") renderModel();
    if (_curView === "settings") renderHealth();
    if (_curView === "file") loadLanguages();
  });
  // 檢查更新：開啟 GitHub Releases 頁（系統瀏覽器）
  $("#btn-check-update") && $("#btn-check-update").addEventListener("click", async () => {
    const btn = $("#btn-check-update"); const o = btn.textContent;
    btn.disabled = true; btn.textContent = T("set.opening", "開啟中…");
    try { await API.checkUpdate(); } catch (e) {}
    setTimeout(() => { btn.disabled = false; btn.textContent = o; }, 1500);
  });

  // 暫存清理：只清本程式在 %TEMP% 建的目錄（asr_web_* / asr_api_*），
  // 且只清 10 分鐘前的，不會動到正在跑的那一份。
  $("#btn-clean-temp") && $("#btn-clean-temp").addEventListener("click", async () => {
    const btn = $("#btn-clean-temp");
    const span = btn.querySelector("[data-i18n]");
    const state = $("#temp-state");
    btn.disabled = true;
    if (span) { span.dataset.i18n = "set.tempCleaning"; I18N.apply(btn); }
    try {
      const r = await API.cleanupTemp();
      const mb = Math.round((r.freed || 0) / 1048576);
      if (state) {
        // set.tempDone 是後端 message key（ui_text.TEMPLATES），走 renderMsg
        // 而不是 T()：T() 只查靜態字典 D。
        state.textContent = (r.ok === false)
          ? (r.message || "")
          : ((window.I18N && I18N.renderMsg("set.tempDone",
               { n: r.removed || 0, mb }, "")) || "");
      }
    } catch (e) {
      if (state) state.textContent = String(e.message || e);
    } finally {
      btn.disabled = false;
      if (span) { span.dataset.i18n = "set.tempClean"; I18N.apply(btn); }
    }
  });

  // 斷點記錄：摘要一行（條數 + 占用）可展開明細，右側一鍵清除。
  // 摘要與明細都是動態文字，不能掛 data-i18n（會被 I18N.apply 覆蓋成 key），
  // 所以改在語言切換後重繪一次。
  function ckStateLabel(s) {
    const map = {
      done: "batch.stDone", running: "batch.stRunning", new: "batch.stPending",
      paused: "set.ckptStPaused", orphan: "set.ckptStOrphan",
    };
    return map[s] ? T(map[s], s) : (s || "—");
  }
  function ckWhen(ts) {
    if (!ts) return "—";
    try {
      const d = new Date(ts * 1000);
      const p = n => String(n).padStart(2, "0");
      const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
      // 今年只顯示月/日 + 時分，省下 4 個字元；跨年才帶完整年份，
      // 否則「最後活動」欄永遠被省略號截斷。
      return d.getFullYear() === new Date().getFullYear()
        ? `${p(d.getMonth() + 1)}/${p(d.getDate())} ${hm}`
        : `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())}`;
    } catch (e) { return "—"; }
  }
  async function refreshCheckpoints() {
    const sum = $("#ckpt-summary"), list = $("#ckpt-list"), clr = $("#btn-clear-ckpt");
    if (!sum || !list) return;
    let r;
    try { r = await API.getCheckpoints(); } catch (e) { return; }
    if (r.retentionDays != null && $("#set-ckpt-days") &&
        document.activeElement !== $("#set-ckpt-days")) {
      $("#set-ckpt-days").value = r.retentionDays;
    }
    const n = r.count || 0;
    sum.textContent = n
      ? T("set.ckptSummary", null, { n, mb: fmtBytes(r.bytes || 0) })
      : T("set.ckptEmpty", "目前沒有斷點記錄");
    if (clr) clr.disabled = !n;
    if (!n) { list.innerHTML = ""; return; }
    const head = `<div class="ck-row head">`
      + `<span>${esc(T("set.ckptColTask", "任務／輸出檔"))}</span>`
      + `<span>${esc(T("set.ckptColState", "狀態"))}</span>`
      + `<span>${esc(T("set.ckptColTime", "最後活動"))}</span>`
      + `<span class="ck-num">${esc(T("set.ckptColSize", "占用"))}</span></div>`;
    const rows = (r.items || []).map(it => {
      const name = it.name || it.task;
      return `<div class="ck-row">`
        + `<span class="ck-name" title="${esc(name)}">${esc(name)}</span>`
        + `<span>${esc(ckStateLabel(it.status))}</span>`
        + `<span>${esc(ckWhen(it.updatedAt))}</span>`
        + `<span class="ck-num">${esc(it.bytes ? fmtBytes(it.bytes) : "—")}</span></div>`;
    }).join("");
    const extra = (r.total || 0) - (r.items || []).length;
    list.innerHTML = head + rows + (extra > 0
      ? `<div class="ck-more">${esc(T("set.ckptMore", null, { n: extra }))}</div>` : "");
  }
  $("#set-ckpt-days") && $("#set-ckpt-days").addEventListener("change", async e => {
    let v = parseInt(e.target.value, 10);
    if (!isFinite(v) || v < 0) v = 0;
    v = Math.min(3650, v);
    e.target.value = v;
    await API.setSettings({ checkpointDays: v });
    refreshCheckpoints();
  });
  $("#btn-clear-ckpt") && $("#btn-clear-ckpt").addEventListener("click", async () => {
    const btn = $("#btn-clear-ckpt");
    const span = btn.querySelector("[data-i18n]");
    btn.disabled = true;
    if (span) { span.dataset.i18n = "set.ckptClearing"; I18N.apply(btn); }
    try {
      await API.clearCheckpoints();
      await refreshCheckpoints();
    } catch (e) {
      logLine("✕ " + (e.message || e));
    } finally {
      if (span) { span.dataset.i18n = "set.ckptClear"; I18N.apply(btn); }
      refreshCheckpoints();      // 清除失敗時把按鈕狀態還原成實際條數
    }
  });
  if (window.I18N && I18N.onAfterLang) I18N.onAfterLang(() => refreshCheckpoints());

  // ── 共用工具 ────────────────────────────────────────────
  function fmtClock(sec) {
    sec = Math.max(0, Math.round(sec));
    const m = String(Math.floor(sec / 60)).padStart(2, "0");
    const s = String(sec % 60).padStart(2, "0");
    return `${m}:${s}`;
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // ── 錄製轉換：MediaRecorder + 停頓偵測(VAD)，分段上傳辨識 ──────
  //   127.0.0.1/localhost 屬安全情境，getUserMedia 可用（不需 HTTPS）。
  //   說完停頓 → 切段 → 上傳 /api/transcribe → 逐段附加即時字幕。
  const REC = {
    on: false, sec: 0, timer: null, raf: 0, firstResult: true,
    stream: null, ctx: null, analyser: null, recorder: null, chunks: [],
    speech: false, silentSince: 0, segStart: 0,
    segs: [],            // 累積辨識結果（含合成時間軸），供清除／存檔／即時存檔
    fileHandle: null,    // 即時存檔的 File System Access 檔案 handle（若支援）
  };
  const SILENCE_MS = 2200, MIN_SEG_MS = 500, MAX_SEG_MS = 20000, VAD_THRESH = 0.014;
  const REC_LINE_SECS = 5;     // 錄製無精確時間戳 → 每段合成固定 5s（比照 app.py）

  $("#rec-btn").addEventListener("click", () => REC.on ? stopRec() : startRec());

  function recSupported() {
    return navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder;
  }

  // ── 麥克風權限狀態（整合進頁面，而非僅依賴瀏覽器彈窗）──────────────
  //   瀏覽器原生「允許麥克風」提示由 WebView2/Edge 控制、無法被頁面取代；
  //   但我們用 Permissions API 把「已授權／將詢問／被拒」狀態與指引顯示在頁內，
  //   被拒時給明確的頁內排除說明，不再只是一行錯誤。
  async function refreshMicPerm() {
    const chip = $("#rec-perm"), help = $("#rec-perm-help");
    if (!chip) return;
    if (!recSupported()) {
      chip.hidden = false; chip.className = "chip chip-muted";
      chip.textContent = T("record.unsupported", "不支援錄音");
      return;
    }
    let state = "prompt";
    try {
      if (navigator.permissions && navigator.permissions.query) {
        const st = await navigator.permissions.query({ name: "microphone" });
        state = st.state;                       // granted / denied / prompt
        st.onchange = () => refreshMicPerm();    // 狀態變更即時更新
      }
    } catch (e) { state = "prompt"; }
    chip.hidden = false;
    if (state === "granted") {
      chip.className = "chip chip-ok"; chip.textContent = T("record.permGranted", "麥克風已授權");
      if (help) help.hidden = true;
    } else if (state === "denied") {
      chip.className = "chip chip-live"; chip.textContent = T("record.permDeniedShort", "麥克風被拒");
      if (help) help.hidden = false;
    } else {
      chip.className = "chip chip-muted"; chip.textContent = T("record.permPrompt", "按麥克風時將請求授權");
      if (help) help.hidden = true;
    }
  }

  // ── 麥克風裝置列舉（瀏覽器原生；對應 app.py 的 sounddevice 裝置選擇）──────
  //   裝置標籤在未授權前為空字串 → 首次 getUserMedia 後再列舉一次才有名稱。
  // 語言切換後就地更新「預設麥克風」選項（不重新列舉，避免打斷已選裝置）。
  function relocalizeMics() {
    const sel = $("#rec-mic"); if (!sel) return;
    [...sel.options].forEach(o => {
      if (o.value === "") o.textContent = T("record.defaultMic", "預設麥克風");
      else if (o.dataset.idx) o.textContent = T("record.micN", null, { n: +o.dataset.idx });
    });
  }
  async function enumerateMics() {
    const sel = $("#rec-mic");
    if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      const cur = sel.value;
      const devs = await navigator.mediaDevices.enumerateDevices();
      const mics = devs.filter(d => d.kind === "audioinput");
      sel.innerHTML = "";
      const def = document.createElement("option");
      def.value = ""; def.textContent = T("record.defaultMic", "預設麥克風");
      sel.appendChild(def);
      mics.forEach((d, i) => {
        const o = document.createElement("option");
        o.value = d.deviceId;
        if (d.label && !/^Microphone \d+$/i.test(d.label)) o.dataset.label = d.label;
        else o.dataset.idx = String(i + 1);
        o.textContent = d.label || T("record.micN", null, { n: i + 1 });
        sel.appendChild(o);
      });
      if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
    } catch (e) { /* 列舉失敗 → 保留預設選項 */ }
  }
  $("#rec-mic-refresh") && $("#rec-mic-refresh").addEventListener("click", enumerateMics);
  if (navigator.mediaDevices) {
    try { navigator.mediaDevices.addEventListener("devicechange", enumerateMics); } catch (e) {}
  }

  async function startRec() {
    if (!recSupported()) { recNote(T("record.noApi", "此環境不支援錄音 API。"), true); return; }
    // 依選定的麥克風建立音訊約束（空值 → 系統預設裝置）。
    const micId = $("#rec-mic") ? $("#rec-mic").value : "";
    const audioConstraint = { echoCancellation: true, noiseSuppression: true };
    if (micId) audioConstraint.deviceId = { exact: micId };
    try {
      REC.stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraint });
    } catch (err) {
      // 被拒 / 無裝置 → 頁內顯示權限指引，不只丟一行錯誤
      const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
      recNote((denied ? T("record.micDenied", "麥克風權限被拒。")
                       : T("record.micFailed", "無法取得麥克風：")) + (err.message || err), true);
      refreshMicPerm();
      if (denied && $("#rec-perm-help")) $("#rec-perm-help").hidden = false;
      return;
    }
    enumerateMics();    // 授權後重新列舉 → 取得真實裝置名稱
    refreshMicPerm();
    REC.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = REC.ctx.createMediaStreamSource(REC.stream);
    REC.analyser = REC.ctx.createAnalyser(); REC.analyser.fftSize = 1024;
    src.connect(REC.analyser);
    REC.on = true; REC.sec = 0;
    $("#rec-btn").classList.add("recording");
    const lw = $("#rec-live-wave"); lw.hidden = false;
    lw.innerHTML = Array.from({ length: 28 }, () => `<div class="b" style="height:4px"></div>`).join("");
    $("#rec-timer").textContent = "00:00";
    REC.timer = setInterval(() => { REC.sec++; $("#rec-timer").textContent = fmtClock(REC.sec); }, 1000);
    recNote(T("record.listening", "聆聽中…說完停頓約 2 秒會自動辨識；再按一次結束並完成最後一段。"));
    startSeg(); monitor();
  }
  function startSeg() {
    REC.chunks = []; REC.speech = false; REC.silentSince = 0; REC.segStart = Date.now();
    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]
      .find(m => MediaRecorder.isTypeSupported(m)) || "";
    REC.recorder = mime ? new MediaRecorder(REC.stream, { mimeType: mime })
                        : new MediaRecorder(REC.stream);
    REC.recorder.ondataavailable = e => { if (e.data && e.data.size) REC.chunks.push(e.data); };
    REC.recorder.onstop = onSegStop;
    REC.recorder.start();
  }
  function cutSeg() { if (REC.recorder && REC.recorder.state === "recording") REC.recorder.stop(); }
  async function onSegStop() {
    const dur = Date.now() - REC.segStart;
    const blob = new Blob(REC.chunks, { type: REC.chunks[0] ? REC.chunks[0].type : "audio/webm" });
    const ok = REC.speech && dur >= MIN_SEG_MS && blob.size > 1200;
    if (REC.on) startSeg(); else teardownRec();    // 先續錄，避免上傳期間漏話
    if (ok) {
      try {
        const file = new File([blob], "recording.webm", { type: blob.type });
        const recLang = $("#rec-lang") ? $("#rec-lang").value : "";
        const res = await API.transcribe({ file, language: recLang || null, diarize: false, align: false });
        (res.segments || []).forEach(s => { if (s.text && s.text.trim()) appendRecLine(s.text.trim()); });
      } catch (err) { recNote(T("record.asrFailed", null, { err: err.message || err }), true); }
    }
  }
  function monitor() {
    const buf = new Uint8Array(REC.analyser.fftSize);
    const bars = $$(".b", $("#rec-live-wave"));
    const tick = () => {
      if (!REC.on) return;
      REC.analyser.getByteTimeDomainData(buf);
      let sum = 0; for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; sum += v * v; }
      const rms = Math.sqrt(sum / buf.length);
      const base = Math.min(34, rms * 260);
      bars.forEach(b => b.style.height = Math.max(4, base * (0.5 + Math.random() * 0.5)).toFixed(0) + "px");
      const now = Date.now();
      if (rms >= VAD_THRESH) { REC.speech = true; REC.silentSince = 0; }
      else if (REC.speech) {
        if (!REC.silentSince) REC.silentSince = now;
        else if (now - REC.silentSince >= SILENCE_MS) cutSeg();   // 停頓 → 切段
      }
      if (Date.now() - REC.segStart >= MAX_SEG_MS && REC.speech) cutSeg();
      REC.raf = requestAnimationFrame(tick);
    };
    REC.raf = requestAnimationFrame(tick);
  }
  function stopRec() {
    REC.on = false; $("#rec-btn").classList.remove("recording");
    if (REC.raf) cancelAnimationFrame(REC.raf);
    clearInterval(REC.timer);
    $("#rec-live-wave").hidden = true;
    recNote(T("record.stopped", "已停止。"));
    cutSeg();    // 收尾段（onSegStop 會上傳並 teardown）
  }
  function teardownRec() {
    try { REC.stream && REC.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    try { REC.ctx && REC.ctx.close(); } catch (e) {}
    REC.stream = REC.ctx = REC.analyser = REC.recorder = null;
  }
  function recNote(msg, warn) {
    const el = $(".rec-note");
    if (el) { el.textContent = msg; el.style.color = warn ? "var(--live)" : "var(--muted)"; }
  }
  function appendRecLine(text) {
    const host = $("#rec-subs");
    if (REC.firstResult) { host.innerHTML = ""; REC.firstResult = false; }
    // 合成時間軸：錄製無精確時間戳，每段固定 5s、段間 0.1s（比照 app.py _on_rt_save）。
    const start = REC.segs.length ? REC.segs[REC.segs.length - 1].end + 0.1 : 0;
    const seg = { start, end: start + REC_LINE_SECS, speaker: null, text };
    REC.segs.push(seg);
    const el = document.createElement("div");
    el.className = "sub-card";
    el.innerHTML = `<span class="tc">${fmtClock(REC.sec)}</span>
      <div class="body"><div class="txt">${escapeHtml(text)}</div></div>`;
    host.appendChild(el);
    host.scrollTop = host.scrollHeight;
    $("#rec-save").disabled = false;
    if (REC.fileHandle) recAutosaveWrite();    // 即時存檔：每段附加後即落盤
  }

  // ── 清除／儲存／即時存檔（對應 app.py 錄製頁的清除、儲存 SRT、即時追加保存）──
  function recClear() {
    REC.segs = []; REC.firstResult = true;
    $("#rec-subs").innerHTML = `<div class="sub-card"><span class="tc">— · —</span>`
      + `<div class="body"><div class="txt" style="color:var(--muted)">開始錄音後，辨識結果會逐段出現在這裡。</div></div></div>`;
    $("#rec-save").disabled = true;
  }
  async function recSave() {
    if (!REC.segs.length) return;
    let fmt = "srt";
    try { fmt = (await API.getSettings()).format || "srt"; } catch (e) {}
    const text = fmt === "txt" ? buildTxt(REC.segs) : buildSrt(REC.segs);
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "").slice(0, 14);
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `realtime_${stamp}` + (fmt === "txt" ? ".txt" : ".srt");
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  $("#rec-clear") && $("#rec-clear").addEventListener("click", recClear);
  $("#rec-save") && $("#rec-save").addEventListener("click", recSave);

  // 即時存檔：用 File System Access API（WebView2/Edge 支援）持續把目前累積的
  // 字幕寫入使用者選定的檔案 —— 重現 app.py「即時追加保存，可隨時中斷不遺失」。
  // 不支援的環境隱藏此開關（仍可用「儲存字幕」一次性匯出）。
  const FS_SAVE_OK = typeof window.showSaveFilePicker === "function";
  if (FS_SAVE_OK) $("#rec-autosave-wrap").hidden = false;
  $("#rec-autosave") && $("#rec-autosave").addEventListener("change", async e => {
    if (!e.target.checked) { REC.fileHandle = null; return; }
    try {
      let fmt = "srt";
      try { fmt = (await API.getSettings()).format || "srt"; } catch (err) {}
      const ext = fmt === "txt" ? "txt" : "srt";
      REC.fileHandle = await window.showSaveFilePicker({
        suggestedName: `realtime.${ext}`,
        types: [{ description: T("record.subtitleFile", "字幕檔"),
                 accept: { "text/plain": ["." + ext] } }],
      });
      await recAutosaveWrite();           // 立即寫一次（含既有片段）
      recNote(T("record.autosaveOn", "即時存檔已啟用：每段辨識完成後會自動寫入所選檔案。"));
    } catch (err) {                         // 使用者取消選檔 → 還原開關
      REC.fileHandle = null; e.target.checked = false;
    }
  });
  async function recAutosaveWrite() {
    if (!REC.fileHandle) return;
    try {
      let fmt = "srt";
      try { fmt = (await API.getSettings()).format || "srt"; } catch (e) {}
      const text = fmt === "txt" ? buildTxt(REC.segs) : buildSrt(REC.segs);
      const w = await REC.fileHandle.createWritable();   // 截斷重寫（內容小，安全可靠）
      await w.write(text); await w.close();
    } catch (e) { recNote(T("record.autosaveFailed", null, { err: e.message || e }), true); REC.fileHandle = null; }
  }

  // ── 啟動 ────────────────────────────────────────────────
  // 載入完成 → 更新就緒燈 + 語言清單（依引擎）；若為「就地下載並載入」流程 → 收尾
  API.on("status", async (s) => {
    refreshStatus(); loadLanguages();
    // Wave 12: an auto-release also emits status. Tell the user why the
    // readiness light went dark instead of leaving them guessing.
    if (s && s.released) modelMsg(I18N.t("model.releaseDone"), "info");
    let ready = s && s.modelReady;
    if (ready == null) { try { ready = (await API.getStatus()).modelReady; } catch (e) {} }
    if (_modelLoading && ready) {
      _modelLoading = false; markBusy("model", false);
      setModelProg(100, T("run.done", "完成"));
      setTimeout(() => { $("#model-progress").hidden = true; }, 800);
      modelMsg(I18N.t("model.readySwitched"), "info");
      renderHealth();                 // 自檢色點刷新
      switchView("file");             // 完成 → 進入轉錄頁
    } else if (s && s.error && _modelLoading) {
      _modelLoading = false; markBusy("model", false);
      $("#model-progress").hidden = true;
      modelMsg(I18N.t("model.loadFailed", {error: s.error}), "warn");
      updateLoadBtn();
    }
  });
  (async function init() {
    await API.ready;
    await refreshStatus();
    await loadLanguages();
    await loadSettings();
    await renderEndpoint();
    // 開始頁決策：目前選擇的模型已就緒 → 直接進「語音轉文字」；否則停在「模型」頁，
    // 讓使用者先確認硬體＋選擇模型（可改 Whisper 等），按「下載並載入」才下載。
    let ready = false;
    try { ready = !!(await API.getStatus()).selectedReady; } catch (e) {}
    switchView(ready ? "file" : "model");
    console.info("[app] ready, mode =", API.mode, "selectedReady =", ready);
  })();
})();
