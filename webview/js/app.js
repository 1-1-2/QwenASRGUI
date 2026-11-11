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
    const msg = $("#catalog-msg");
    try {
      const st = await API.getModelCatalog();
      const bundles = st.bundles || [];
      const incomplete = st.incomplete || [];
      const summary = $("#catalog-summary");
      if (summary) {
        const mirrorTxt = st.mirror
          ? T("dl.mirror", null, { name: st.mirror })
          : T("dl.mirrorOfficial", "目前鏡像：官方（未設定）");
        summary.textContent = T("dl.summary", null, {
          mirror: mirrorTxt,
          done: st.complete ? st.complete.length : 0,
          total: bundles.length,
          missing: incomplete.length,
          used: fmtBytes(st.total_bytes_on_disk),
          root: st.model_root || "",
        });
      }
      const sel = $("#catalog-import-bundle");
      if (sel) {
        sel.innerHTML = bundles.map(b =>
          `<option value="${esc(b.id)}">${esc(window.I18N ? I18N.L(b.label) : b.label)}</option>`).join("");
      }
      host.innerHTML = bundles.map(b => {
        const badge = b.complete
          ? `<span class="chip" style="color:var(--ok,#1a7f45)">${T("dl.badgeComplete", "完整")}</span>`
          : `<span class="chip chip-muted">${T("dl.badgeMissing", null,
              { missing: b.total_count - b.present_count, total: b.total_count })}</span>`;
        // models.json already writes size_hint as "约 1.2 GB". Wrapping that
        // in another "約 …（估計）" produced "約 约 1.2 GB（估計）", so strip a
        // leading 约/約 before formatting.
        const hint = v => {
          const s = String(v || "").trim().replace(/^[约約]\s*/, "");
          return s ? T("dl.estimated", null, { size: esc(s) }) : "";
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
          const state = f.present ? "✓" : ("✗ " + esc(f.problem || T("dl.missing", "缺失")));
          return `<div style="display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:12px;font-family:var(--font-mono)">
            <span style="width:14px;color:${f.present ? "var(--ok,#1a7f45)" : "var(--warn,#b26a00)"}">${f.present ? "✓" : "✗"}</span>
            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
            <span style="color:var(--ink-2)">${size}</span>
            <span style="color:var(--ink-2);min-width:96px;text-align:right">${sha}</span>
            <span style="color:var(--ink-2);min-width:150px;text-align:right">${f.present ? "" : state}</span>
          </div>`;
        }).join("");
        return `<details style="border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:6px">
          <summary style="cursor:pointer;font-size:13px;display:flex;gap:8px;align-items:center">
            <span style="flex:1">${esc(window.I18N ? I18N.L(b.label) : b.label)}</span>${approx}${badge}
          </summary>
          <div style="margin-top:6px;color:var(--ink-2);font-size:12px">${T("dl.target", null, {
            dir: esc(b.target_dir), provider: esc(b.provider || ""),
            optional: b.optional ? T("dl.optional", " · 選用") : "" })}</div>
          ${files}
        </details>`;
      }).join("") || `<div style="color:var(--ink-2)">${T("dl.empty", "無資料")}</div>`;
    } catch (e) {
      host.innerHTML = `<div style="color:var(--warn,#b26a00)">${T("dl.readFailed", null, { err: esc(e.message || e) })}</div>`;
    }
  }
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
  let batchUploadErrors = [], logExpanded = false;

  function markBusy(scope, on) {
    if (on) UI_BUSY.add(scope); else UI_BUSY.delete(scope);
    syncActionButtons();
  }
  function syncActionButtons() {
    const busy = UI_BUSY.size > 0;
    const run = $("#btn-run");
    if (run) {
      run.disabled = busy || !picked;
      const span = run.querySelector("[data-i18n]");
      if (span && window.I18N) {
        span.dataset.i18n = UI_BUSY.has("transcribe") ? "file.running" : "file.run";
        I18N.apply(run);
      }
    }
    const prev = $("#btn-vad-preview");
    if (prev && !UI_BUSY.has("preview")) prev.disabled = busy || !picked;
    const loadTxt = $("#btn-load-txt"); if (loadTxt) loadTxt.disabled = busy;
    const add = $("#btn-batch-add"); if (add) add.disabled = busy || batchRunning;
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
    drop.innerHTML = `<span class="file-chip">${escapeHtml(f.name)}
      ${meta ? `<span class="meta">${meta}</span>` : ""}
      <button class="x" title="${T("batch.remove", "移除")}">✕</button></span>`;
    drop.querySelector(".x").addEventListener("click", ev => { ev.stopPropagation(); setFile(null); });
    syncActionButtons();
  }
  function renderDropEmpty() {
    drop.innerHTML = `<div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg></div>
      <div class="big">${T("file.dropBigHtml", "拖曳音訊到此，或<b>點擊選擇檔案</b>")}</div>
      <div class="sub">${T("file.dropSubHtml", "支援 mp3 / wav / m4a / 影片音軌")}</div>`;
  }

  // 桌面：原生對話框；web/mock：fallback 到 <input type=file>
  drop.addEventListener("click", async () => {
    if (drop.classList.contains("has-file")) return;
    const native = await API.pickFile();
    if (native) setFile(native);
    else hiddenFile.click();
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
  $("#btn-run").addEventListener("click", async () => {
    if (!picked || running) return;
    running = true;
    markBusy("transcribe", true);
    $("#result").hidden = true;
    showProgress(0, T("run.preparing", "準備中…"));
    resetLive();
    logLine(T("run.started", null, { name: picked.name }));
    try {
      const res = await API.transcribe({
        path: picked.path, file: picked.file || picked,
        language: $("#sel-lang").value || null,
        diarize: $("#sw-diar").checked,
        nSpeakers: $("#sel-spk").value,
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
      $("#btn-open-dir").disabled = false;
      $("#btn-save-sub").disabled = false;
    } catch (err) {
      showProgress(0, "");
      $("#progress").hidden = true;
      logLine("✕ " + (err.message || err));
    } finally {
      running = false;
      markBusy("transcribe", false);
    }
  });

  // 進度分派：模型就地載入 → 模型頁；服務端批次 → 由 batch SSE 事件獨自負責；
  // mock 批次 → 當前批次列；否則 → 單檔進度條。
  API.on("progress", ({ pct, status }) => {
    if (_modelLoading) {
      setModelProg(pct, status);
    } else if (batchRunning) {
      // Server-side queue publishes its own "batch" events; do not paint the
      // single-file progress bar with a task belonging to another view.
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
    [["#sw-align", cap.alignAvailable], ["#sw-diar", cap.diarAvailable]]
      .forEach(([sel, available]) => {
        const el = $(sel);
        if (!el || available === undefined) return;
        el.disabled = !available;
        const lab = el.closest("label");
        if (lab) lab.style.opacity = available ? "" : "0.45";
        if (!available) lab.title = I18N.t("file.enrichMissing");
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

  function showProgress(pct, status) {
    $("#progress").hidden = false;
    $("#prog-bar").style.width = pct + "%";
    $("#prog-pct").textContent = Math.round(pct) + "%";
    if (status != null) $("#prog-status").textContent =
      window.I18N ? I18N.L(status) : status;
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
  // Phase labels are resolved through i18n at render time; the map only holds
  // the key + Hant fallback so a language switch never leaves stale Hant text.
  const PHASE_TEXT = {
    running: "run.running", paused: "run.paused",
    done: "run.done", preparing: "run.preparingShort",
  };
  API.on("progress-detail", (d) => {
    if (!d) return;
    const total = Number(d.totalSec) || 0, done = Number(d.doneSec) || 0;
    const elTime = $("#prog-time"), elGroups = $("#prog-groups"),
          elEta = $("#prog-eta"), elPhase = $("#prog-phase");
    if (elTime) elTime.textContent = T("run.processed", null,
      { done: fmtDur(done), total: total ? fmtDur(total) : "—" });
    if (elGroups) elGroups.textContent = T("run.groups", null, { n: d.groups ?? 0 });
    if (elEta) elEta.textContent = d.etaSec == null
      ? T("run.remainingUnknown", "剩餘 —")
      : T("run.remaining", null, { eta: fmtDur(d.etaSec) });
    if (elPhase) {
      const ph = PHASE_TEXT[d.phase];
      elPhase.textContent = ph ? T(ph) : (d.phase || T("run.running"));
    }
    // Queue view: keep the overall percentage list in sync without a second
    // polling loop. The batch page renders the same number in its summary.
    if (!batchRunning && batchMockRunning && batchActiveIdx >= 0 && batchItems[batchActiveIdx]) {
      batchItems[batchActiveIdx].detail = d;
      updateBatchRow(batchActiveIdx);
      batchSummary();
    }
  });

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

  $("#btn-open-dir").addEventListener("click", () => API.openOutputDir());

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
    if (entry.level === "error") d.style.color = "var(--danger, #c0392b)";
    else if (entry.level === "warn") d.style.color = "#b26a00";
    d.textContent = `[${entry.ts || ""}] ${entry.message || entry}`;
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

  // 批次與單檔共用同一組參數（辨識頁），不再維護第二份開關。
  function currentBatchOpts() {
    return {
      language: $("#sel-lang") ? $("#sel-lang").value : null,
      diarize: $("#sw-diar") ? $("#sw-diar").checked : false,
      nSpeakers: $("#sel-spk") ? $("#sel-spk").value : "auto",
      align: $("#sw-align") ? $("#sw-align").checked : false,
      hint: $("#hint-box") ? $("#hint-box").value.trim() : "",
      trial: $("#sw-trial") ? $("#sw-trial").checked : false,
    };
  }

  batchInput.addEventListener("change", async e => {
    const files = Array.from(e.target.files || []);
    batchInput.value = "";
    if (!files.length) return;
    if (API.mode === "web") {
      const opts = currentBatchOpts();
      for (let i = 0; i < files.length; i++) {
        const sum = $("#batch-summary");
        if (sum) sum.textContent = T("batch.uploading", null, { i: i + 1, n: files.length });
        try { await API.batchAdd(files[i], opts); }
        catch (err) { batchUploadErrors.push(`${files[i].name}：${err.message || err}`); }
      }
      await refreshBatch();
    } else {
      for (const f of files) batchItems.push({
        id: "m" + Math.random().toString(36).slice(2, 9), file: f,
        name: f.name, status: "pending", progress: 0,
      });
      renderBatch();
    }
  });

  /** 從服務端拉全量快照；服務端是唯一事實源。 */
  async function refreshBatch() {
    if (API.mode !== "web") return;
    let snap;
    try { snap = await API.getBatch(); }
    catch { return; }
    batchItems = (snap.items || []).map(it => Object.assign({}, it, { file: null }));
    batchRunning = !!(snap.summary && snap.summary.running);
    renderBatch();
    syncActionButtons();
  }

  API.on("batch", payload => {
    if (API.mode !== "web" || !payload) return;
    if (payload.type === "state" && Array.isArray(payload.items)) {
      batchItems = payload.items.map(it => Object.assign({}, it, { file: null }));
    } else if (payload.item) {
      const idx = batchItems.findIndex(i => i.id === payload.item.id);
      if (idx >= 0) batchItems[idx] = Object.assign({}, batchItems[idx], payload.item);
      else batchItems.push(Object.assign({}, payload.item, { file: null }));
    }
    if (payload.summary) batchRunning = !!payload.summary.running;
    else if (typeof payload.running === "boolean") batchRunning = payload.running;
    renderBatch();
    syncActionButtons();
  });

  function batchSummary() {
    const done = batchItems.filter(i => i.status === "done").length;
    const failed = batchItems.filter(i => i.status === "failed").length;
    const avg = batchItems.length
      ? batchItems.reduce((a, b) => a + (b.progress || 0), 0) / batchItems.length : 0;
    let text = T("batch.progress", null,
      { done, total: batchItems.length, pct: Math.round(avg * 100) });
    if (failed) text += T("batch.failedSuffix", null, { n: failed });
    if (batchRunning) text += T("batch.runningSuffix", " · 執行中");
    const el = $("#batch-summary"); if (el) el.textContent = text;
  }
  function renderBatch() {
    batchSummary();
    const host = $("#batch-list"); if (!host) return;
    host.innerHTML = "";
    if (batchUploadErrors.length) {
      const box = document.createElement("div");
      box.className = "card";
      box.style.cssText = "color:var(--danger, #c0392b);padding:10px;margin-bottom:8px;font-size:12px";
      box.textContent = T("batch.uploadFailed", null, { err: batchUploadErrors.join("；") });
      host.appendChild(box);
    }
    if (!batchItems.length) {
      host.innerHTML += `<div class="card" style="text-align:center;color:var(--muted);padding:28px">
        尚未加入檔案 — 點「加入檔案」可一次選多個音訊，由服務端依序處理。</div>`;
      return;
    }
    batchItems.forEach((it, idx) => host.appendChild(batchRow(it, idx)));
  }
  function batchRow(it, idx) {
    const [label, cls] = stLabel(it.status);
    const row = document.createElement("div");
    row.className = "b-row"; row.dataset.idx = idx;
    const terminal = ["failed", "canceled"].includes(it.status);
    const actTitle = it.status === "done" ? T("batch.openOut", "開啟輸出資料夾")
      : (terminal && API.mode === "web") ? T("batch.retry", "重試")
      : T("batch.remove", "移除");
    row.innerHTML = `
      <div class="nm"><div class="f">${escapeHtml(it.name)}</div>
        ${it.error ? `<div class="e">${escapeHtml(it.error)}</div>` : ""}</div>
      <div class="pbar"><div class="bar"><i style="width:${Math.round((it.progress || 0) * 100)}%"></i></div></div>
      <span class="chip ${cls}">${label}</span>
      <button class="more" title="${actTitle}">⋯</button>`;
    row.querySelector(".more").addEventListener("click", async () => {
      if (it.status === "done") { API.openOutputDir(); return; }
      if (API.mode === "web") {
        if (terminal) { await API.batchRetry(it.id); }
        else if (!batchRunning) { await API.batchRemove(it.id); }
        await refreshBatch();
      } else if (!batchMockRunning) {
        batchItems.splice(idx, 1); renderBatch();
      }
    });
    return row;
  }
  function updateBatchRow(idx) {
    const it = batchItems[idx];
    const row = $(`.b-row[data-idx="${idx}"]`); if (!row) return;
    const [label, cls] = stLabel(it.status);
    row.querySelector(".pbar i").style.width = Math.round((it.progress || 0) * 100) + "%";
    const chip = row.querySelector(".chip"); chip.className = "chip " + cls; chip.textContent = label;
    batchSummary();
  }

  $("#btn-batch-add").addEventListener("click", () => batchInput.click());
  $("#btn-batch-run").addEventListener("click", runBatch);
  $("#btn-batch-clear") && $("#btn-batch-clear").addEventListener("click", async () => {
    if (API.mode === "web") { await API.batchClear(); await refreshBatch(); }
    else { batchItems = batchItems.filter(i => !["done", "failed", "canceled"].includes(i.status)); renderBatch(); }
  });

  async function runBatch() {
    if (API.mode === "web") {
      try {
        if (batchRunning) await API.batchCancel();
        else await API.batchStart();
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
  async function renderHealth() {
    let h;
    try { h = await API.getHealthCheck(); } catch (e) { return; }
    const sum = $("#health-sum");
    if (h.summary) {
      if (h.summary.red > 0) { sum.className = "chip chip-live"; sum.textContent = T("catalog.itemsToFix", null, { n: h.summary.red }); }
      else if (h.summary.yellow > 0) { sum.className = "chip chip-accent"; sum.textContent = T("catalog.coreReady", null, { n: h.summary.yellow }); }
      else { sum.className = "chip chip-ok"; sum.textContent = T("catalog.allReady", "全部就緒"); }
    }
    const host = $("#health-panel"); host.innerHTML = "";
    const dot = s => `<span class="hdot hdot-${s}"></span>`;
    const groups = [...(h.cores || [])];
    if (h.shared && h.shared.length) groups.push({ label: T("catalog.shared", "共用元件"), items: h.shared });
    groups.forEach(g => {
      const card = document.createElement("div"); card.className = "health-core";
      const active = g.backend && g.backend === h.activeBackend;
      card.innerHTML = `<div class="hc-title">${escapeHtml(I18N.L(g.label))}` +
        (active ? ` <span class="chip chip-ok" style="font-size:10px">${T("catalog.inUse", "使用中")}</span>` : ``) + `</div>`;
      (g.items || []).forEach(it => {
        const row = document.createElement("div"); row.className = "hc-item";
        row.innerHTML = `${dot(it.status)}<span class="hi-label">${escapeHtml(I18N.L(it.label))}</span>` +
          `<span class="hi-detail">${escapeHtml(I18N.L(it.detail || ""))}</span>`;
        card.appendChild(row);
      });
      host.appendChild(card);
    });
  }
  $("#btn-health-recheck") && $("#btn-health-recheck").addEventListener("click", renderHealth);

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
    const bp = $("#basic-pane"), ap = $("#advanced-pane"), ax = $("#advanced-extra");
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
        <div class="d" style="margin-top:4px;font-family:var(--font-mono);font-size:11.5px">
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
      // 已就緒且未要求重啟 → 引導前往轉錄
      btn.disabled = false; btn.textContent = T("model.goto", "前往語音轉文字"); btn.dataset.act = "goto"; return;
    }
    if (res && res.restartRequired) {
      // 已載入其他核心、切換需重啟 → 按鈕反白（不可就地載入）
      btn.disabled = true; btn.textContent = T("model.needRestart", "切換核心需重新啟動"); btn.dataset.act = ""; return;
    }
    // 尚未載入 → 可就地下載並載入
    btn.disabled = false; btn.textContent = T("model.load", "下載並載入模型"); btn.dataset.act = "load";
  }
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
          o.textContent = l.value ? (window.I18N ? I18N.L(l.label) : l.label) : T("common.langAuto", "語言 自動");
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
    if ($("#sw-diar")) $("#sw-diar").checked = !!s.diarize;
    if ($("#sel-spk") && s.nSpeakers != null) $("#sel-spk").value = s.nSpeakers;
    if ($("#sw-align")) $("#sw-align").checked = !!s.align;
    if ($("#hint-box") && s.hint != null) $("#hint-box").value = s.hint;
    if ($("#sw-trial")) $("#sw-trial").checked = !!s.trial;
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
  bindPersisted("#sw-diar", "diarize", el => !!el.checked);
  bindPersisted("#sel-spk", "nSpeakers", el => el.value);
  bindPersisted("#sw-align", "align", el => !!el.checked);
  bindPersisted("#sw-trial", "trial", el => !!el.checked);
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
  [["#set-format", "format"], ["#set-vocab", "vocab"], ["#set-theme", "theme"]].forEach(([sel, key]) => {
    $(sel).addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      $$(sel + " button").forEach(x => x.classList.toggle("on", x === b));
      API.setSettings({ [key]: b.dataset.v });
      if (key === "theme") applyTheme(b.dataset.v);
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
