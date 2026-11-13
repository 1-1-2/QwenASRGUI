"""后端集成测试：验证 webview_backend.transcribe() 确实接入了长音频 Runner。

用 unittest.mock 替换掉 app / GUI 依赖，只保留真实的调度层与 Runner，
因此不需要 OpenVINO 权重、pywebview 或 Windows WebView2 即可运行。

    python tests/test_integration.py
"""

from __future__ import annotations

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
sys.path.insert(0, str(HERE))

from make_fixture import build  # noqa: E402


# ── 伪 app 模块（webview_backend 会 `import app as core`）──

def _split_to_lines(text: str):
    return [p.strip() for p in re.split(r"[，。？！；：,.!?;:\n]+", text)
            if p.strip()]


def _assign_ts(lines, t0, t1):
    if not lines:
        return []
    total = sum(max(1, len(x)) for x in lines)
    span = max(0.1, t1 - t0)
    out, cur = [], t0
    for line in lines:
        end = cur + span * max(1, len(line)) / total
        out.append((cur, end, line))
        cur = end
    return out


fake_core = types.ModuleType("app")
fake_core.ASREngine = mock.MagicMock()
fake_core._split_to_lines = _split_to_lines
fake_core._assign_ts = _assign_ts
fake_core.SRT_DIR = None
sys.modules["app"] = fake_core

# webview_backend 会无条件 `from api_server import _VIDEO_HINT / _parse_srt`
def _parse_srt(text: str):
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        m = re.search(
            r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*"
            r"(\d\d):(\d\d):(\d\d),(\d\d\d)", lines[1])
        if not m:
            continue
        g = m.groups()

        def to_s(h, mi, s, ms):
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000

        out.append({"start": to_s(*g[:4]), "end": to_s(*g[4:]),
                    "text": "\n".join(lines[2:])})
    return out


fake_api = types.ModuleType("api_server")
fake_api._VIDEO_HINT = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
fake_api._parse_srt = _parse_srt
sys.modules["api_server"] = fake_api

