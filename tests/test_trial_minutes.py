"""試跑時長（分鐘）就地編輯的讀寫、夾緊與接線。

為什麼要有這個測試：試跑時長決定「這次只跑多遠」，寫死 5 分鐘對想快速
看效果的人太長、對想評估長文的人太短。它放在音檔頁開關旁就地編輯，
不進設定頁 —— 但一樣要能存、能夾、能回填，否則重啟就跳回 5。

跑法：python tests/test_trial_minutes.py
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
    fake_core = types.ModuleType("app")
    fake_core.ASREngine = mock.MagicMock()
    fake_core._DEFAULT_MODEL_DIR = tmp / "ov_models"
    fake_core.SETTINGS_FILE = tmp / "settings.json"
    fake_core.SRT_DIR = tmp / "subtitles"
    # 用直接赋值而非 setdefault：第二次建 backend 時要讓 core.SETTINGS_FILE
    # 指向新的臨時目錄，否則讀到的還是上一個（可能已刪除）的路徑。
    sys.modules["app"] = fake_core
    fake_api = types.ModuleType("api_server")
    fake_api._VIDEO_HINT = set()
    fake_api._parse_srt = lambda text: []
    sys.modules["api_server"] = fake_api

    import webview_backend as wb
    wb.BASE_DIR = tmp
    # webview_backend 在第一次 import 時就把 `core` 綁定到當時的假 app 模組，
    # 之後再換 sys.modules["app"] 也改不動它。直接改 wb.core 上的路徑，
    # 這樣第二次建 backend 才會讀到新的臨時目錄。
    wb.core.SETTINGS_FILE = tmp / "settings.json"
    if hasattr(wb.core, "SRT_DIR"):
        wb.core.SRT_DIR = tmp / "subtitles"
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

        print("\n[1] 預設值與範圍")
        r.check("預設 5 分鐘", be._trial_minutes() == 5, str(be._trial_minutes()))
        s = be.get_settings()
        r.check("get_settings 回 trialMinutes", s.get("trialMinutes") == 5,
                str(s.get("trialMinutes")))
        r.check("回 trialMinutesRange = [1, 60]",
                s.get("trialMinutesRange") == [1, 60],
                str(s.get("trialMinutesRange")))

        print("\n[2] 往返")
        be.set_settings({"trialMinutes": 12})
        r.check("讀回 12", be.get_settings()["trialMinutes"] == 12)
        raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        r.check("落地鍵名 trial_minutes", raw.get("trial_minutes") == 12,
                str(raw.get("trial_minutes")))
        r.check("存成整數", isinstance(raw["trial_minutes"], int))

        print("\n[3] 夾緊")
        be.set_settings({"trialMinutes": 0})
        r.check("0 → 1 分鐘（否則試跑等於什麼都不做）",
                be.get_settings()["trialMinutes"] == 1)
        be.set_settings({"trialMinutes": 9999})
        r.check("9999 → 60 分鐘（再長就不叫試跑了）",
                be.get_settings()["trialMinutes"] == 60)
        be.set_settings({"trialMinutes": -7})
        r.check("-7 → 1", be.get_settings()["trialMinutes"] == 1)

        print("\n[4] 壞值回退預設")
        be.set_settings({"trialMinutes": "abc"})
        r.check("字串 → 5", be.get_settings()["trialMinutes"] == 5)
        be.set_settings({"trialMinutes": None})
        r.check("None → 5", be.get_settings()["trialMinutes"] == 5)

        print("\n[5] 手改 settings.json 也能救回")
        (tmp / "settings.json").write_text(
            json.dumps({"trial_minutes": 0.5}), encoding="utf-8")
        r.check("0.5 → 1（四捨五入後仍夾在下限）",
                be.get_settings()["trialMinutes"] == 1,
                str(be.get_settings()["trialMinutes"]))

        print("\n[6] 未涉及的鍵不動")
        be.set_settings({"hint": "不該被試跑時長影響"})
        raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        r.check("hint 保留", raw.get("hint") == "不該被試跑時長影響")

    print("\n[7] 接線：後端真的用它算 limit_sec")
    src = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
    r.check("limit_sec 來自 trial_mins", "limit_sec = trial_mins * 60 if trial else None" in src)
    r.check("trial_mins 來自 _trial_minutes()", "trial_mins = self._trial_minutes()" in src)
    r.check("不再 import 寫死的 TRIAL_MINUTES",
            "probe_duration, TRIAL_MINUTES" not in src)
    r.check("執行紀錄的分鐘數跟著設定",
            '_t.render("progress.trialPrefix", {"mins": trial_mins})' in src)

    print("\n[7b] 切分預檢與試跑共用同一個時長")
    # 使用者心裡只有一個「前 N 分鐘」，不該有兩個旋鈕。預檢不跑辨識，
    # 跟著試跑時長走不會明顯變慢。
    r.check("預檢讀 _trial_minutes()",
            'or self._trial_minutes()' in src)
    r.check("預檢提示文案用動態分鐘數",
            "切分預檢：只分析前 {_prev_mins} 分鐘" in src)
    r.check("不再直接引用 PREVIEW_MINUTES",
            "import vad_preview as _vp, PREVIEW_MINUTES" not in src)
    r.check("vad_preview 仍保留 minutes 參數（供覆寫）",
            "minutes=_prev_mins" in src)
    lr = (ROOT / "longform_runner.py").read_text(encoding="utf-8")
    r.check("常數註釋說明只是預設值，非固定 5 分鐘",
            "只是「呼叫方沒傳值時」的預設" in lr)

    print("\n[7c] 預檢真的把設定值傳進 vad_preview（不是只改了文案）")
    import threading
    import longform_runner as lrmod
    captured = {}

    def fake_vp(**kw):
        captured.update(kw)
        return {"groupCount": 0, "groups": []}

    # 這一段必須在 TemporaryDirectory 內：_trial_minutes() 讀的是
    # core.SETTINGS_FILE，出了 with 塊那個路徑就沒了，只會拿到預設 5。
    with tempfile.TemporaryDirectory() as td2:
        tmp2 = Path(td2)
        wb2, be2 = _make_backend(tmp2)
        be2._emit = lambda e, p: None
        wav = tmp2 / "probe.wav"
        wav.write_bytes(b"RIFF")
        with mock.patch.object(lrmod, "vad_preview", fake_vp):
            be2._ensure_engine = lambda: None
            be2.engine = types.SimpleNamespace(vad_sess=object(), max_chunk_secs=20)
            be2._resolve_ffmpeg = lambda: "ffmpeg"
            be2._cancel_event = threading.Event()
            be2.set_settings({"trialMinutes": 18})
            be2.vad_preview({"path": str(wav)})
        r.check("minutes 跟隨 trial_minutes = 18",
                captured.get("minutes") == 18, str(captured.get("minutes")))
        captured.clear()
        with mock.patch.object(lrmod, "vad_preview", fake_vp):
            be2.vad_preview({"path": str(wav), "minutes": 3})
        r.check("opts.minutes 仍可覆寫（CLI / 測試用）",
                captured.get("minutes") == 3, str(captured.get("minutes")))
        captured.clear()
        with mock.patch.object(lrmod, "vad_preview", fake_vp):
            be2.vad_preview({"path": str(wav), "minutes": 999})
        r.check("opts.minutes=999 → 60", captured.get("minutes") == 60,
                str(captured.get("minutes")))

    print("\n[8] 前端：就地編輯框在開關裡，且不進設定頁")
    html = (ROOT / "webview/index.html").read_text(encoding="utf-8")
    r.check("有 #trial-mins 數字框", 'id="trial-mins"' in html)
    r.check("型別與範圍正確",
            'id="trial-mins" type="number" min="1" max="60"' in html)
    # 必須在同一個 label 內，否則「試跑前 __ 分鐘」會被拆成兩段視覺元素
    seg = html[html.index('id="sw-trial"'):html.index('id="trial-mins"') + 200]
    r.check("與 #sw-trial 同一個 label 內", "</label>" not in seg.split('id="trial-mins"')[0][-400:])
    r.check("沒有在設定頁重複一份",
            html.count('id="trial-mins"') == 1)
    r.check("文案已拆成前綴 + 單位",
            '"file.trialPre"' in (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8")
            and '"file.trialUnit"' in (ROOT / "webview/js/i18n.js").read_text(encoding="utf-8"))

    app = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    r.check("點輸入框不會把開關切掉（擋冒泡）",
            'el.addEventListener(ev, e => e.stopPropagation())' in app)
    r.check("change 時存檔", "API.setSettings({ trialMinutes: v })" in app)
    r.check("徽章文案帶分鐘數",
            'T("file.trialBadge", null, { mins: _trialMins })' in app)
    r.check("執行紀錄帶分鐘數",
            'T("run.trialNote", null, { mins: _trialMins })' in app)
    r.check("批量頁設定摘要帶分鐘數",
            'T("batch.cfgTrial", null, { mins: _trialMins })' in app)

    print("\n" + "=" * 56)
    print(f"通過 {r.pass_} / 失敗 {r.fail}")
    if r.fails:
        print("失敗項：")
        for n in r.fails:
            print("  -", n)
    return 1 if r.fail else 0


if __name__ == "__main__":
    sys.exit(main())
