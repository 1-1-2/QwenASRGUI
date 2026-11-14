"""对齐管線三個旋鈕（門控閾值 / 攢批窗口 / 收尾等待）的讀寫與夾緊。

為什麼要有這個測試：這三個值直接決定「對齊跑不跑得起來」。settings.json
是可以手改的，寫進 0.2 GB 的閾值會讓對齊永遠開不起來，寫進 5000 秒的
窗口會讓字幕延遲到使用者以為程式壞了。夾緊邏輯必須有測試釘住。

跑法：python tests/test_fa_tuning.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Report:
    def __init__(self):
        self.pass_ = 0
        self.fail = 0
        self.fails: list[str] = []

    def check(self, name: str, cond, detail: str = ""):
        if cond:
            self.pass_ += 1
            print(f"  ✓ {name}")
        else:
            self.fail += 1
            self.fails.append(name)
            print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def _make_backend(tmp: Path):
    """最小可用的 WebBackend：不載模型、不讀真實 settings。"""
    fake_core = types.ModuleType("app")
    fake_core.ASREngine = mock.MagicMock()
    fake_core._DEFAULT_MODEL_DIR = tmp / "ov_models"
    fake_core.SETTINGS_FILE = tmp / "settings.json"
    fake_core.SRT_DIR = tmp / "subtitles"
    sys.modules.setdefault("app", fake_core)
    fake_api = types.ModuleType("api_server")
    fake_api._VIDEO_HINT = set()
    fake_api._parse_srt = lambda text: []
    sys.modules.setdefault("api_server", fake_api)

    import webview_backend as wb
    wb.BASE_DIR = tmp
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be = wb.WebBackend(on_event=None)
    be._emit = lambda e, p: None
    return wb, be


def main() -> int:
    r = Report()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wb, be = _make_backend(tmp)

        print("\n[1] 預設值")
        s = be.get_settings()
        r.check("faGateGb 預設 4.0", s["faGateGb"] == 4.0, str(s.get("faGateGb")))
        r.check("faWindowSec 預設 26", s["faWindowSec"] == 26, str(s.get("faWindowSec")))
        r.check("faDrainSec 預設 180", s["faDrainSec"] == 180, str(s.get("faDrainSec")))
        r.check("faGateWarnGb = 3.0", s["faGateWarnGb"] == 3.0)
        r.check("faAvailGb 存在且為 float 或 None",
                s["faAvailGb"] is None or isinstance(s["faAvailGb"], float),
                repr(s.get("faAvailGb")))
        r.check("三個 range 都回給前端",
                s["faGateRange"] == [1.0, 32.0]
                and s["faWindowRange"] == [10.0, 40.0]
                and s["faDrainRange"] == [30.0, 900.0])

        print("\n[2] 往返（寫入後讀回同值）")
        be.set_settings({"faGateGb": 3.5, "faWindowSec": 30, "faDrainSec": 600})
        s = be.get_settings()
        r.check("faGateGb 往返", s["faGateGb"] == 3.5, str(s.get("faGateGb")))
        r.check("faWindowSec 往返", s["faWindowSec"] == 30, str(s.get("faWindowSec")))
        r.check("faDrainSec 往返", s["faDrainSec"] == 600, str(s.get("faDrainSec")))
        raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        r.check("落地到 settings.json 的鍵名正確",
                raw.get("fa_gate_gb") == 3.5
                and raw.get("fa_window_sec") == 30
                and raw.get("fa_drain_sec") == 600,
                json.dumps({k: raw.get(k) for k in
                            ("fa_gate_gb", "fa_window_sec", "fa_drain_sec")}))
        r.check("窗口/收尾存成整數（不留 30.0）",
                isinstance(raw["fa_window_sec"], int)
                and isinstance(raw["fa_drain_sec"], int))

        print("\n[3] 夾緊：低於下限抬到下限")
        be.set_settings({"faGateGb": 0.2, "faWindowSec": 3, "faDrainSec": 5})
        s = be.get_settings()
        r.check("0.2 GB → 1.0 GB", s["faGateGb"] == 1.0, str(s.get("faGateGb")))
        r.check("3 秒 → 10 秒", s["faWindowSec"] == 10, str(s.get("faWindowSec")))
        r.check("5 秒 → 30 秒", s["faDrainSec"] == 30, str(s.get("faDrainSec")))

        print("\n[4] 夾緊：高於上限壓到上限")
        be.set_settings({"faGateGb": 999, "faWindowSec": 5000, "faDrainSec": 99999})
        s = be.get_settings()
        r.check("999 GB → 32 GB", s["faGateGb"] == 32.0, str(s.get("faGateGb")))
        r.check("5000 秒 → 40 秒（= MAX_WINDOW_SEC）",
                s["faWindowSec"] == 40, str(s.get("faWindowSec")))
        r.check("99999 秒 → 900 秒", s["faDrainSec"] == 900, str(s.get("faDrainSec")))

        print("\n[5] 壞值退回預設，不寫進 settings.json")
        be.set_settings({"faGateGb": "abc", "faWindowSec": None, "faDrainSec": ""})
        s = be.get_settings()
        r.check("字串 → 4.0", s["faGateGb"] == 4.0, str(s.get("faGateGb")))
        r.check("None → 26", s["faWindowSec"] == 26, str(s.get("faWindowSec")))
        r.check("空字串 → 180", s["faDrainSec"] == 180, str(s.get("faDrainSec")))

        print("\n[6] 手改 settings.json 的壞值也能被救回")
        (tmp / "settings.json").write_text(
            json.dumps({"fa_gate_gb": 0.05, "fa_window_sec": -8,
                        "fa_drain_sec": 1e9}), encoding="utf-8")
        s = be.get_settings()
        r.check("讀取時就夾緊，不必等寫入",
                s["faGateGb"] == 1.0 and s["faWindowSec"] == 10 and s["faDrainSec"] == 900,
                f'{s["faGateGb"]}/{s["faWindowSec"]}/{s["faDrainSec"]}')

        print("\n[7] 未涉及的鍵不被動到")
        be.set_settings({"hint": "不該被旋鈕影響"})
        raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        r.check("hint 保留", raw.get("hint") == "不該被旋鈕影響")

    print("\n[8] AlignQueue 真的吃 window_sec / threshold_gb")
    import fa_queue
    q = fa_queue.AlignQueue(lambda *a: [], src_path="x.m4a", out_path="y.jsonl",
                            threshold_gb=3.5, window_sec=30)
    r.check("threshold_gb 生效", q.threshold_gb == 3.5)
    r.check("window_sec 生效", q.window_sec == 30.0)
    st = q.stats()
    r.check("stats 回報 windowSec/thresholdGb",
            st["windowSec"] == 30.0 and st["thresholdGb"] == 3.5,
            f'{st["windowSec"]}/{st["thresholdGb"]}')

    print("\n[9] LongformRunner 吃 drain_timeout")
    import inspect
    from longform_runner import DRAIN_TIMEOUT_SEC, LongformRunner
    sig = inspect.signature(LongformRunner.__init__)
    r.check("__init__ 有 drain_timeout 參數", "drain_timeout" in sig.parameters)
    r.check("預設值 = DRAIN_TIMEOUT_SEC",
            sig.parameters["drain_timeout"].default == DRAIN_TIMEOUT_SEC,
            str(sig.parameters["drain_timeout"].default))
    r.check("DRAIN_TIMEOUT_SEC = 180", DRAIN_TIMEOUT_SEC == 180.0)

    print("\n[10] 接線：三個值真的傳到 AlignQueue / LongformRunner")
    # 只驗「參數存在」不夠 —— 中間任何一環寫死成常數，使用者的設定就白調了。
    # 這裡直接讀 webview_backend 的建構段落，確認三個值都從 _fa_tune 取。
    src = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
    r.check("AlignQueue 的 threshold 來自設定",
            'threshold_gb=_fa_tune["gate"]' in src)
    r.check("AlignQueue 的 window 來自設定",
            'window_sec=_fa_tune["window"]' in src)
    r.check("LongformRunner 的 drain 來自設定",
            'drain_timeout=_fa_tune["drain"]' in src)
    r.check("建隊列前先讀一次旋鈕（同源）",
            "_fa_tune = self._fa_tuning()" in src)
    r.check("舊的 _st.get(\"fa_gate_gb\") 直讀已移除（避免繞過夾緊）",
            '_st.get("fa_gate_gb"' not in src)
    r.check("_close_queues 用的是實例值",
            "timeout=self.drain_timeout" in
            (ROOT / "longform_runner.py").read_text(encoding="utf-8"))

    print("\n" + "=" * 56)
    print(f"通過 {r.pass_} / 失敗 {r.fail}")
    if r.fails:
        print("失敗項：")
        for n in r.fails:
            print("  -", n)
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
