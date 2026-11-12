"""Wave 11 regression: the eight UI / queue / startup fixes.

Runs headless: static contracts on the shipped HTML/CSS/JS plus real
execution of the launcher's singleton-lock cleanup and early-exit retry.
No model weights, no GUI toolkit, no Windows desktop required.

    python tests/test_wave11.py
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _load_launcher():
    """Import app_webview with GUI-only modules stubbed out (Linux CI)."""
    stubs = {
        "proc_guard": types.SimpleNamespace(setup_kill_on_close_job=lambda: None),
        "single_instance": types.SimpleNamespace(SingleInstance=lambda: None),
        "webview_server": types.SimpleNamespace(WebViewServer=object),
    }
    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)
    if "app_paths" not in sys.modules:
        sys.modules["app_paths"] = types.SimpleNamespace(
            app_dir=lambda: ROOT,
            resource_path=lambda *a: ROOT.joinpath(*a),
            resource_root=lambda: ROOT,
            user_path=lambda *a: ROOT.joinpath(*a),
            is_frozen=lambda: False,
        )
    sys.modules.pop("app_webview", None)
    import app_webview
    return app_webview


def main() -> int:
    r = Result()
    html = (ROOT / "webview/index.html").read_text(encoding="utf-8")
    css = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
    app = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    bridge = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
    i18n = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
    server = (ROOT / "webview_server.py").read_text(encoding="utf-8")
    backend = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
    queue = (ROOT / "batch_queue.py").read_text(encoding="utf-8")
    launcher = (ROOT / "app_webview.py").read_text(encoding="utf-8")

    print("\n[1] 進度條上下方去冗餘")
    r.check("HTML 只留 prog-time / prog-eta",
            'id="prog-time"' in html and 'id="prog-eta"' in html and
            'id="prog-groups"' not in html and 'id="prog-phase"' not in html)
    r.check("renderProgressDetail 不再寫 groups / phase",
            "elGroups" not in app and "elPhase" not in app and "PHASE_TEXT" not in app)
    r.check("CSS 有精簡的 .prog-detail", ".prog-detail {" in css)

    print("\n[2] 冷啟動閃退：清 SingletonLock + 秒退自動重試")
    r.check("有 _clear_stale_singleton 且在開窗前呼叫",
            "def _clear_stale_singleton(" in launcher and
            "_clear_stale_singleton(_profile_dir(variant))" in launcher)
    r.check("三個鎖檔名都列在清理清單",
            all(n in launcher for n in ("SingletonLock", "SingletonSocket",
                                        "SingletonCookie")))
    r.check("清鎖前先檢查擁有者 PID 是否還活著",
            "def _lock_owner_alive(" in launcher and
            "_lock_owner_alive(p)" in launcher)
    r.check("有 _run_edge_session 與備用 profile variant",
            "def _run_edge_session(" in launcher and "_EDGE_MAX_ATTEMPTS = 3" in launcher
            and 'f"edge-profile-r{variant}"' in launcher)
    r.check("主流程把 server 前端連線訊號注入 _run_edge_session",
            "_run_edge_session(" in launcher and
            "seen=lambda: srv.window_signal()" in launcher and
            "wait_close=lambda: _wait_for_window_close(srv)" in launcher)
    r.check("server 端有 frontend_seen / SSE 計數 / window_signal",
            all(k in server for k in ("self.frontend_seen = threading.Event()",
                                      "def sse_enter(", "def sse_leave(",
                                      "def window_signal(", "def window_alive(")))
    r.check("不再以 exit 0 + 存活秒數判定秒退",
            "_EDGE_EARLY_EXIT_SEC" not in launcher and
            "early = (rc in (0, None))" not in launcher and
            "存活 < 2s 且 exit 0" not in launcher)

    print("\n[3] 本機路徑直引 + 清理機制")
    r.check("後端有 add_path / ownedPath / 孤兒清掃",
            all(k in queue for k in ("def add_path(", "ownedPath", "_sweep_orphans",
                                     "_drop_owned")))
    r.check("remove / clear_finished 會刪自有副本",
            queue.count("self._drop_owned(it)") >= 2)
    r.check("外部路徑標記為非自有",
            '"ownedPath": False' in queue and '"localPath": True' in queue)
    r.check("HTTP 有 /api/batch/add-path 路由",
            'path == "/api/batch/add-path"' in server)
    r.check("bridge 有 batchAddPath", "async batchAddPath(" in bridge)
    r.check("前端有貼路徑入口",
            'id="btn-batch-path"' in html and 'id="batch-path-input"' in html and
            "API.batchAddPath(line, opts)" in app)

    print("\n[4] 佇列狀態管理（8 → 1 → 0 → 0）")
    r.check("do_POST 統一讀走 keep-alive 殘留 body",
            'self._post_body = self.rfile.read(n)' in server)
    r.check("_read_json_body 改用 _post_body", "getattr(self, \"_post_body\", None)" in server)
    # 只檢查 getBatch 本體：getLogs() 仍可合理回空陣列，別被它誤傷。
    getbatch = bridge.split("async getBatch()", 1)[1].split("},", 1)[0]
    r.check("getBatch 不再降級成空佇列",
            'return await apiGet("/api/batch")' in getbatch and
            "catch" not in getbatch and "items: []" not in getbatch)
    rb = app.split("async function refreshBatch()", 1)[1].split("syncActionButtons();", 1)[0]
    r.check("refreshBatch 失敗時保留既有佇列",
            "catch (err) {" in rb and "return;" in rb and
            "keeping existing queue" in rb and
            "if (!snap || !Array.isArray(snap.items)) return;" in rb and
            "batchItems = []" not in rb and "items: []" not in rb)
    r.check("單筆 SSE 事件會補拉全量",
            "if (!batchItems.length && serverTotal > 1)" in app)
    r.check("cancel 保留 pending、running 退回 pending",
            "it[\"status\"] = \"pending\"" in queue and
            "self._gen += 1" in queue)
    r.check("generation 防止舊 worker 覆蓋新狀態",
            "if self._gen != gen:" in queue and "target=self._run, args=(gen,)" in queue)

    print("\n[5] VAD 試算改為可摺疊面板")
    r.check("HTML 用 <details> 包切分參數",
            '<details class="card seg-panel" id="seg-panel">' in html and
            '<summary class="seg-summary">' in html)
    r.check("CSS 收起/展開樣式齊備",
            ".seg-panel[open] > summary" in css and
            "::-webkit-details-marker" in css and ".seg-panel .seg-body" in css)
    r.check("JS 會更新摺疊摘要", "function syncSegSummary(" in app and
            'T("file.segSummary"' in app and "syncSegSummary();" in app)

    print("\n[6] 試跑醒目提示")
    r.check("開始轉換旁有 trial-badge", 'id="trial-badge"' in html and
            html.index('id="btn-run"') < html.index('id="trial-badge"'))
    r.check("CSS 醒目徽章樣式", ".trial-badge {" in css and ".btn-primary.trial-on" in css)
    r.check("JS 與 #sw-trial 聯動且設定回填後同步",
            "function syncTrialBadge(" in app and
            'addEventListener("change", syncTrialBadge)' in app and
            "syncTrialBadge();" in app)
    r.check("i18n 三語補齊 trialBadge",
            '"file.trialBadge"' in i18n and "TRIAL · first 5 min only" in i18n)

    print("\n[7] 即時輸出純文字逐行 + 時間戳")
    r.check("CSS 無卡片裝飾、單行不折行",
            ".log .ln { white-space: pre;" in css and
            "background: transparent; border: 0;" in css)
    r.check("JS 壓平內嵌換行", '.replace(/\\r\\n|\\r|\\n/g, " · ")' in app)
    r.check("時間戳以淡色前綴顯示",
            's.className = "ts"' in app and 'ts + "  "' in app and
            "[${entry.ts" not in app)
    r.check("等寬逐行樣式仍在", ".log .ln .ts" in css)

    print("\n[8] 批次頁獨立進度")
    r.check("批次頁有自己的 .progress 卡片",
            'class="progress card batch-progress"' in html and
            'id="batch-prog-bar"' in html and 'id="batch-prog-status"' in html)
    r.check("JS 會畫總進度與當前任務",
            "function batchSummary(" in app and "$(\"#batch-prog-bar\")" in app and
            "function refreshBatchActiveText(" in app)
    r.check("每一列有狀態文字與百分比",
            "function batchRowSub(" in app and 'class="pct"' in app)
    r.check("後端標記 scope=batch",
            "def set_progress_scope(" in backend and 'payload["scope"] = scope' in backend
            and '_set_scope("batch")' in queue and "self._set_scope(None)" in queue)
    r.check("前端依 scope 分派、不畫單檔進度條",
            'scope === "batch"' in app and "applyBatchProgress(status)" in app)
    r.check("批次頁進度 i18n 三語齊備",
            all(k in i18n for k in ('"batch.addPath"', '"batch.progRunning"',
                                    '"batch.localPath"', '"batch.progCount"')))

    print("\n[實跑] Edge 清鎖與秒退重試")
    aw = _load_launcher()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        for n in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (p / n).write_text("stale", encoding="utf-8")
        (p / "Preferences").write_text("{}", encoding="utf-8")
        aw._clear_stale_singleton(str(p))
        r.check("三個 Singleton 鎖檔都被清掉",
                not any((p / n).exists() for n in
                        ("SingletonLock", "SingletonSocket", "SingletonCookie")))
        r.check("profile 其他檔案不受影響", (p / "Preferences").is_file())

    class FakeProc:
        def __init__(self, rc):
            self._rc = rc

        def wait(self):
            return self._rc

        def poll(self):
            return self._rc

    variants = []
    orig_open, orig_sleep = aw.open_edge_app, aw.time.sleep
    aw.time.sleep = lambda _s: None
    aw.open_edge_app = lambda url, variant=0: (variants.append(variant), FakeProc(0))[1]
    try:
        rc, alive, attempts = aw._run_edge_session(
            "http://127.0.0.1:1/", seen=lambda: False,
            wait_close=lambda: None, grace=0.0)
    finally:
        aw.open_edge_app, aw.time.sleep = orig_open, orig_sleep
    r.check("完全沒有頁面連線 → 重試 3 次並逐次換備用 profile",
            attempts == 3 and variants == [0, 1, 2],
            f"attempts={attempts} variants={variants}")

    # Wave 12／問題 4：exit 0 / 0.0s 但頁面連上了 → 只開一次，不重複開窗。
    variants2 = []
    aw.open_edge_app = lambda url, variant=0: (variants2.append(variant), FakeProc(0))[1]
    try:
        rc2, alive2, attempts2 = aw._run_edge_session(
            "http://127.0.0.1:1/", seen=lambda: True,
            wait_close=lambda: None, grace=0.0)
    finally:
        aw.open_edge_app, aw.time.sleep = orig_open, orig_sleep
    r.check("頁面已連上 → 只啟動 1 次（不再連開 3 個視窗）",
            attempts2 == 1 and variants2 == [0],
            f"attempts={attempts2} variants={variants2}")

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
