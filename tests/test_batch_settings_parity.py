"""批次設定一致性 / 雙倍率預估 / 首屏語言注入。

這幾項的共同點是「行為分散在 JS、HTML、Python 三層」，單看任一層都會誤判成
一致：例如前端把 vad/chunk 傳過去了、後端卻仍讀 settings；或後端算出了
5 分鐘倍率，前端卻沒有顯示位置。所以這裡同時查三層的契約。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# webview_backend 會 import app；app 在非 WebView 建置下會 import tkinter 與
# customtkinter。這裡走打包版的同一條路徑（發行版本來就設這個變數），
# 免得測試因為開發機沒裝 tkinter 而跑不起來。
os.environ.setdefault("QWENASR_WEBVIEW_BUILD", "1")

from batch_queue import BatchQueue            # noqa: E402
import longform_runner as lf                  # noqa: E402
import webview_server as ws                   # noqa: E402
from webview_backend import WebBackend        # noqa: E402

APP = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
I18N = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
HTML = (ROOT / "webview/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
SERVER = (ROOT / "webview_server.py").read_text(encoding="utf-8")
BACKEND = (ROOT / "webview_backend.py").read_text(encoding="utf-8")


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


class FakeBackend:
    """只實作 transcribe / cancel 的輕量替身。"""

    def __init__(self):
        self.seen = []
        self.done = threading.Event()

    def transcribe(self, opts, progress_cb=None):
        self.seen.append(dict(opts))
        if progress_cb:
            progress_cb(100, "done")
        self.done.set()
        return {"srtPath": "out.srt", "totalSegments": 3}

    def cancel(self):
        pass


def test_run_opts_locked(r: Result):
    print("\n[1] 佇列『開始』鎖定執行參數")
    be = FakeBackend()
    with tempfile.TemporaryDirectory() as d:
        q = BatchQueue(be, state_dir=Path(d))
        q.items = [{
            "id": "a1", "name": "a.wav", "path": str(Path(d) / "a.wav"),
            "status": "pending", "progress": 0.0, "error": None, "srtPath": None,
            # 入隊時的舊快照：language 故意與開始時不同，用來驗證誰優先
            "opts": {"language": "Chinese", "align": False, "hint": "old"},
            "createdAt": "2026-01-01T00:00:00", "ownedPath": False,
            "localPath": True, "durationSec": 10.0, "etaSec": None,
        }]
        q.start({"language": "English", "align": True, "diarize": True,
                 "nSpeakers": "2", "hint": "new",
                 "vadThreshold": 0.62, "chunkSecs": 12})
        for _ in range(100):
            if be.done.wait(0.1):
                break
        for _ in range(50):
            if not q.running:
                break
            time.sleep(0.05)

        got = be.seen[0] if be.seen else {}
        r.check("開始時傳入的值覆寫入隊快照",
                got.get("language") == "English" and got.get("align") is True,
                str({k: got.get(k) for k in ("language", "align", "hint")}))
        r.check("切分參數一併帶進 transcribe",
                got.get("vadThreshold") == 0.62 and got.get("chunkSecs") == 12,
                str({k: got.get(k) for k in ("vadThreshold", "chunkSecs")}))
        r.check("hint 用開始當下那份，不是入隊時那份",
                got.get("hint") == "new", str(got.get("hint")))
        snap = q.snapshot()
        r.check("snapshot 帶回 runOpts（前端要靠它顯示）",
                isinstance(snap.get("runOpts"), dict)
                and snap["runOpts"].get("chunkSecs") == 12,
                str(snap.get("runOpts")))
        r.check("佇列級耗時由 summary 提供（不再拿單檔 elapsedSec）",
                snap["summary"].get("elapsedSec") is not None
                and snap["summary"]["elapsedSec"] >= 0,
                str(snap["summary"].get("elapsedSec")))

    # 續跑：不傳 opts 時不得把已鎖定的值清掉
    with tempfile.TemporaryDirectory() as d:
        be2 = FakeBackend()
        q2 = BatchQueue(be2, state_dir=Path(d))
        q2.run_opts = {"language": "English", "chunkSecs": 12}
        q2._save()
        q3 = BatchQueue(be2, state_dir=Path(d))     # 重新載入模擬重啟
        r.check("runOpts 持久化，重啟後續跑仍用同一組",
                q3.run_opts.get("language") == "English"
                and q3.run_opts.get("chunkSecs") == 12, str(q3.run_opts))


def test_chunk_override(r: Result):
    print("\n[2] 切分秒數：本次執行可覆寫，且仍夾到模型天花板")

    class Eng:
        max_chunk_secs = 10

    class Stub(WebBackend):
        def __init__(self):                     # 不跑真正的 __init__（會載模型）
            self.engine = Eng()

        def _settings_raw(self):
            return {"chunk_secs": 30}           # 設定裡是 30，模型上限 10

    be = Stub()
    be._apply_chunk_secs(7)
    r.check("顯式指定 7 秒 → 生效", be.engine.max_chunk_secs == 7,
            str(be.engine.max_chunk_secs))
    be._apply_chunk_secs(99)
    r.check("超過模型天花板 → 夾到 10", be.engine.max_chunk_secs == 10,
            str(be.engine.max_chunk_secs))
    be._apply_chunk_secs(None)
    r.check("不傳 → 回到 settings（30 再夾到 10）", be.engine.max_chunk_secs == 10,
            str(be.engine.max_chunk_secs))


def test_rate_window(r: Result):
    print("\n[3] 雙倍率：5 分鐘窗口不足時不給值")
    run = lf.LongformRunner.__new__(lf.LongformRunner)
    run._rate_samples = []
    run._start_wall = time.monotonic()

    t0 = 1000.0
    for k in range(0, 121, 10):                # 0..120s，共 2 分鐘
        run._mark_progress(float(k), t0 + k)
    r.check("只累積 2 分鐘 → 5 分鐘倍率為 None",
            run._speed_5m(120.0, t0 + 120) is None,
            str(run._speed_5m(120.0, t0 + 120)))

    run2 = lf.LongformRunner.__new__(lf.LongformRunner)
    run2._rate_samples = []
    for k in range(0, 601, 10):                # 0..600s，共 10 分鐘
        run2._mark_progress(float(k), t0 + k)
    sp5 = run2._speed_5m(600.0, t0 + 600)
    r.check("累積 10 分鐘 → 近 5 分鐘倍率 ≈ 1.0×（每牆鐘秒處理 1 音訊秒）",
            sp5 is not None and abs(sp5 - 1.0) < 0.05, f"sp5={sp5}")

    # 樣本數不隨片長增長（4 小時也不會爆記憶體）
    run3 = lf.LongformRunner.__new__(lf.LongformRunner)
    run3._rate_samples = []
    for k in range(0, 4 * 3600, 10):
        run3._mark_progress(float(k), t0 + k)
    r.check("4 小時後取樣點仍被裁到常數級",
            len(run3._rate_samples) <= 80, f"n={len(run3._rate_samples)}")

    r.check("ETA 在有窗口資料時以窗口倍率為基準（rateBasis=5m）",
            run2._eta_sec(600.0, 1200.0) is not None
            and getattr(run2, "_last_eta_basis", "") == "5m",
            f"basis={getattr(run2, '_last_eta_basis', '')}")

    # 續跑：doneSec 是檔案內絕對位置（例如從 28s 接著跑），牆鐘只算本輪。
    # 若直接 done/elapsed 會得到 9.9× 這種假倍率，ETA 也會短到不可能。
    run4 = lf.LongformRunner.__new__(lf.LongformRunner)
    run4._rate_samples = []
    run4._rate_base = 28.0
    run4._start_wall = time.monotonic() - 10.0     # 本輪只跑了 10 秒
    eta4 = run4._eta_sec(29.0, 100.0)              # 本輪只新增 1 秒音訊
    r.check("續跑時倍率以本輪新增音訊為基準（不會虛報 2.9×）",
            eta4 is not None and eta4 > 300, f"eta={eta4}")


def test_frontend_contract(r: Result):
    print("\n[4] 前端／後端契約")
    fn = APP.split("function currentBatchOpts()", 1)[1].split("\n  }", 1)[0]
    r.check("currentBatchOpts 帶 vadThreshold / chunkSecs",
            "vadThreshold" in fn and "chunkSecs" in fn, fn.strip()[:60])
    r.check("開始時把設定交給後端（batchStart(opts)）",
            "API.batchStart(currentBatchOpts())" in APP
            and "async batchStart(opts)" in BRIDGE)
    r.check("後端 /api/batch/start 解析 opts 並轉給 queue.start",
            'server.batch_queue.start(opts)' in SERVER)

    r.check("transcribe 支援 opts 覆寫切分參數",
            'opts.get("chunkSecs")' in BACKEND
            and 'opts.get("vadThreshold")' in BACKEND)

    r.check("批量頁有設定卡與格子",
            'id="batch-cfg"' in HTML and 'id="batch-cfg-grid"' in HTML)
    r.check("設定卡由 renderBatchConfig 產生且值沒變就不重建",
            "function renderBatchConfig(" in APP and "_batchCfgSig" in APP)
    r.check("佇列級與檔案級各有一個層級標籤",
            'id="batch-prog-eta"' in APP or 'id="batch-prog-eta"' in HTML)
    r.check("佇列級剩餘時間由 queueEtaSec 計算（不吃單檔 eta）",
            "function queueEtaSec(" in APP)

    keys = ["batch.cfgTitle", "batch.cfgLang", "batch.cfgDiar", "batch.cfgAlign",
            "batch.cfgHint", "batch.cfgVad", "batch.cfgChunk", "batch.cfgTrial",
            "batch.queueEta", "batch.rateLong", "batch.rate5m",
            "batch.lvQueue", "batch.lvFile"]
    missing = [k for k in keys if f'"{k}":' not in I18N]
    r.check(f"新增文案 {len(keys)} 個都在字典裡", not missing, str(missing))


def test_locale_injection(r: Result):
    print("\n[5] 首屏語言")
    class SS:
        class backend:
            @staticmethod
            def _settings_raw():
                return {"ui_lang": "简体中文"}
    raw = b'<html lang="zh-Hant" data-locale="hant" translate="no">'
    out = ws._inject_locale(raw, SS).decode("utf-8")
    r.check("简体中文 → data-locale=hans、lang=zh-Hans",
            'data-locale="hans"' in out and 'lang="zh-Hans"' in out, out)

    class SE:
        class backend:
            @staticmethod
            def _settings_raw():
                return {"ui_lang": "English"}
    out2 = ws._inject_locale(raw, SE).decode("utf-8")
    r.check("English → lang=en", 'lang="en"' in out2 and 'data-locale="en"' in out2, out2)

    class SH:
        class backend:
            @staticmethod
            def _settings_raw():
                return {"ui_lang": "繁體中文"}
    r.check("繁中維持原樣（HTML 預設）",
            ws._inject_locale(raw, SH) == raw)

    r.check("i18n 在 head 同步載入（不等到 body 尾端）",
            HTML.index('<script src="js/i18n.js"></script>') < HTML.index("<body"))
    r.check("有 data-i18n-ready 閘門，腳本未跑完不會先畫出繁中",
            "html:not([data-i18n-ready]) body" in CSS)
    r.check("壓掉瀏覽器翻譯提示",
            'translate="no"' in HTML and 'name="google" content="notranslate"' in HTML)
    r.check("bootstrap 在 DOM 就緒後套用 data-locale",
            'getAttribute("data-locale")' in I18N and "bootstrapLang" in I18N)


def main() -> int:
    r = Result()
    test_run_opts_locked(r)
    test_chunk_override(r)
    test_rate_window(r)
    test_frontend_contract(r)
    test_locale_injection(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
