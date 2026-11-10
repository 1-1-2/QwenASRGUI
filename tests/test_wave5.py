"""Wave 5 regression: J5 TLS downgrade warning, C1 cross-segment punctuation,
C2 handout export.

    python tests/test_wave5.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import subtitle_extras as se  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


SRT = """1
00:00:00,000 --> 00:00:02,500
大家好歡迎來到這堂課

2
00:00:02,600 --> 00:00:05,000
，今天我們要講的是語音辨識

3
00:00:05,100 --> 00:00:08,000
首先看第一個重點，

4
00:00:12,000 --> 00:00:15,000
接著是第二段的主題
"""


def main() -> int:
    r = Result()

    print("\n[C1] SRT parsing and punctuation refinement")
    entries = se.parse_srt(SRT)
    r.check("parsed 4 entries", len(entries) == 4, str(len(entries)))
    r.check("timestamps parsed", entries[0]["start"] == 0.0 and
            entries[3]["start"] == 12.0, str([e["start"] for e in entries]))
    refined = se.refine_punctuation(entries)
    texts = [e["text"] for e in refined]
    r.check("leading punctuation stripped", not texts[1].startswith("，"), texts[1])
    r.check("unterminated line gets a comma", texts[0].endswith("，"), texts[0])
    r.check("comma ending before a new sentence becomes a full stop",
            texts[2].endswith("。"), texts[2])
    r.check("last line gets a full stop", texts[3].endswith("。"), texts[3])
    r.check("timestamps unchanged",
            [e["start"] for e in refined] == [e["start"] for e in entries] and
            [e["end"] for e in refined] == [e["end"] for e in entries])
    r.check("refinement is deterministic",
            se.refine_punctuation(entries) == refined)
    roundtrip = se.parse_srt(se.format_srt(refined))
    r.check("format_srt round-trips", len(roundtrip) == 4 and
            roundtrip[0]["text"] == texts[0])

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "lesson.srt"
        src.write_text(SRT, encoding="utf-8")
        before = src.read_bytes()
        dst = se.refine_srt_file(src)
        r.check("writes a separate .punct.srt", dst.name == "lesson.punct.srt")
        r.check("original SRT is byte-identical", src.read_bytes() == before)
        r.check("refined copy parses", len(se.parse_srt(dst.read_text(encoding="utf-8"))) == 4)

        print("\n[C2] handout export")
        md = se.build_handout(entries, fmt="md", title="語音辨識導論", gap_sec=2.0)
        r.check("markdown has a title", md.startswith("# 語音辨識導論"))
        r.check("markdown keeps time markers", "[00:00]" in md and "[00:12]" in md)
        r.check("pause splits paragraphs", "接著是第二段" in md and md.count("[00:") >= 2)
        txt = se.build_handout(entries, fmt="txt", title="語音辨識導論")
        r.check("txt has no time markers", "[00:00]" not in txt and "大家好" in txt)
        r.check("markdown is deterministic", se.build_handout(entries, "md", "x") ==
                se.build_handout(entries, "md", "x"))
        md_path = se.export_handout_file(src, "md")
        txt_path = se.export_handout_file(src, "txt")
        r.check("exports both formats", md_path.suffix == ".md" and txt_path.suffix == ".txt")
        r.check("exports are non-empty", md_path.stat().st_size > 20 and
                txt_path.stat().st_size > 10)
        r.check("exports do not modify the source", src.read_bytes() == before)
        try:
            se.build_handout([], "md")
            r.check("empty input raises", False)
        except ValueError:
            r.check("empty input raises", True)

        print("\n[C1/C2] backend path validation")
        # _safe_srt must reject paths outside the subtitle directory.
        import types
        sys.modules.setdefault("app", types.ModuleType("app"))
        fake = sys.modules["app"]
        fake.SRT_DIR = Path(td)
        fake.SETTINGS_FILE = Path(td) / "settings.json"
        fake.BASE_DIR = Path(td)
        fake.ASREngine = object
        # Stub the rest of webview_backend's app.core surface lazily used.
        import app_paths
        app_paths.app_dir = lambda: Path(td)
        import webview_backend as wb
        r.check("outside path rejected", _rejects(wb, str(Path(td).parent / "etc" / "passwd")))
        r.check("missing file rejected", _rejects(wb, str(Path(td) / "nope.srt")))
        r.check("valid path accepted", wb.WebBackend._safe_srt(str(src)) == src.resolve())

    print("\n[J5] TLS verification downgrade warning")
    import downloader
    seen = []
    downloader.set_ssl_insecure_hook(lambda reason: seen.append(reason))
    downloader._SSL_INSECURE = False
    # Force the insecure fallback by making both verified paths fail.
    with mock.patch("ssl.create_default_context", side_effect=OSError("no CA")), \
         mock.patch.dict(sys.modules, {"certifi": None}):
        ctx = downloader._ssl_ctx()
    import ssl
    r.check("fallback returns CERT_NONE", ctx.verify_mode == ssl.CERT_NONE)
    r.check("insecure flag is set", downloader.ssl_insecure_active() is True)
    r.check("hook fired once", len(seen) == 1, str(seen))
    r.check("hook explains the reason", "CA bundle" in seen[0])
    # A later successful context clears the flag.
    downloader._SSL_INSECURE = True
    with mock.patch("ssl.create_default_context", return_value=object()):
        downloader._ssl_ctx()
    r.check("flag clears when verification is available",
            downloader.ssl_insecure_active() is False)

    backend = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
    server = (ROOT / "webview_server.py").read_text(encoding="utf-8")
    appjs = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    html = (ROOT / "webview/index.html").read_text(encoding="utf-8")
    r.check("backend registers the SSL hook", "set_ssl_insecure_hook" in backend)
    r.check("status exposes sslInsecure", '"sslInsecure"' in backend)
    r.check("UI has a warning banner", 'id="global-warning"' in html and
            "showGlobalWarning" in appjs)
    r.check("UI listens for warning events", 'API.on("warning"' in appjs)
    r.check("C1/C2 routes are wired", '"/api/refine-punct"' in server and
            '"/api/export-handout"' in server)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


def _rejects(wb, path: str) -> bool:
    try:
        wb.WebBackend._safe_srt(path)
        return False
    except (ValueError, FileNotFoundError):
        return True


if __name__ == "__main__":
    raise SystemExit(main())
