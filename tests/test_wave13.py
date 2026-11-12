"""Wave 13 regression: batch-page layout overhaul + three-dot menu fix.

Two user-reported items after Wave 12:

1. Rework the batch page into the layered layout from the mockup: an overall
   progress card (elapsed / done ratio / total %), a current-file section
   (N/M, per-file %, audio position, segments / file elapsed / speed / file
   ETA), and a queue header (file count / total audio / ETA) above grid-aligned
   queue rows (name / duration / status / used / left / menu).
2. The per-row "..." button did nothing while the queue was running. Root
   cause: the old handler only acted on terminal rows or when NOT running, so
   running / pending rows silently fell through both branches; the backend
   ``BatchQueue.remove()`` also refused running items. Now every row opens an
   actionable drop-down, and removing a running row first cancels the queue.

Headless: no model weights, no GUI, no network. The backend detail payload is
exercised by really calling ``LongformRunner._emit()`` on a lightweight
instance, not by grepping the source alone.

    python tests/test_wave13.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import longform_runner as lf  # noqa: E402

APP = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "webview/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
I18N = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
RUNNER = (ROOT / "longform_runner.py").read_text(encoding="utf-8")

WAVE13_KEYS = [
    "batch.queue", "batch.queueMeta", "batch.currentFile", "batch.noActive",
    "batch.elapsed", "batch.doneRatio", "batch.groups", "batch.fileElapsed",
    "batch.speed", "batch.fileRemaining", "batch.noDetail", "batch.used",
    "batch.left", "batch.etaPending", "batch.menuTitle", "batch.menuOpenOut",
    "batch.menuRetry", "batch.menuSkip", "batch.menuStop", "batch.menuRemove",
    "batch.removeBusy",
]


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def js_block(src: str, signature: str) -> str:
    """Return one 2-space-indented function body, cut at the next sibling decl."""
    start = src.index(signature)
    tail = src[start + len(signature):]
    m = re.search(r"\n  (?://|function |const \w+ = |let \w+ = )", tail)
    return tail[:m.start()] if m else tail


def trilingual(src: str, key: str):
    """Parse ``"key": [hant, hans, en]`` out of i18n.js's ``D`` dictionary."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[(.*?)\]', src, re.S)
    if not m:
        return None
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def test_backend_detail(r: Result):
    print("\n[1] 後端進度明細：實跑 LongformRunner._emit()")
    r.check("來源標記在 detail 分支內（Wave 13）",
            'payload["elapsedSec"]' in RUNNER and 'payload["speedX"]' in RUNNER
            and "Wave 13" in RUNNER)

    runner = lf.LongformRunner.__new__(lf.LongformRunner)  # 跳過重構造，只驗 _emit
    runner.task_id = "wave13"
    runner.progress_cb = lambda pct, status: None
    seen = []
    runner.detail_cb = seen.append
    runner._start_wall = time.monotonic() - 10.0  # 模擬已跑 10 秒

    runner._emit(50, "識別中",
                 {"doneSec": 59.0, "totalSec": 100.0, "groups": 20, "etaSec": 8.0})
    p = seen[-1]
    r.check("原有 detail 欄位完整保留",
            p.get("pct") == 50 and p.get("status") == "識別中"
            and p.get("doneSec") == 59.0 and p.get("totalSec") == 100.0
            and p.get("groups") == 20 and p.get("etaSec") == 8.0,
            str(sorted(p)))
    r.check("附帶實測 elapsedSec（約 10s）",
            isinstance(p.get("elapsedSec"), (int, float)) and p["elapsedSec"] >= 9.5,
            f"elapsedSec={p.get('elapsedSec')}")
    r.check("附帶實測 speedX ≈ done/elapsed ≈ 5.9×",
            isinstance(p.get("speedX"), (int, float)) and 5.5 <= p["speedX"] <= 6.2,
            f"speedX={p.get('speedX')}")

    runner._emit(60, "階段提示")  # 無 detail
    p2 = seen[-1]
    r.check("無 detail 時不塞耗時欄位",
            "elapsedSec" not in p2 and "speedX" not in p2, str(sorted(p2)))

    runner._emit(70, "起跑", {"doneSec": 0, "totalSec": 100.0})
    p3 = seen[-1]
    r.check("doneSec=0 時不算倍速（避免無意義數字）",
            "elapsedSec" in p3 and "speedX" not in p3, str(sorted(p3)))

    eta = runner._eta_sec(59.0, 100.0)
    r.check("_eta_sec 與 speedX 用同一 _start_wall 基準",
            eta is not None and 0 < eta < 20, f"eta={eta}")