import webview_backend as wb  # noqa: E402


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
        self._last_segments_rich = None
        self.calls = 0

    def transcribe(self, audio, language=None, context=None, **kw):
        self.calls += 1
        return "这是长音频流式模式的集成测试字幕，用于验证后端接线。"


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def make_backend(tmp: Path):
    """构造一个脱离 GUI / 设置文件的 WebBackend。"""
    # 构造期需要绕开设置文件读写，用类级 patch；其余改为实例级覆盖，
    # 在整个测试期间持续生效（with 退出后不会失效）。
    with mock.patch.object(wb.WebBackend, "_seed_defaults", lambda self: None), \
         mock.patch.object(wb.WebBackend, "_apply_runtime_prefs", lambda self: None):
        be = wb.WebBackend(on_event=None)
    be._settings_raw = lambda: {"output_format": "srt",
                                "longform_minutes": 20,
                                "vad_threshold": 0.5,
                                # Wave 12：本測試要觀察 FakeEngine 的呼叫
                                # 次數；轉檔後自動釋放會把 engine 換成空殼，
                                # 故這裡明確關掉（生命週期由 test_wave12 覆蓋）。
                                "auto_release_model": False}
    be._emit = lambda e, p: None
    be._apply_chunk_secs = lambda *a, **k: None
    be._resolve_ffmpeg = lambda: "ffmpeg"
    be.engine = FakeEngine()
    fake_core.SRT_DIR = tmp / "subtitles"
    wb.BASE_DIR = tmp          # 断点目录也落在临时目录，不污染仓库
    return be


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qwenasr_integration_"))
    r = Result()
    print("\n[集成] webview_backend → 长音频 Runner")

    # 一段约 12 秒、含静音的测试音频
    pcm, _ = build(0.2, seed=11)
    wav = tmp / "course.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm.tobytes())

    # ── 1. 显式 longform=True ──
    be = make_backend(tmp)
    events = []
    res = be.transcribe({"path": str(wav), "longform": True, "hint": ""},
                        progress_cb=lambda p, s: events.append((p, s)))
    srt = Path(res["srtPath"])
    ckpt_dir = tmp / "longform_state"
    r.check("返回 segments / srtPath",
            "segments" in res and bool(res["srtPath"]) and len(res["segments"]) > 0,
            f"segments={len(res.get('segments') or [])}")
    r.check("SRT 文件已生成", srt.exists())
    r.check("SRT 含字幕", srt.exists() and "--> " in srt.read_text(encoding="utf-8"))
    r.check("引擎被流式调用", be.engine.calls > 0, f"{be.engine.calls} 次")
    r.check("进度已回传", len(events) > 0, f"{len(events)} 条")
    r.check("断点目录已建立", ckpt_dir.exists(),
            str(ckpt_dir) if ckpt_dir.exists() else "未生成（Runner 未触发）")

    # ── 2. 统一流式：不再按时长判定（换个路径，避免命中上一条的 done 断点）──
    wav2 = tmp / "course_auto.wav"
    with wave.open(str(wav2), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    be2 = make_backend(tmp)
    be2.engine = FakeEngine()
    be2._cancel_event.clear()
    with mock.patch("longform_runner.probe_duration", return_value=3600.0):
        res2 = be2.transcribe({"path": str(wav2), "name": "auto.wav"},
                              progress_cb=lambda p, s: None)
    r.check("统一流式路径生效", bool(res2.get("segments")) and be2.engine.calls > 0,
            f"无论时长都应走流式路径；"
            f"segments={len(res2.get('segments') or [])} calls={be2.engine.calls} "
            f"srt={res2.get('srtPath')}")

    # ── 3. cancel() 会通知 Runner ──
    be3 = make_backend(tmp)
    be3.cancel()
    r.check("cancel() 设置取消事件", be3._cancel_event.is_set())

    # ── 4. 同名文件：默认保留旧稿，不静默覆盖（设定→同名文件=数字后缀）──
    # 换一组参数（hint）→ 指纹不符 → 从头重跑；旧稿必须还在，新结果另存
    # 「course (1).srt」。这就是 A 策略与「静默覆盖」的区别。
    be4 = make_backend(tmp)
    res4 = be4.transcribe({"path": str(wav), "hint": "换一组参数"},
                          progress_cb=lambda p, s: None)
    srt4 = Path(res4["srtPath"])
    r.check("参数变了 → 另存 course (1).srt", srt4.name == "course (1).srt",
            f"{srt4.name}（sub={srt4.parent.name}）")
    r.check("旧稿仍在且未被清空", (tmp / "subtitles" / "course.srt").exists() and
            "--> " in (tmp / "subtitles" / "course.srt").read_text(encoding="utf-8"))
    r.check("新稿也有字幕", srt4.exists() and "--> " in srt4.read_text(encoding="utf-8"))

    # 续跑（同一组参数）必须写回原来的那份，而不是又开一个 (2)。
    be5 = make_backend(tmp)
    res5 = be5.transcribe({"path": str(wav), "hint": "换一组参数"},
                          progress_cb=lambda p, s: None)
    r.check("续跑沿用同一份 (1)，不新增副本",
            Path(res5["srtPath"]).name == "course (1).srt", res5.get("srtPath"))
    r.check("没有意外的 course (2).srt",
            not (tmp / "subtitles" / "course (2).srt").exists())

    # ── 5. 拖放（上传副本）是「一次性」：不支持接续，断点用完即清 ──
    # 副本落在随机临时目录、请求结束就删，task_id 取自这条随机路径 →
    # 重新拖入必然是新任务。这也是介面文案「拖放不支持接续」的依据。
    up1 = tmp / "up1" / "upload.wav"
    up2 = tmp / "up2" / "upload.wav"
    for p in (up1, up2):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(wav.read_bytes())
    be6 = make_backend(tmp)
    res6 = be6.transcribe({"path": str(up1), "name": "課程.wav",
                           "uploaded": True}, progress_cb=lambda p, s: None)
    calls6 = be6.engine.calls
    r.check("上传来源能正常转写", calls6 > 0 and bool(res6.get("segments")),
            f"calls={calls6} segments={len(res6.get('segments') or [])}")

    import sqlite3
    up_id = wb.WebBackend._source_identity(up1)[0]
    local_id = wb.WebBackend._source_identity(wav)[0]
    con = sqlite3.connect(str(tmp / "longform_state" / "checkpoints.db"))
    try:
        rows = {r[0] for r in con.execute("SELECT task_id FROM progress")}
    finally:
        con.close()
    r.check("拖放任务的断点用完即清（永远接不上）", up_id not in rows, str(sorted(rows)))
    r.check("拖放任务的逐段记录也删了",
            not (tmp / "longform_state" / f"{up_id}.segments.jsonl").exists())
    r.check("本机路径的断点仍然保留（点选可续跑）", local_id in rows, str(sorted(rows)))

    # 换个临时目录再拖一次 ⇒ 新任务、从头跑、另存 (1)：与「不支持接续」一致
    be7 = make_backend(tmp)
    res7 = be7.transcribe({"path": str(up2), "name": "課程.wav",
                           "uploaded": True}, progress_cb=lambda p, s: None)
    r.check("重新拖入会从头开始（不接续）", be7.engine.calls > 0,
            f"calls={be7.engine.calls}")
    r.check("重拖另存为 (1)，不覆盖上一份",
            Path(res7["srtPath"]).name == "課程 (1).srt", res7.get("srtPath"))

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
