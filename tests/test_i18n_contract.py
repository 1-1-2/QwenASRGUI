"""H6/H7 contract: backend ui_text.TEMPLATES <-> frontend i18n.js M dictionary.

The whole point of the structured-key rewrite is that a missing translation
must fail CI instead of leaking Traditional Chinese into an English (or
Simplified Chinese) UI. This test loads the real i18n.js in Node and compares
it with the real Python templates.

    python tests/test_i18n_contract.py

No model weights, no GUI, no network. Requires `node` on PATH (already used by
ci.yml for the frontend syntax check).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ui_text  # noqa: E402

PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
CJK = re.compile(r"[\u4e00-\u9fff]")

# Real backend APIs, driven in a subprocess with a stand-in `app` core so the
# probe never needs OpenVINO / customtkinter / a GUI. Collects every structured
# key the backend actually emits and asserts the payloads survive JSON.
PROBE_SCRIPT = r'''
import json, sys, types, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
tmp = tempfile.mkdtemp()
fake = types.ModuleType("app")
def _get(n):
    if n == "ASREngine":
        return lambda: SimpleNamespace(ready=False, processor=None, vad_sess=None)
    if n == "SETTINGS_FILE": return Path(tmp) / "settings.json"
    if n == "_DEFAULT_MODEL_DIR": return Path(tmp) / "ov_models"
    if n == "_BIN_PATH": return Path(tmp) / "ov_models/qwen3-asr-1.7b.bin"
    if n == "resource_path": return lambda *a: str(Path(tmp).joinpath(*a))
    return mock.MagicMock()
fake.__getattr__ = _get
sys.modules["app"] = fake
ctk = types.ModuleType("customtkinter")
ctk.__getattr__ = lambda n: mock.MagicMock()
sys.modules["customtkinter"] = ctk

from webview_backend import WebBackend
b = WebBackend()
payloads, errors, keys = {}, [], set()
for name in ("get_status", "get_languages", "list_devices", "get_basic_profiles",
             "get_model_options", "health_check", "get_accel", "set_accel",
             "set_backend", "set_model"):
    try:
        args = {"set_accel": ("vulkan",), "set_backend": (0,),
                "set_model": ("Qwen", "Qwen3-ASR-0.6B")}.get(name, ())
        v = getattr(b, name)(*args)
        payloads[name] = json.loads(json.dumps(v, ensure_ascii=False))
    except Exception as e:
        errors.append(f"{name}: {type(e).__name__}: {e}")

def walk(x):
    if isinstance(x, dict):
        if isinstance(x.get("k"), str): keys.add(x["k"])
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(payloads)
print(json.dumps({
    "errors": errors,
    "keys": sorted(keys),
    "hardware_k": (payloads.get("get_basic_profiles", {}).get("hardware") or {}).get("k", ""),
    "device_note_k": ((payloads.get("list_devices", {}).get("devices") or [{}])[0].get("note") or {}).get("k", ""),
    "health_label_k": ((payloads.get("health_check", {}).get("cores") or [{}])[0].get("label") or {}).get("k", ""),
    "accel_reason_k": (payloads.get("get_accel", {}).get("reason") or {}).get("k", ""),
    "accel_option_k": (((payloads.get("get_accel", {}).get("options") or [{}])[0].get("label") or {})).get("k", ""),
    "set_model_k": (payloads.get("set_model", {}).get("message") or {}).get("k", ""),
}, ensure_ascii=False))
'''


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


NODE_SCRIPT = r"""
global.window = {};
global.document = {documentElement: {}, querySelectorAll: () => ({forEach() {}})};
require(JS_PATH);
const I = window.I18N;
const messages = {};
for (const k of I.msgKeys) {
  const arr = [];
  for (const lang of ['hant', 'hans', 'en']) {
    I.setLang(lang);
    arr.push(I.renderMsg(k, {}, ''));   // no vars -> placeholders stay intact
  }
  messages[k] = arr;
}
const samples = {};
for (const lang of ['hant', 'hans', 'en']) {
  I.setLang(lang);
  samples[lang] = {
    free: I.L('記憶體不足，請調整預設值，或透過網路下載檔案'),
    structured: I.L({k: 'msg.model.same', v: {core: 'Qwen', model: 'Q8'}, t: 'fallback'}),
    parts: I.L({parts: [{k: 'state.downloaded'}, {k: 'state.ready'}], sep: ' / '}),
    missing: I.L({k: 'no.such.key', v: {}, t: '後備文字'}),
    nested: I.L({k: 'msg.model.withNote', v: {
      message: {k: 'msg.model.same', v: {core: 'Qwen', model: 'Q8'}},
      note: {k: 'state.downloaded'}}}),
  };
}
const dictKeys = I.tKeys;
process.stdout.write(JSON.stringify({messages, samples, dictKeys}));
"""


def load_frontend() -> dict:
    script = NODE_SCRIPT.replace("JS_PATH", json.dumps(str(ROOT / "webview/js/i18n.js")))
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
# Lines whose Chinese is legitimately not user-facing prose:
#   // … or /* … */            comments
#   /^[约約]\s*/ etc.          regex literals (character classes)
#   data-i18n="…"              attribute keys, resolved by I18N.apply
#   font / style declarations  CSS inside template strings
_SKIP_HINTS = (
    'data-i18n', "replace(/", "match(/", "test(/", "font-family",
    "Microsoft JhengHei", "var(--", "cssText",
)


def scan_hardcoded_zh(src: str) -> list[tuple[int, str]]:
    """Return (line, text) for Chinese literals that bypass the dictionary.

    Rules:
      * comments, regex literals, CSS/attribute snippets are ignored;
      * a Chinese string appearing after ``T("key"`` on the same line is the
        deliberate Hant fallback argument — allowed;
      * any other Chinese string literal that reaches textContent / innerHTML
        / title / placeholder / logLine is a leak and gets flagged.
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for no, raw in enumerate(src.splitlines(), 1):
        line = raw
        if in_block:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_block = False
        if line.strip().startswith("//"):
            continue
        if "/*" in line and "*/" not in line:
            line = line.split("/*", 1)[0]
            in_block = True
        if not CJK_CHAR.search(line):
            continue
        if any(h in line for h in _SKIP_HINTS):
            continue
        # Everything after the first T("key" is fallback territory.
        cut = line.find('T("')
        scan = line[:cut] if cut >= 0 else line
        # A T( call opening on this line but listing the fallback on the next
        # line ends with a bare `T("key",` — the following lines are then
        # fallbacks too, which the per-line cut already handles via `cut`.
        if not re.search(r'[\u4e00-\u9fff]', scan):
            continue
        quoted = re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'|`([^`]*)`', scan)
        for a, b, c in quoted:
            seg = a or b or c
            if seg and CJK_CHAR.search(seg):
                out.append((no, seg.strip()))
                break
    return out