def test_layout_dom(r: Result):
    print("\n[2] 分層佈局 DOM / CSS")
    ids = ["batch-progress", "batch-prog-bar", "batch-prog-status", "batch-prog-pct",
           "batch-prog-detail", "batch-prog-count", "batch-prog-active",
           "batch-prog-meta", "batch-cur", "batch-cur-bar", "batch-cur-pct",
           "batch-cur-time", "batch-cur-detail", "batch-queue-meta", "batch-list"]
    missing = [i for i in ids if f'id="{i}"' not in HTML]
    r.check(f"新舊進度 id 齊備（{len(ids)} 個）", not missing, str(missing))

    anchors = ['id="batch-progress"', 'id="batch-cur"',
               'class="batch-queue-head"', 'id="batch-list"']
    pos = [HTML.index(a) for a in anchors]
    r.check("資訊層級順序：總進度 → 目前檔案 → 佇列標題 → 佇列行",
            pos == sorted(pos), str(pos))
    r.check("目前檔案區與佇列標題結構存在",
            'class="cur" id="batch-cur"' in HTML
            and 'class="batch-queue-head"' in HTML
            and 'id="batch-queue-meta"' in HTML)

    r.check("CSS 佇列列為網格 ic/nm/dur/st/used/left/more",
            "grid-template-areas" in CSS and "ic nm dur st used left more" in CSS)
    r.check("CSS 目前檔案區 / 佇列標題 / 高亮列 / 下拉選單",
            all(s in CSS for s in (".batch-progress .cur", ".batch-queue-head",
                                   ".b-row.is-active", ".b-menu", ".b-menu button")))


def test_frontend_dataflow(r: Result):
    print("\n[3] 前端資料流：目前檔案區由後端實測值驅動")
    r.check("renderCurrentFile() 存在並讀 speedX / groups / etaSec",
            "function renderCurrentFile(" in APP
            and "d.speedX" in APP and "d.groups" in APP and "d.etaSec" in APP)
    r.check("耗時走 batchElapsedSec(d)，優先吃後端 elapsedSec",
            "function batchElapsedSec(" in APP
            and "Number(d.elapsedSec)" in APP and "batchElapsedSec(d)" in APP)
    r.check("目前檔案進度條 / 音訊位置 / 明細都有對應 id",
            all(s in APP for s in ('$("#batch-cur-bar")', '$("#batch-cur-pct")',
                                   '$("#batch-cur-time")', '$("#batch-cur-detail")')))
    r.check("batchSummary() 寫總進度 meta 與佇列 meta",
            "function batchSummary(" in APP
            and '$("#batch-prog-meta")' in APP and '$("#batch-queue-meta")' in APP)
    r.check("批次起始計時 trackBatchTiming() 與目前第 N/M 個",
            "function trackBatchTiming(" in APP
            and 'T("batch.currentFile"' in APP
            and "function refreshBatchActiveText(" in APP)
    r.check("佇列統計用 queueMeta（檔數 / 總音訊 / 剩餘）",
            'T("batch.queueMeta"' in APP and "durationSec" in APP)
    r.check("進度明細依 scope=batch 分派且記回 durationSec",
            "function renderBatchDetail(" in APP
            and 'durationSec = Number(d.totalSec)' in APP
            and 'd.scope === "batch"' in APP)
    body = js_block(APP, "function renderBatchDetail(d) {")
    r.check("明細晚到時回寫處理中那一列（已用/剩餘不卡在 —）",
            "updateBatchRow(i)" in body and 'findIndex(x => x.status === "running")' in body,
            "renderBatchDetail 未呼叫 updateBatchRow")
    ubody = js_block(APP, "function updateBatchRow(idx) {")
    r.check("局部更新保留 .st 網格類別（狀態欄不被擠出 grid-area）",
            'className = "st chip " + cls' in ubody)


