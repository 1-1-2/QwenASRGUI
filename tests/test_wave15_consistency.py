"""Wave 15 regression: 自检/下载中心一致、拖放提示、rich.json 中断、队列排序。

These four came out of a round of manual testing:

1. 下载中心的 VAD 与系统自检结论不一致（自检绿、下载中心说未下载），
   原因是两边各写一套路径查找逻辑。
2. 批量页拖放拿不到本机路径（Chromium 限制），提示要能说清原因与替代操作。
3. 中断后完全没有 rich.json —— 旧版在 paused 分支直接 raise，跳过了 finalize()。
4. 排队中的项目无法调整顺序。

Run:  python tests/test_wave15_batch_order.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch_queue as bq          # noqa: E402
import ui_text                    # noqa: E402
import webview_backend as wb      # noqa: E402
import app as core                # noqa: E402

APP = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
BRIDGE = (ROOT / "webview/js/bridge.js").read_text(encoding="utf-8")
I18N = (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
HTML = (ROOT / "webview/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "webview/css/app.css").read_text(encoding="utf-8")
SERVER = (ROOT / "webview_server.py").read_text(encoding="utf-8")
QUEUE = (ROOT / "batch_queue.py").read_text(encoding="utf-8")
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


def _backend_with_model_dir(model_dir: Path):
    """A WebBackend with no model loaded, pointed at ``model_dir``."""
    b = object.__new__(wb.WebBackend)
    eng = core.ASREngine()
    eng.ready = False
    eng.vad_sess = None
    b.engine = eng
    b._loaded = False
    b._load_err = None
    b._loading = False
    b._running = 0
    b._batch_running = False
    b._release_pending = False
    b._progress_scope = None
    b._active_backend = "openvino"
    b._lock = threading.Lock()
    b._settings_raw = lambda: {"model_dir": str(model_dir)}
    b._emit = lambda *a, **k: None
    return b


def _vad_status(h: dict) -> str | None:
    for g in h.get("cores", []):
        for it in g.get("items", []):
            if it.get("key") == "vad":
                return it["status"]
    for it in (h.get("shared") or []):
        if it.get("key") == "vad":
            return it["status"]
    return None


# ── 1. 下载中心 × 自检：VAD 必须同源 ────────────────────────────────────
def test_vad_consistent(r: Result):
    print("\n[1] 下载中心与系统自检的 VAD 结论一致")
    r.check("判定集中在一个共享函数",

            "def _vad_present(" in BACKEND)
    r.check("自检调用共享函数", "_vad_present(model_dir," in BACKEND)
    r.check("下载中心调用同一个函数",
            re.search(r'_decorate_catalog_runtime[\s\S]{0,2000}?_vad_present\(', BACKEND) is not None)

    # 真实场景：用户把 model_dir 指到别处，VAD 仍随套件在安装目录里。
    # 旧版这里自检 green、下载中心 INCOMPLETE（就是用户报的现象）。
    tmp = Path(tempfile.mkdtemp())
    be = _backend_with_model_dir(tmp)
    st = _vad_status(be.health_check())
    cat = {x["id"]: x for x in be.get_model_catalog()["bundles"]}
    vad = cat.get("silero_vad_v4", {})
    bundled = (wb.BASE_DIR / "ov_models" / "silero_vad_v4.onnx").exists()
    if bundled:
        r.check("VAD 随包存在时自检为 green", st == "green", st)
        r.check("同情况下载中心也判 complete", bool(vad.get("complete")),
                f"complete={vad.get('complete')}")
        r.check("下载中心附 runtimeReady 供前端显示",
                vad.get("runtimeReady") is True)
    else:                                 # 干净环境：两边都该说「缺」
        r.check("VAD 缺失时自检非 green", st != "green", st)
        r.check("VAD 缺失时下载中心也非 complete", not vad.get("complete"))

    # 其余 bundle 逐条校验：确保只有 VAD 曾经分岔，现在也没有别的漏网项
    r.check("VAD 的 runtime 注记走三语 key",
            "dl.runtimeVadOk" in ui_text.TEMPLATES and "dl.runtimeVadMissing" in ui_text.TEMPLATES)


def test_health_default_open(r: Result):
    # 需求变更：自检信息量太大，改为**默认收起**，只在红燈（缺且不可自动补）
    # 时自动展开——黄灯可补，不打扰。
    print("\n[2] 自检默认收起，红灯时自动展开")
    r.check("details 默认不 open", re.search(r'id="health-fold"\s+open', HTML) is None)
    r.check("health-sum 只有一处（重复 id 会让徽标永远停在初始文案）",
            HTML.count('id="health-sum"') == 1)
    r.check("红灯时自动展开",
            "h.summary.red > 0) fold.open = true" in APP)
    r.check("初始占位走 i18n（切语言会变）",
            'data-i18n="catalog.checking"' in HTML and "catalog.checking" in I18N)


# ── 3. 拖放：Wave 16 起整条链路取消，改为页面内说明 ──────────────────
def test_drop_hint(r: Result):
    # Wave 15 的「提示里带文件数」已被 Wave 16 推翻：拖放本身不再可用，
    # 因此提示改在页面上静态说明，不再有「收到 N 个文件」这种运行时文案。
    print("\n[3] 拖放已取消，改为页面内说明原因 + 等效入口")
    r.check("拖放代码与 DOM 均已移除",
            "batchDrop" not in APP and 'id="batch-drop"' not in HTML)
    m = re.search(r'"batch\.noDropWhy":\s*\[(.*?)\],\n', I18N, re.S)
    r.check("说明文案三语齐备", m is not None and m.group(1).count('"') >= 6)
    body = m.group(1) if m else ""
    r.check("说明这是浏览器限制而非程序缺陷",
            "Chromium" in body and ("安全" in body or "security" in body))
    r.check("给出等效操作（加入文件按钮）",
            "加入檔案" in body or "加入文件" in body or "Add files" in body)
    # 正式模式绝不能退化成「上传副本」—— 那会产生副本，与「只引用路径」矛盾
    r.check("批量页不存在任何上传路径", "await API.batchAdd(" not in APP)


# ── 4. rich.json：中断也要产出 ─────────────────────────────────────────
def test_rich_on_pause(r: Result):
    print("\n[4] 中断时也生成 rich.json")
    block = re.search(r'if result\["status"\] == "paused":([\s\S]{0,900}?)raise RuntimeError',
                      BACKEND)
    r.check("paused 分支在抛错前调用 finalize()",
            block is not None and "enhancer.finalize(" in block.group(1))
    r.check("finalize 从 JSONL 全量重建（不是增量，不会只写后半段）",
            "authoritative line list" in (ROOT / "postprocess.py").read_text(encoding="utf-8"))
    r.check("finalize 失败不掩盖取消语义",
            block is not None and "except Exception" in block.group(1))


# ── 5. 队列排序 ───────────────────────────────────────────────────────
def _queue_with(n: int):
    state, files = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())

    class FakeBE:
        def transcribe(self, *a, **k):
            return {}

    q = bq.BatchQueue(FakeBE(), on_event=lambda *a: None, state_dir=state)
    ids = []
    for i in range(n):
        p = files / f"f{i}.wav"
        p.write_bytes(b"RIFF")
        ids.append(q.add_path(p, f"f{i}")["id"])
    return q, ids


def test_queue_order(r: Result):
    print("\n[5] 队列拖放排序与置顶")
    q, ids = _queue_with(5)
    names = lambda: [x["name"] for x in q.items]

    r.check("初始顺序保持加入次序", names() == ["f0", "f1", "f2", "f3", "f4"], str(names()))

    q.move(ids[0], 5)          # 拖到末尾
    r.check("拖到末尾", names() == ["f1", "f2", "f3", "f4", "f0"], str(names()))

    q.move(ids[0], 0)          # 拖到最前
    r.check("拖到最前", names() == ["f0", "f1", "f2", "f3", "f4"], str(names()))

    q.move(ids[4], 1)          # 跨多格
    r.check("跨多格位置准确（索引偏移已补偿）",
            names() == ["f0", "f4", "f1", "f2", "f3"], str(names()))

    r.check("放回自己身上不算移动（不触发重排）", q.move(ids[1], 2) is False)

    q.promote(ids[3])
    r.check("置顶到队首", names() == ["f3", "f0", "f4", "f1", "f2"], str(names()))

    # 执行中的项目不能被拖走，也不会被「置顶」盖过
    q.items[0]["status"] = "running"
    q.active_id = ids[3]
    r.check("执行中的项目不可移动", q.move(ids[3], 3) is False)
    q.items[1]["status"] = "pending"
    q.promote(ids[2])
    r.check("置顶不会越过执行中的那一列", names()[0] == "f3", str(names()))

    r.check("落盘（重排後 _save 被调用）", "self._save()" in QUEUE)


def test_queue_wiring(r: Result):
    print("\n[6] 队列排序的 API 接线")
    r.check("路由 /api/batch/move", '"/api/batch/move"' in SERVER)
    r.check("路由支持 promote",
            "body.get(\"promote\")" in SERVER and "batch_queue.promote(" in SERVER)
    r.check("bridge 暴露 batchMove/batchPromote",
            "async batchMove(" in BRIDGE and "async batchPromote(" in BRIDGE)
    r.check("前端调用 batchMove/batchPromote",
            "API.batchMove(" in APP and "API.batchPromote(" in APP)
    r.check("pending 才可拖", 'row.draggable = true' in APP and
            'it.status !== "pending"' in APP)
    r.check("有拖放视觉反馈", ".b-row.dragging" in CSS and ".b-row.drop-before" in CSS)
    r.check("菜单有置顶项", "batch.menuPromote" in APP and "batch.menuPromote" in I18N)
    r.check("置顶项只在非队首 pending 出现",
            "_firstPending" in APP and "batchItems.indexOf(it) > _firstPending" in APP)


def main() -> int:
    r = Result()
    test_vad_consistent(r)
    test_health_default_open(r)
    test_drop_hint(r)
    test_rich_on_pause(r)
    test_queue_order(r)
    test_queue_wiring(r)
    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
