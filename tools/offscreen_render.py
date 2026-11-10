"""Offscreen Web UI verification with headless Chromium.

What this verifies
------------------
* The real ``webview/index.html`` + CSS + JS loads and renders in Chromium.
* The Wave 4 SSE handlers (``progress-detail``, ``segments``) actually paint
  the dual progress view and the bounded live-subtitle list.
* View switching, i18n language switching, batch rows and settings restore do
  not throw, and the DOM contains the expected values afterwards.

What this does NOT verify
-------------------------
* Windows window creation (Edge --app), DWM title-bar colouring, pythonnet
  removal at runtime, or EXE startup. Those need a Windows desktop.
* ASR accuracy, OpenVINO weights, or real audio inference.

The page is served over HTTP without a /health endpoint, so bridge.js settles
on its built-in mock transport. ``QwenAPI._emit`` is then used to inject the
same SSE payloads the backend sends. No model download, no network.

Usage:
    python tools/offscreen_render.py
    python tools/offscreen_render.py --out /path/to/dir --headed
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "webview"
OUT = Path("/data/workspace/offscreen")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def start_server():
    handler = functools.partial(QuietHandler, directory=str(WEB))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def make_dummy_files(out: Path) -> list[str]:
    """Small valid WAVs so the batch file input has realistic file names."""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, name in enumerate(["第01讲-导论.wav", "第02讲-方法.wav", "第03讲-习题.wav"]):
        p = out / name
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        paths.append(str(p))
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"url": "", "console_errors": [], "expected_console_errors": [],
              "page_errors": [], "failed_requests": [], "checks": [],
              "screenshots": []}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is required: pip install playwright", file=sys.stderr)
        return 2

    srv, base = start_server()
    report["url"] = base
    launch = {"headless": not args.headed}
    # Prefer the system browser: the Playwright-managed bundle may not exist.
    for exe in ("/usr/bin/google-chrome", "/usr/local/bin/chromium",
                "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if Path(exe).exists():
            launch["executable_path"] = exe
            break
    launch["args"] = ["--no-sandbox", "--disable-dev-shm-usage",
                      "--force-color-profile=srgb"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  device_scale_factor=1, locale="zh-TW")
        page = ctx.new_page()

        # External webfonts are not available offline; abort them so the page
        # settles immediately and falls back to the installed CJK fonts.
        page.route("https://fonts.googleapis.com/**", lambda r: r.abort())
        page.route("https://fonts.gstatic.com/**", lambda r: r.abort())
        # Two console errors are expected in this offline harness and must not
        # be confused with UI bugs: the aborted Google Fonts stylesheet, and
        # the deliberate 404 on /health that makes bridge.js fall back to its
        # built-in mock transport.
        EXPECTED = ("fonts.googleapis.com", "fonts.gstatic.com", "/health")
        def on_console(m):
            if m.type != "error":
                return
            url = (m.location or {}).get("url", "")
            if any(k in url for k in EXPECTED):
                report["expected_console_errors"].append({"url": url, "text": m.text})
            else:
                report["console_errors"].append({"url": url, "text": m.text})
        page.on("console", on_console)
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        page.on("requestfailed", lambda r: report["failed_requests"].append(
            {"url": r.url, "failure": r.failure}))

        page.goto(base, wait_until="load", timeout=30000)
        page.wait_for_function("window.QwenAPI && window.QwenAPI.mode", timeout=15000)
        # Let init() finish: refreshStatus -> loadLanguages -> loadSettings -> switchView.
        page.wait_for_timeout(1200)

        mode = page.evaluate("window.QwenAPI.mode")
        report["mode"] = mode

        def check(name, ok, detail=""):
            report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
            print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

        def shot(name):
            p = out / name
            page.screenshot(path=str(p), full_page=False)
            report["screenshots"].append(str(p))
            print("  screenshot:", p)

        check("bridge settled on mock transport (no backend)", mode == "mock", mode)
        check("no uncaught page errors after init", not report["page_errors"],
              "; ".join(report["page_errors"][:3]))

        # Initial view: no model ready -> model page.
        shot("01_初始_模型页.png")

        # Switch to the transcription view via the real nav button.
        page.click('.nav-item[data-view="file"]')
        page.wait_for_timeout(350)
        check("file view visible", page.locator("#view-file").is_visible()
              if page.locator("#view-file").count() else
              page.locator("#progress").count() > 0)

        # Inject the exact Wave 4 SSE payloads the backend emits.
        page.evaluate("""() => {
          const api = window.QwenAPI;
          api._emit('progress', {pct: 42, status: '已处理 84.0 分钟 · 612 段'});
          api._emit('progress-detail', {
            pct: 42, status: '已处理 84.0 分钟 · 612 段', taskId: 'offscreen01',
            doneSec: 5040, totalSec: 12000, groups: 612, etaSec: 6960, phase: 'running'
          });
          // Deliberately omit the numeric fields first: a partial payload must
          // not paint "undefineds" / "NaN GB" into the settings hint.
          api._emit('capabilities', {streaming: true, align: false, diarize: false,
                                     trial: false});
          // Models absent -> switches are greyed out with an explanation.
          api._emit('capabilities', {streaming: true, alignAvailable: false,
                                     diarAvailable: false, alignActive: false,
                                     diarActive: false});
          // Models present -> switches become usable again. This replaces the
          // old assertion that they were permanently disabled in streaming mode.
          api._emit('capabilities', {streaming: true, alignAvailable: true,
                                     diarAvailable: true, alignActive: true,
                                     diarActive: true, effectiveChunkSecs: 10,
                                     modelCeilingSecs: 10, estimatedRamMB: 6144,
                                     trial: false});
          const segs = [];
          for (let i = 1; i <= 24; i++) {
            segs.push({index: i, start: (i-1)*12.5, end: i*12.5,
                       text: '第 ' + i + ' 段：这里验证实时字幕增量渲染与 DOM 上限，'
                             + '长音频不会再把整个文件解码进浏览器内存。'});
          }
          api._emit('segments', {taskId: 'offscreen01', count: segs.length,
                                 segments: segs});
        }""")
        page.wait_for_timeout(500)
        shot("02_识别页_双进度与实时字幕.png")
        page.locator("#live-panel").scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        shot("02b_实时字幕面板.png")

        t = page.locator("#prog-time").inner_text()
        check("time/segment progress rendered",
              "1:24:00" in t and "3:20:00" in t, t)
        check("ETA rendered", "剩餘" in page.locator("#prog-eta").inner_text(),
              page.locator("#prog-eta").inner_text())
        check("group count rendered", "612" in page.locator("#prog-groups").inner_text(),
              page.locator("#prog-groups").inner_text())
        check("percentage bar retained at 42%", "42%" in page.locator("#prog-pct").inner_text(),
              page.locator("#prog-pct").inner_text())
        live_rows = page.locator("#live-list > div").count()
        check("live subtitles rendered", live_rows == 24, f"rows={live_rows}")
        check("live panel is visible", page.locator("#live-panel").is_visible())
        # The renderer-OOM guard: 250 injected lines must leave exactly 200 DOM
        # nodes, not 250 (and certainly not one node per line of a 5h lecture).
        page.evaluate("""() => {
          const api = window.QwenAPI, segs = [];
          for (let i = 25; i <= 250; i++) {
            segs.push({index: i, start: (i-1)*12.5, end: i*12.5,
                       text: 'DOM 上限验证第 ' + i + ' 条。'});
          }
          for (let k = 0; k < segs.length; k += 8) {
            api._emit('segments', {taskId: 'offscreen01',
                                   count: Math.min(8, segs.length - k),
                                   segments: segs.slice(k, k + 8)});
          }
        }""")
        page.wait_for_timeout(350)
        capped = page.locator("#live-list > div").count()
        check("live subtitle DOM capped at 200", capped == 200, f"rows={capped}")
        check("enrichment switches re-enabled when models are available",
              not page.locator("#sw-align").is_disabled() and
              not page.locator("#sw-diar").is_disabled())
        page.evaluate("""() => window.QwenAPI._emit('capabilities',
          {streaming: true, alignAvailable: false, diarAvailable: false})""")
        check("absent enrichment models grey the switches",
              page.locator("#sw-align").is_disabled() and
              page.locator("#sw-diar").is_disabled())
        page.evaluate("""() => window.QwenAPI._emit('capabilities',
          {streaming: true, alignAvailable: true, diarAvailable: true,
           effectiveChunkSecs: 10, modelCeilingSecs: 10, estimatedRamMB: 6144})""")
        eff = page.locator("#seg-effective").inner_text()
        ram = page.locator("#seg-ram").inner_text()
        check("capabilities shows effective chunk / ceiling",
              "10s" in eff and "10s" in eff.split("（")[1], eff)
        check("capabilities shows finite RAM estimate",
              "6.0 GB" in ram and "NaN" not in ram, ram)
        check("partial payload never paints undefined/NaN",
              "undefined" not in eff and "NaN" not in eff and
              "undefined" not in ram and "NaN" not in ram)
        note = page.locator("#cap-note").inner_text()
        check("streaming note is i18n-driven and describes optional enhancement",
              "可選增強" in note and "失敗不影響字幕" in note and
              "此模式暫不提供" not in note, note)
        check("preview button and trial switch share one row",
              abs(page.locator("#btn-vad-preview").bounding_box()["y"] -
                  page.locator("#seg-panel .seg-actions .switch").bounding_box()["y"]) < 12,
              str((page.locator("#btn-vad-preview").bounding_box()["y"],
                   page.locator("#seg-panel .seg-actions .switch").bounding_box()["y"])))
        check("segmentation panel copy is short",
              len(page.locator("#seg-panel").inner_text()) < 220,
              str(len(page.locator("#seg-panel").inner_text())))

        # Batch view: upload three files through the real file input.
        page.click('.nav-item[data-view="batch"]')
        page.wait_for_timeout(300)
        dummy = make_dummy_files(out / "_dummy")
        page.locator('input[type="file"][multiple]').set_input_files(dummy)
        page.wait_for_timeout(400)
        shot("03_批量页_统一参数与总进度.png")
        check("three batch rows rendered", page.locator(".b-row").count() == 3,
              str(page.locator(".b-row").count()))
        check("batch summary shows overall percentage",
              "總進度" in page.locator("#batch-summary").inner_text(),
              page.locator("#batch-summary").inner_text())
        check("duplicate batch switches removed",
              page.locator("#sw-batch-diar").count() == 0 and
              page.locator("#sel-batch-spk").count() == 0)
        check("shared-options hint has i18n text",
              page.locator('[data-i18n="batch.sharedOpts"]').inner_text().strip() != "")

        # Settings view, then switch UI language to Simplified Chinese and English.
        page.click('.nav-item[data-view="settings"]')
        page.wait_for_timeout(300)
        shot("04_设置页_繁体.png")
        page.select_option("#set-lang", "简体中文")
        page.wait_for_timeout(350)
        shot("05_设置页_简体.png")
        check("i18n switched to simplified",
              "设置" in page.locator('.nav-item[data-view="settings"]').inner_text()
              or "設定" not in page.locator('.nav-item[data-view="settings"]').inner_text())
        page.select_option("#set-lang", "English")
        page.wait_for_timeout(350)
        shot("06_设置页_English.png")
        check("i18n switched to English",
              "Settings" in page.locator('.nav-item[data-view="settings"]').inner_text())
        check("streaming note follows English",
              "Failures never affect subtitles" in page.locator("#cap-note").inner_text(),
              page.locator("#cap-note").inner_text())

        # Model page: download center should render from the catalog endpoint.
        page.click('.nav-item[data-view="model"]')
        page.wait_for_timeout(400)
        shot("07_模型页_下载中心.png")
        # Mock transport has no /api/model-catalog, so inject a representative
        # catalog payload (including the deliberately null size/sha fields) and
        # click the real refresh button to verify Wave 2's rendering path.
        page.evaluate("""() => {
          window.QwenAPI.getModelCatalog = async () => ({
            mirror: 'https://hf-mirror.com', model_root: 'C:\\QwenASR\\ov_models',
            total_bytes_on_disk: 1258291200, complete: ['qwen_0p6b'],
            incomplete: ['qwen_1p7b'],
            bundles: [
              {id: 'qwen_0p6b', label: 'Qwen3-ASR 0.6B INT8（CPU）', complete: true,
               total_count: 3, present_count: 3, size_hint: '约 1.2 GB',
               target_dir: 'ov_models/qwen3_asr_int8', provider: 'HuggingFace',
               optional: false, files: [
                 {name: 'audio_encoder.xml', present: true, size_bytes: 1048576,
                  sha256_expected: 'a'*64, sha256_ok: true},
                 {name: 'decoder_model.bin', present: true, size_bytes: 1073741824,
                  sha256_expected: 'b'*64, sha256_ok: true},
               ]},
              {id: 'qwen_1p7b', label: 'Qwen3-ASR 1.7B INT8（CPU，較準）', complete: false,
               total_count: 15, present_count: 0, size_hint: '约 4.4 GB',
               target_dir: 'ov_models/qwen3_asr_1p7b_kv_int8', provider: 'HuggingFace',
               optional: true, files: [
                 {name: 'decoder_kv_model.bin', present: false, problem: 'missing',
                  size_bytes: null, size_hint: null, sha256_expected: null},
               ]},
            ]
          });
        }""")
        page.click("#btn-catalog-refresh")
        page.wait_for_timeout(300)
        page.locator("#catalog-list").scroll_into_view_if_needed()
        page.wait_for_timeout(250)
        shot("07b_模型页_下载中心_下半.png")
        check("download center renders both bundles",
              page.locator("#catalog-list details").count() == 2,
              str(page.locator("#catalog-list details").count()))
        cat = page.locator("#catalog-summary").inner_text()
        check("download center summary includes mirror and totals",
              "hf-mirror.com" in cat and "1/2" in cat, cat)
        page.evaluate("document.querySelectorAll('#catalog-list details').forEach(d => d.open = true)")
        cat_list = page.locator("#catalog-list").inner_text()
        check("unknown size/sha are labelled, not faked",
              "大小未知" in cat_list and "SHA256 未知" in cat_list,
              cat_list[-180:].replace("\n", " | "))
        check("size_hint is not double-wrapped with 約/约",
              "約 約" not in cat_list and "約 约" not in cat_list and
              "（估計）（估計）" not in cat_list,
              cat_list[:160].replace("\n", " | "))

        # Font/network failures from aborted webfonts are expected and filtered.
        real_failed = [r for r in report["failed_requests"]
                       if "fonts.g" not in r["url"]]
        check("no unexpected failed requests", not real_failed, str(real_failed[:3]))
        check("no unexpected console errors", not report["console_errors"],
              "; ".join(str(x) for x in report["console_errors"][:4]))
        check("expected offline/health errors are isolated",
              len(report["expected_console_errors"]) >= 1,
              str(len(report["expected_console_errors"])))
        check("no uncaught page errors overall", not report["page_errors"],
              "; ".join(report["page_errors"][:3]))

        ctx.close()
        browser.close()

    srv.shutdown()
    srv.server_close()

    ok = all(c["ok"] for c in report["checks"])
    report["passed"] = sum(c["ok"] for c in report["checks"])
    report["total"] = len(report["checks"])
    report["ok"] = ok
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"\n结果：{report['passed']}/{report['total']} 项通过")
    print("报告：", out / "report.json")
    print("✅ 离屏渲染无报错" if ok else "❌ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
