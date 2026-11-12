"""Wave 14 回归：批量页重构 / 断点续传 / 时长探测 / 下载中心与自检统一 / 内存释放。

覆盖用户本轮提出的 18 项需求中「可以用代码钉死」的部分。凡是需要真机
（Windows 窗口、Vulkan 驱动）的部分只做静态契约断言，行为验证交给
tools/offscreen_render.py 与 Win runner。

    python tests/test_wave14.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "webview/index.html").read_text(encoding="utf-8")
APP = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
CSS = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
I18N = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
SERVER = (ROOT / "webview_server.py").read_text(encoding="utf-8")
BACKEND = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
RUNNER = (ROOT / "longform_runner.py").read_text(encoding="utf-8")
QUEUE_SRC = (ROOT / "batch_queue.py").read_text(encoding="utf-8")
LAUNCHER = (ROOT / "app_webview.py").read_text(encoding="utf-8")
UI_TEXT = (ROOT / "ui_text.py").read_text(encoding="utf-8")


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


# ──────────────────────────────────────────────────────────────────────
# 1~2、6、7：批量页布局与「只用本机路径」
# ──────────────────────────────────────────────────────────────────────
def test_batch_page_layout(r: Result):
    print("\n[1] 批量页：拖放框独占整行、按钮下移、无「加入文件」按钮")
    head = HTML.split('class="batch-head"', 1)[1].split("</div>\n\n        <!-- Wave 11", 1)[0]
    drop_at = head.find('id="batch-drop"')
    tools_at = head.find('class="batch-tools"')
    r.check("拖放框在按钮列之前（独占一行）", 0 < drop_at < tools_at)
    r.check("「加入檔案」按钮已移除", 'id="btn-batch-add"' not in HTML)
    r.check("batch-tools 有独立样式", ".batch-tools" in CSS)

    print("\n[2] 点击拖放框即叫出选档（含预览模式）")
    r.check("点击绑定 pickBatchFiles", 'batchDrop.addEventListener("click", () => pickBatchFiles())' in APP)
    r.check("键盘 Enter/Space 同样可用", 'e.key === "Enter" || e.key === " "' in APP)
    r.check("预览模式退化为浏览器选档而非无反应",
            'if (API.mode !== "web") { batchInput.click(); return Promise.resolve(); }' in APP)

    print("\n[3] 拖放只用本机路径：上传路由彻底停用")
    r.check("后端 /api/batch/add 返回 410", 'return self._err(410' in SERVER)
    r.check("后端不再有 _batch_add 实现", "def _batch_add" not in SERVER)
    r.check("bridge 不再有上传版 batchAdd", "async batchAdd(" not in BRIDGE)
    r.check("前端不再调用 API.batchAdd(",
            not re.search(r"API\.batchAdd\(", APP))
    r.check("拿不到绝对路径时给出明确引导",
            'T("batch.dropNoPath"' in APP and 'T("batch.pickUnsupported")' in APP)
    # Wave 15：提示要带上「收到几个文件」，否则用户无法判断拖放有没有生效。
    r.check("拖放提示带上文件数",
            re.search(r'T\("batch\.dropNoPath",\s*null,\s*\{\s*n:', APP) is not None)
    r.check("引导文案三语齐备",
            all(k in I18N for k in ("batch.dropNoPath", "batch.pickUnsupported")))


# ──────────────────────────────────────────────────────────────────────
# 4：批量页不继承「试跑前 5 分钟」
# ──────────────────────────────────────────────────────────────────────
def test_batch_no_trial(r: Result):
    print("\n[4] 批量任务不带 trial 字段")
    fn = APP.split("function currentBatchOpts()", 1)[1].split("\n  }", 1)[0]
    r.check("currentBatchOpts 不含 trial", "trial" not in fn)
    r.check("后端 add_path 白名单不含 trial",
            '"language", "diarize", "nSpeakers", "align", "hint"' in QUEUE_SRC)


# ──────────────────────────────────────────────────────────────────────
# 5、9：多语言
# ──────────────────────────────────────────────────────────────────────
def test_i18n(r: Result):
    print("\n[5] 运行期生成的文案不再写死繁中")
    r.check("空队列提示走 T()", 'box.textContent = T("batch.empty")' in APP)
    r.check("batch.empty 三语齐备", '"batch.empty"' in I18N)

    print("\n[6] 识别语言下拉不再显示 [object Object]")
    r.check("结构化 label 保留对象引用", "o._src = auto ? l.label : null" in APP)
    r.check("语言专名不被汉化/机翻", 'o._literal = auto ? "" : String(l.label || "")' in APP)
    r.check("切语言时按 _literal 还原",
            "if (o._literal) { o.textContent = o._literal; return; }" in APP)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "probe.js"
        out.write_text(
            # setLang() 会写 document.documentElement.lang，node 里给个空壳。
            "global.window=global;global.document={documentElement:{},"
            "querySelectorAll:()=>[],querySelector:()=>null};"
            "require('%s');" % (ROOT / "webview/js/i18n.js").as_posix() +
            "const I=window.I18N;"
            "const label={k:'msg.lang.auto',v:{},t:'自動偵測'};"
            "console.log(JSON.stringify(['hant','hans','en'].map(l=>{I.setLang(l);return I.L(label);})));",
            encoding="utf-8")
        import subprocess
        res = subprocess.run(["node", str(out)], capture_output=True, text=True, timeout=60)
        try:
            got = json.loads(res.stdout.strip().splitlines()[-1])
        except Exception:
            got = []
        r.check("自動偵測 三语各自本地化",
                got == ["自動偵測", "自动检测", "Auto detect"], str(got))


# ──────────────────────────────────────────────────────────────────────
# 3、8：断点续传与时长探测（真实调用后端对象）
# ──────────────────────────────────────────────────────────────────────
def test_resume_and_duration(r: Result):
    print("\n[7] 断点续传保留「已处理段数」")
    r.check("runner 回传 resumeFrom", '"resumeFrom": resume_from' in RUNNER)
    r.check("runner 起点事件带 segments",
            '"segments": max(0, next_index - 1)' in RUNNER)
    r.check("后端把 resumedFrom 透传", '"resumedFrom": int(result.get("resumeFrom") or 1)' in BACKEND)
    r.check("队列开始任务时不清零 progress",
            'nxt["error"] = None\n                    nxt["resumedFrom"]' in QUEUE_SRC)
    r.check("队列完成后回写总段数与起点",
            'cur["totalSegments"] = result.get("totalSegments")' in QUEUE_SRC and
            'cur["resumedFrom"] = int(result.get("resumedFrom") or 1)' in QUEUE_SRC)
    r.check("前端明细优先显示累计段数", "const segDone = Number(d && d.segments);" in APP)

    print("\n[8] 入队后异步探测时长")
    r.check("队列有后台探测线程", "def _probe_duration_async" in QUEUE_SRC)
    r.check("加入后触发探测", QUEUE_SRC.count("self._probe_duration_async(snapshot[\"id\"])") == 2)
    r.check("探测结果走 SSE duration 事件",
            'self._emit(cur, "duration")' in QUEUE_SRC and
            'payload.type === "duration"' in APP)

    # 真跑一次：用临时目录 + 假 backend，验证 durationSec 被写回。
    import app_paths
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        orig = app_paths.user_path
        app_paths.user_path = lambda *p: tmp.joinpath(*p)
        try:
            import batch_queue as bq
            wav = tmp / "a.wav"
            import wave as _w
            with _w.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000)      # 1.0 秒
            q = bq.BatchQueue(backend=SimpleNamespace(), state_dir=tmp / "state")
            item = q.add_path(str(wav), None, {})
            r.check("add_path 立即可见", item["status"] == "pending")
            deadline = time.time() + 8
            dur = None
            while time.time() < deadline:
                dur = next((i.get("durationSec") for i in q.items), None)
                if dur:
                    break
                time.sleep(0.05)
            r.check("背景探测写入 durationSec≈1.0s",
                    dur is not None and abs(float(dur) - 1.0) < 0.2, str(dur))
        finally:
            app_paths.user_path = orig


# ──────────────────────────────────────────────────────────────────────
# 11~13：下载中心与系统自检
# ──────────────────────────────────────────────────────────────────────
def test_download_center(r: Result):
    print("\n[9] 下载中心复用系统自检结论")
    r.check("后端附加 runtimeReady", "b[\"runtimeReady\"]" in BACKEND)
    r.check("crispasr 核心只认已装的那个版本",
            'if bid == "crispasr_core"' in BACKEND and
            "quick_check_crispasr(self._crispasr_dir()" in BACKEND)
    r.check("ffmpeg 只认可执行文件", '_ff = self._resolve_ffmpeg()' in BACKEND)
    r.check("前端以 runtimeReady 为准",
            "const rt = b.runtimeReady;" in APP and "rt === true" in APP)
    r.check("重算 complete/incomplete 汇总",
            'st["complete"] = [x["id"]' in BACKEND)

    print("\n[10] 明細可折疊 + 跳轉自檢")
    r.check("bundle 仍是 <details> 可折叠", "<details" in APP and "open" in APP)
    r.check("有「查看系統自檢」按鈕", "data-goto-health" in APP and 'T("dl.gotoSelfCheck")' in APP)
    r.check("跳转按钮滚动到自检面板", 'panel.scrollIntoView' in APP)

    print("\n[11] 手动下载文件夹用途说明")
    r.check("下载中心有用途说明", 'data-i18n="dl.importDirHint"' in HTML)
    r.check("说明文案三语齐备", '"dl.importDirHint"' in I18N)

    print("\n[12] 公共组件撑满右侧空白")
    r.check("自檢面板改用 auto-fit", "repeat(auto-fit, minmax(320px, 1fr))" in CSS)
    r.check("最后一组跨整行", ".health-core:last-child:nth-child(odd)" in CSS)


# ──────────────────────────────────────────────────────────────────────
# 14、15：设置页布局与误报
# ──────────────────────────────────────────────────────────────────────
def test_model_page(r: Result):
    print("\n[13] 千问 / Whisper 并排一行")
    r.check("core-cards 横向排列", "#core-cards { flex-direction: row" in CSS)
    r.check("窄屏可换行", "#core-cards .radio-card { flex: 1 1 240px" in CSS)

    print("\n[14] 设置页顺序：设备 → 选模型 → 加速版本 → 诊断")
    body = HTML.split('data-view="model"', 1)[1].split("<!-- ── Download center", 1)[0]
    i_dev = body.find('id="dev-list"')
    i_pick = body.find('id="model-mode"')
    i_accel = body.find("accel-cards")
    i_diag = body.find('id="gpu-diag"')
    i_health = body.find('id="health-fold"')
    r.check("顺序正确", 0 < i_dev < i_pick < i_accel < i_diag < i_health,
            f"{i_dev},{i_pick},{i_accel},{i_diag},{i_health}")
    r.check("自检并入诊断段落（可折叠）", 'class="diag-fold" id="health-fold"' in HTML)
    # Wave 15 需求变更：自检改为**默认展开**（信息应直接可见），并且不再依红/黄灯
    # 数量强制 open —— 否则用户刚手动收起来，点「重新检查」又会被弹开。
    r.check("自检默认展开", re.search(r'id="health-fold"\s+open', HTML) is not None)
    r.check("不再按红绿灯强制展开",
            "h.summary.red > 0) fold.open = true" not in APP)

    print("\n[15] 修正「Qwen 1.7B + CPU 却报核心未下载/版本不符」")
    r.check("OpenVINO 路径下核心标为「不需要」", "health.crisp.coreNotNeeded" in BACKEND)
    r.check("版本相符仅变体不同时单独说明", "health.crisp.coreVariantMismatch" in BACKEND)
    r.check("两句文案后端/前端都注册",
            all(k in UI_TEXT for k in ("health.crisp.coreNotNeeded",
                                       "health.crisp.coreVariantMismatch")) and
            all(k in I18N for k in ("health.crisp.coreNotNeeded",
                                    "health.crisp.coreVariantMismatch")))


# ──────────────────────────────────────────────────────────────────────
# 16~18：二次启动与内存释放
# ──────────────────────────────────────────────────────────────────────
def test_memory_and_launch(r: Result):
    print("\n[16] 二次启动回到已开窗口")
    r.check("有转交既有窗口的实现", "def _focus_existing_window(" in LAUNCHER)
    r.check("优先转交，失败才退回默认浏览器",
            "if not _focus_existing_window(existing_url):" in LAUNCHER)
    r.check("复用同一 user-data-dir 以命中既有实例",
            "f\"--user-data-dir={_profile_dir(0)}\"" in LAUNCHER)

    print("\n[17] 取消队列后释放内存（延迟到真正空闲）")
    r.check("有 pending 标志", "self._release_pending = False" in BACKEND)
    r.check("取消时登记释放", 'self._release_pending = True' in BACKEND)
    r.check("空闲时立即释放", "def release_on_idle(" in BACKEND)
    r.check("队列 cancel 主动触发释放",
            'releaser("batch-canceled")' in QUEUE_SRC)
    r.check("新一轮开始清除标志", "self._release_pending = False\n            self._running += 1" in BACKEND)

    # 行为验证：假 backend 上模拟「运行中取消 → 结束 → 已释放」。
    import webview_backend as wb
    import threading as _th
    b = object.__new__(wb.WebBackend)
    b.engine = SimpleNamespace(ready=True, audio_enc=object(), vad_sess=object(),
                               _last_segments_rich=None, _last_vad_diag=None)
    b._loaded = True
    b._loading = False
    b._running = 1                     # 假装 worker 还在 transcribe() 里
    b._batch_running = True
    b._release_pending = False
    b._released_once = False
    b._lock = _th.Lock()
    b._settings_raw = lambda: {}
    b._active_backend = "openvino"
    released = []
    b.release_model = lambda reason="manual": (released.append(reason),
                                               {"ok": True, "released": True})[1]
    res = b.release_model_if_enabled("batch-canceled")
    r.check("取消瞬间不硬放（等 worker 退出）",
            res.get("reason") == "deferred" and not released, str(res))
    b._end_run()                       # worker 真正结束
    r.check("worker 结束后立刻释放", released == ["batch-canceled"], str(released))

    print("\n[18] 「不在识别中」就释放，但整队进行中不逐档卸载")
    b2 = object.__new__(wb.WebBackend)
    b2.engine = SimpleNamespace(ready=True, audio_enc=object(), vad_sess=object(),
                                _last_segments_rich=None, _last_vad_diag=None)
    b2._loaded = True
    b2._loading = False
    b2._running = 0
    b2._batch_running = True           # 整队进行中，中间没有任何 pending 结束
    b2._release_pending = False
    b2._released_once = False
    b2._lock = _th.Lock()
    b2._settings_raw = lambda: {}
    b2._active_backend = "openvino"
    rel2 = []
    b2.release_model = lambda reason="manual": (rel2.append(reason),
                                                {"ok": True, "released": True})[1]
    b2._end_run()
    r.check("整队进行中不逐档释放（避免每档重新编译）", rel2 == [], str(rel2))
    b2._batch_running = False
    b2._end_run()
    r.check("整队结束后释放", len(rel2) == 1, str(rel2))


def main() -> int:
    r = Result()
    test_batch_page_layout(r)
    test_batch_no_trial(r)
    test_i18n(r)
    test_resume_and_duration(r)
    test_download_center(r)
    test_model_page(r)
    test_memory_and_launch(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