def main() -> int:
    r = Result()
    fe = load_frontend()
    messages = fe["messages"]
    samples = fe["samples"]

    print("\n[1] Key sets match (backend templates vs frontend dictionary)")
    be_keys = set(ui_text.TEMPLATES)
    fe_keys = set(messages)
    r.check("same key count", len(be_keys) == len(fe_keys),
            f"backend={len(be_keys)} frontend={len(fe_keys)}")
    r.check("no missing frontend keys", not (be_keys - fe_keys),
            str(sorted(be_keys - fe_keys)))
    r.check("no orphan frontend keys", not (fe_keys - be_keys),
            str(sorted(fe_keys - be_keys)))

    print("\n[2] Every key has three non-empty hand written strings")
    empty = [k for k, v in messages.items()
             if len(v) != 3 or any(not isinstance(s, str) or not s.strip() for s in v)]
    r.check("all three locales populated", not empty, str(empty[:5]))

    print("\n[3] {placeholders} identical in backend template and all 3 locales")
    bad = []
    for key, template in ui_text.TEMPLATES.items():
        want = placeholders(template)
        if key not in messages:
            continue
        if any(placeholders(s) != want for s in messages[key]):
            bad.append((key, sorted(want), [sorted(placeholders(s)) for s in messages[key]]))
    r.check("placeholder sets agree", not bad, str(bad[:3]))

    print("\n[4] Hant frontend text is byte-identical to the backend fallback")
    mismatch = [(k, ui_text.TEMPLATES[k], messages[k][0])
                for k in ui_text.TEMPLATES if k in messages
                and messages[k][0] != ui_text.TEMPLATES[k]]
    r.check("Hant is the single source of truth", not mismatch, str(mismatch[:2]))

    print("\n[5] English text never leaks CJK (the original H6/H7 defect)")
    leaks = [k for k, v in messages.items() if CJK.search(v[2])]
    r.check("no Chinese in English messages", not leaks, str(leaks[:5]))

    print("\n[6] Simplified text uses real wording, not per-character conversion")
    # Model-card wording: 影片/视频, 記憶體/内存, 預設/默认. A character table
    # would produce 影片/记忆体/预设, which no Simplified reader would write.
    desc_hans = messages.get("profile.zh.desc", ["", "", ""])[1]
    r.check("profile description uses 视频", "视频" in desc_hans and "影片" not in desc_hans)
    free_hans = samples["hans"]["free"]
    r.check("free-text fallback uses 内存 / 默认值 / 通过 / 网络 / 文件",
            all(w in free_hans for w in ("内存", "默认值", "通过", "网络", "文件")),
            free_hans)
    r.check("free-text fallback has no broken 记忆体/建續",
            "记忆体" not in free_hans and "建續" not in free_hans)
    r.check("Hans differs from Hant for wording keys",
            messages["msg.backend.restart"][1] != messages["msg.backend.restart"][0])

    print("\n[7] Structured payloads render in all three locales")
    expectations = {
        # unknown keys fall back to `t`; the fallback still respects the active
        # locale, so Hans converts the wording and English keeps it verbatim.
        "hant": ("「Qwen · Q8」已是目前使用的模型。", "已下載 / 已就緒", "後備文字"),
        "hans": ("「Qwen · Q8」已是当前使用的模型。", "已下载 / 已就绪", "后备文字"),
        "en": ("Qwen · Q8 is already in use.", "Downloaded / Ready", "後備文字"),
    }
    for lang, (structured, parts, missing) in expectations.items():
        s = samples[lang]
        r.check(f"{lang}: key+vars", s["structured"] == structured, s["structured"])
        r.check(f"{lang}: joined parts", s["parts"] == parts, s["parts"])
        r.check(f"{lang}: unknown key falls back to t",
                s["missing"] == missing, s["missing"])
        r.check(f"{lang}: nested message resolved",
                "{" not in s["nested"] and "no.such" not in s["nested"],
                s["nested"][:60])
    r.check("en: free-form diagnostics keep the original (by design)",
            samples["en"]["free"] == "記憶體不足，請調整預設值，或透過網路下載檔案")

    print("\n[8] Backend msg()/plain()/join() stay string-safe")
    inner = ui_text.msg("state.downloaded")
    wrapped = ui_text.msg("msg.model.withNote", message=inner, note=inner)
    r.check("msg() carries k/v/t", set(wrapped) == {"k", "v", "t"})
    r.check("plain() resolves nested messages",
            ui_text.plain(wrapped) == "已下載\n已下載", repr(ui_text.plain(wrapped)))
    r.check("render() interpolates nested messages",
            ui_text.render("msg.model.withNote", {"message": inner, "note": inner})
            == "已下載\n已下載")
    joined = ui_text.join(ui_text.msg("state.downloaded"), ui_text.msg("state.ready"), sep=" / ")
    r.check("join() builds a parts payload", ui_text.plain(joined) == "已下載 / 已就緒")
    r.check("unknown key never raises", ui_text.plain(ui_text.msg("no.such.key")) == "no.such.key")

    print("\n[9] Real backend APIs emit JSON-serialisable, registered keys")
    proc = subprocess.run([sys.executable, "-c", PROBE_SCRIPT, str(ROOT)],
                          capture_output=True, text=True, timeout=120)
    r.check("backend probe exits cleanly", proc.returncode == 0,
            proc.stderr.strip()[-200:])
    if proc.returncode == 0:
        probe = json.loads(proc.stdout)
        r.check("all 10 backend APIs serialise", not probe["errors"],
                str(probe["errors"][:2]))
        unknown = sorted(set(probe["keys"]) - set(ui_text.TEMPLATES))
        r.check("every emitted key is registered in ui_text.TEMPLATES",
                not unknown, str(unknown[:5]))
        r.check("backend emits structured keys at all", len(probe["keys"]) >= 20,
                f"{len(probe['keys'])} distinct keys")
        for label, key in (("hardware summary", probe["hardware_k"]),
                           ("device note", probe["device_note_k"]),
                           ("health group", probe["health_label_k"]),
                           ("accel recommendation", probe["accel_reason_k"]),
                           ("accel option card", probe["accel_option_k"]),
                           ("set_model feedback", probe["set_model_k"])):
            r.check(f"{label} is structured", key in ui_text.TEMPLATES, key)

    print("\n[7] No runtime prose bypasses the dictionary (Wave 7)")
    # The original complaint was that a Simplified / English UI still showed
    # Traditional Chinese. The dictionary was complete, but ~40 user-visible
    # strings were concatenated straight into textContent / innerHTML in
    # app.js, so those paths ignored the language entirely. Every Chinese
    # literal that reaches the DOM must now come through T()/I18N.
    app_js = (ROOT / "webview/js/app.js").read_text(encoding="utf-8")
    offenders = scan_hardcoded_zh(app_js)
    r.check("no hard-coded Chinese in DOM assignments", not offenders,
            "; ".join(f"L{n}: {s[:40]}" for n, s in offenders[:6]))
    # Every T("key", ...) must exist in the dictionary, otherwise the fallback
    # (Hant) silently leaks into the other two languages.
    used = set(re.findall(r'\bT\("([A-Za-z0-9_.]+)"', app_js))
    dict_keys = set(fe["dictKeys"])
    missing_keys = sorted(k for k in used if k not in dict_keys)
    r.check("every T() key is in the dictionary", not missing_keys, str(missing_keys[:6]))
    r.check("app.js routes UI text through T()", len(used) >= 60, f"{len(used)} keys")

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