def test_three_dot_menu(r: Result):
    print("\n[4] 三點選單：執行中也要有可點動作")
    r.check("openBatchMenu / closeBatchMenu 存在",
            "function openBatchMenu(btn, it) {" in APP
            and "function closeBatchMenu()" in APP)
    body = js_block(APP, "function openBatchMenu(btn, it) {")
    r.check(".more 點擊無條件開選單（不再看 batchRunning）",
            'openBatchMenu(row.querySelector(".more"), it);' in APP)
    r.check("任何狀態都至少有「開輸出資料夾 / 移除」兩個動作",
            'acts = [["menuOpenOut"' in body and 'acts.push(["menuRemove"' in body)
    r.check("執行中額外提供「略過此檔 / 停止佇列」",
            'if (running && batchRunning && API.mode === "web")' in body
            and 'T("batch.menuSkip")' in body and 'T("batch.menuStop")' in body)
    r.check("略過此檔走 cancel → remove → start",
            "API.batchCancel()" in body and "API.batchStart()" in body
            and 'T("batch.menuSkip")' in body)
    r.check("移除 running 列先停止佇列再 remove",
            "if (running) await API.batchCancel();" in body
            and "API.batchRemove(it.id)" in body)
    r.check("remove 回 ok=false 時有可見提示",
            'T("batch.removeBusy"' in body and "logLine(" in body)
    r.check("動作三元組 [key,label,fn]：渲染用 label、執行用 found[2]()",
            "acts.map(([k, label])" in body and "escapeHtml(label)" in body
            and "found[2]()" in body)
    r.check("選單定位在視窗內、點外部關閉",
            "getBoundingClientRect()" in body and "closeBatchMenu" in body
            and "window.innerWidth" in body and "window.innerHeight" in body)
    r.check("無舊靜默守衛（done 早退 / !batchRunning 分支）",
            "openOutputDir(); return;" not in body and "!batchRunning" not in body)
    r.check("無動態 T(\"batch.\" + k) 拼接（i18n contract）",
            'T("batch." +' not in APP)


def test_i18n(r: Result):
    print("\n[5] Wave 13 文案三語齊備且都被取用")
    bad = []
    for k in WAVE13_KEYS:
        v = trilingual(I18N, k)
        if not v or len(v) != 3 or any(not s.strip() for s in v):
            bad.append(k)
    r.check(f"{len(WAVE13_KEYS)} 個 key 皆為三語非空白", not bad, str(bad))

    samples = ["batch.queueMeta", "batch.currentFile", "batch.menuSkip", "batch.removeBusy"]
    ok = True
    for k in samples:
        v = trilingual(I18N, k)
        if not v or len(v) != 3 or v[0] == v[2]:
            ok = False
    r.check("抽查英文翻譯非繁中原文（非佔位）", ok)

    unused = [k for k in WAVE13_KEYS
              if f'T("{k}"' not in APP and f'data-i18n="{k}"' not in HTML]
    r.check("每個 key 都被 T() 或 data-i18n 取用", not unused, str(unused))


def main() -> int:
    r = Result()
    test_backend_detail(r)
    test_layout_dom(r)
    test_frontend_dataflow(r)
    test_three_dot_menu(r)
    test_i18n(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
