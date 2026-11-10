"""Wave 3/4 regression: Edge --app default (G1), update source (B7),
persisted UI controls (B3), structured progress + live-subtitle SSE.

No OpenVINO weights, no pywebview, no network, no Windows needed.

    python tests/test_wave34.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import types
import wave
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Small WAV + injected VAD / ASR
# ---------------------------------------------------------------------------

class MockVAD:
    def __init__(self):
        self.calls = 0

    def run(self, _names, feed):
        x = feed["input"][0]
        self.calls += 1
        p = 0.95 if float(np.sqrt(np.mean(x ** 2))) > 0.005 else 0.01
        return np.array([[p]], dtype=np.float32), feed["h"], feed["c"]


class FakeEngine:
    max_chunk_secs = 30

    def __init__(self):
        self.ready = True
        self.vad_sess = MockVAD()
        self.calls = 0

    def transcribe(self, audio, language=None, context=None, **kw):
        self.calls += 1
        return "这是流式结构化进度与实时字幕的测试字幕，用于验证 SSE 批次。"


def make_wav(path: Path, seconds: float = 12.0):
    sr = 16000
    n = int(sr * seconds)
    t = np.arange(n) / sr
    # alternating 2s speech / 1s silence
    env = (np.sin(2 * np.pi * 220 * t) * 0.2).astype(np.float32)
    gate = ((t % 3.0) < 2.0).astype(np.float32)
    pcm = (env * gate * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qwenasr_wave34_"))
    r = Result()

    print("\n[Wave 3 / G1] default window is Edge --app, no .NET")
    aw = source("app_webview.py")
    r.check("opt-in env var gate exists",
            'os.environ.get("QWENASR_NATIVE_WINDOW"' in aw)
    r.check("native window is conditional, never unconditional",
            "native_requested and webview2_available()" in aw and
            "native_ok = has_wv2 and run_native_window(url)" not in aw)
    r.check("Edge --app path remains", "--app={url}" in aw and "--inprivate" in aw)
    tree = ast.parse(aw)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(main_fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "run_native_window"]
    r.check("run_native_window called exactly once inside a guard", len(calls) == 1,
            str(len(calls)))
    br = source("build_release.py")
    ns = {}
    exec(compile(ast.parse(br), "build_release.py", "exec"), ns) if False else None
    r.check("COLLECT_ALL no longer bundles pywebview/.NET",
            '"pythonnet"' not in br.split("EXCLUDE_MODULES")[0].split("COLLECT_ALL =")[1].split("\n")[0])
    collect_line = next(l for l in br.splitlines() if l.startswith("COLLECT_ALL ="))
    r.check("COLLECT_ALL is tokenizers only", "tokenizers" in collect_line and
            "webview" not in collect_line and "pythonnet" not in collect_line,
            collect_line.strip())
    excl = br.split("EXCLUDE_MODULES = [")[1].split("]")[0]
    r.check("EXCLUDE_MODULES drops webview/pythonnet/clr", all(
        k in excl for k in ('"webview"', '"pythonnet"', '"clr_loader"')))
    r.check("HIDDEN_IMPORTS still includes model_catalog",
            '"model_catalog"' in br)
    r.check("models.json is packaged", '("models.json", ".")' in br)
    # G2: onefile -> onedir layered layout. The build must ask PyInstaller for
    # --onedir and verify the staged payload, not parse a CArchive table.
    r.check("build uses --onedir and not --onefile",
            '"--onedir"' in br and '"--onefile"' not in br)
    r.check("verification checks the layered payload", "_internal/models.json" in br and
            "def verify_onedir(" in br)

    print("\n[Wave 3 / G1] release notes reflect the new behaviour")
    import build_release as build_mod
    txt = build_mod.readme_text("test", "abc1234", True)
    r.check("readme mentions Edge --app", "--app 无痕模式" in txt)
    r.check("readme no longer claims WebView2 runtime required",
            "一般已自带 WebView2 Runtime" not in txt)
    r.check("readme says single streaming path", "所有输入统一走同一条流式路径" in txt)
    r.check("readme no longer splits short/long paths", "短音频（< 20 分钟）仍走原路径" not in txt)
    r.check("readme no longer promises VAD-state loss", "边界最多损失一个语音组" not in txt)
    r.check("readme documents download center", "下载中心" in txt and "aria2c" in txt)
    r.check("readme points updates at fork", "1-1-2/QwenASRGUI" in txt)
    r.check("readme states 5-minute trial", "前 5 分钟" in txt)

    print("\n[Wave 4 / B7] update source points at this fork")
    import version as ver
    r.check("upstream identifier preserved", ver.GITHUB_REPO == "dseditor/QwenASRMiniTool")
    r.check("UPDATE_REPO is this fork", ver.UPDATE_REPO == "1-1-2/QwenASRGUI")
    r.check("releases page is this fork", "1-1-2/QwenASRGUI" in ver.GITHUB_RELEASES_PAGE)
    r.check("latest API is this fork", "1-1-2/QwenASRGUI" in ver.GITHUB_API_LATEST)
    with mock.patch.dict(os.environ, {"QWENASR_UPDATE_REPO": "upstream/x"}):
        import importlib
        v2 = importlib.reload(ver)
        r.check("env override works for upstream PR", v2.UPDATE_REPO == "upstream/x")
    importlib.reload(ver)
    r.check("bridge.js fallback no longer points upstream",
            "dseditor/QwenASRMiniTool/releases/latest" not in source("webview/js/bridge.js"))
    r.check("backend fallback no longer points upstream",
            "dseditor/QwenASRMiniTool/releases/latest" not in source("webview_backend.py"))

    print("\n[Wave 4 / B3] UI controls persist")
    fake_core = types.ModuleType("app")
    fake_core.ASREngine = mock.MagicMock()
    fake_core._DEFAULT_MODEL_DIR = tmp / "ov_models"
    fake_core.SETTINGS_FILE = tmp / "settings.json"
    sys.modules.setdefault("app", fake_core)
    fake_api = types.ModuleType("api_server")
    fake_api._VIDEO_HINT = set()
    fake_api._parse_srt = lambda text: []
    sys.modules.setdefault("api_server", fake_api)
    import webview_backend as wb
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be = wb.WebBackend(on_event=None)
    be._emit = lambda e, p: None
    wb.BASE_DIR = tmp
    defaults = be.get_settings()
    r.check("get_settings exposes transcription keys", all(
        k in defaults for k in ("language", "diarize", "nSpeakers", "align", "hint", "trial")))
    be.set_settings({"language": "zh", "diarize": False, "nSpeakers": "3",
                     "align": False, "hint": "量子力学 薛定谔方程", "trial": True})
    got = be.get_settings()
    r.check("settings round-trip", all([
        got["language"] == "zh", got["diarize"] is False, got["nSpeakers"] == "3",
        got["align"] is False, got["hint"] == "量子力学 薛定谔方程", got["trial"] is True,
    ]), json.dumps({k: got[k] for k in ("language", "diarize", "nSpeakers",
                                        "align", "hint", "trial")}, ensure_ascii=False))
    raw = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
    r.check("values land in settings.json", raw.get("hint") == "量子力学 薛定谔方程" and
            raw.get("trial") is True and raw.get("n_speakers") == "3")
    # A second instance must read the same persisted values.
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be2 = wb.WebBackend(on_event=None)
    r.check("restart restores controls", be2.get_settings()["hint"] == "量子力学 薛定谔方程")
    r.check("legacy keys still work", be2.get_settings()["scale"] == 100)

    print("\n[Wave 4] structured progress + throttled live subtitles")
    from longform_runner import ArrayPcmStream, LongformRunner, SR
    wav = make_wav(tmp / "lesson.wav", 12.0)
    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    details, batches, legacy = [], [], []

    def detail_cb(d):
        details.append(d)

    def segment_cb(p):
        batches.append(p)

    runner = LongformRunner(
        engine=FakeEngine(), vad_sess=MockVAD(),
        out_srt=tmp / "out.srt", work_dir=tmp / "state",
        task_id="w34", fingerprint="fp", max_chunk_secs=30,
        progress_cb=lambda p, s: legacy.append((p, s)),
        detail_cb=detail_cb, segment_cb=segment_cb)
    res = runner.run(None, pcm_source=ArrayPcmStream(pcm),
                     total_sec=12.0, resume=False)
    r.check("runner completed", res["status"] == "done", res["status"])
    r.check("legacy progress_cb unchanged", len(legacy) > 0 and
            all(isinstance(p, int) and isinstance(s, str) for p, s in legacy))
    running = [d for d in details if d.get("phase") == "running"]
    r.check("detail events emitted", len(running) > 0, str(len(running)))
    d0 = running[0] if running else {}
    r.check("detail has doneSec/totalSec/groups/etaSec/phase", all(
        k in d0 for k in ("doneSec", "totalSec", "groups", "etaSec", "phase")), str(d0))
    r.check("detail carries taskId", d0.get("taskId") == "w34")
    r.check("doneSec is monotonic", all(
        running[i]["doneSec"] <= running[i + 1]["doneSec"]
        for i in range(len(running) - 1)))
    r.check("final detail is phase=done at 100%",
            details[-1].get("phase") == "done" and details[-1].get("pct") == 100)
    segs = [s for b in batches for s in b["segments"]]
    srt_lines = [l for l in (tmp / "out.srt").read_text(encoding="utf-8").split("\n\n") if l.strip()]
    r.check("all subtitles delivered in SSE batches", len(segs) == len(srt_lines),
            f"sse={len(segs)} srt={len(srt_lines)}")
    r.check("segment fields complete", all(
        set(("index", "start", "end", "text")) <= set(s) for s in segs))
    r.check("indices are sequential", [s["index"] for s in segs] == list(range(1, len(segs) + 1)))
    r.check("batching actually throttles", len(batches) <= (len(segs) // 8 + 2),
            f"batches={len(batches)} lines={len(segs)}")
    r.check("batch payload has taskId/count", all(
        b.get("taskId") == "w34" and b.get("count") == len(b["segments"]) for b in batches))
    # Legacy callers: no detail_cb / segment_cb must still work unchanged.
    runner2 = LongformRunner(
        engine=FakeEngine(), vad_sess=MockVAD(),
        out_srt=tmp / "out2.srt", work_dir=tmp / "state2",
        task_id="w34b", max_chunk_secs=30,
        progress_cb=lambda p, s: None)
    res2 = runner2.run(None, pcm_source=ArrayPcmStream(pcm),
                       total_sec=12.0, resume=False)
    r.check("no callbacks is still safe", res2["status"] == "done")

    print("\n[Wave 4] frontend wiring (static)")
    app = source("webview/js/app.js")
    html = source("webview/index.html")
    i18n = source("webview/js/i18n.js")
    r.check("frontend listens to progress-detail", 'API.on("progress-detail"' in app)
    r.check("frontend listens to segments", 'API.on("segments"' in app)
    r.check("live subtitle DOM bounded", "LIVE_MAX = 200" in app and
            "childElementCount > LIVE_MAX" in app)
    r.check("live panel follows / pauses on scroll", "liveFollow" in app and
            'addEventListener("scroll"' in app)
    r.check("time/segment/ETA elements exist", all(
        f'id="{x}"' in html for x in ("prog-time", "prog-groups", "prog-eta", "prog-phase")))
    r.check("percentage bar retained", 'id="prog-bar"' in html and 'id="prog-pct"' in html)
    r.check("live subtitle panel exists", 'id="live-list"' in html and 'id="live-panel"' in html)
    r.check("batch page shares options hint", 'batch.sharedOpts' in html)
    r.check("batch page no longer has duplicate toggles",
            'id="sw-batch-diar"' not in html and 'id="sel-batch-spk"' not in html)
    r.check("batch opts reuse shared controls",
            'hint: $("#hint-box").value.trim()' in app and
            'diarize: $("#sw-diar").checked' in app)
    r.check("batch summary shows overall percentage", "總進度" in app)
    r.check("i18n has sharedOpts in three languages", '"batch.sharedOpts"' in i18n)

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
