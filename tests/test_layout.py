"""Wave 3 / G2 regression: layered (onedir) path resolution.

The onefile -> onedir switch moves every bundled file from a temp extraction
directory into ``_internal/``. Anything that used to derive the application
directory from ``__file__`` would silently start reading *and writing* inside
``_internal/`` - settings, models and checkpoints would then vanish on the
next update. This suite pins that behaviour down for all three layouts:

    development   source tree
    onefile       launcher + sys._MEIPASS pointing at a temp dir
    onedir        launcher + _internal/ (sys._MEIPASS points at _internal/)

It builds real directory trees in a temp dir and reloads ``app_paths`` against
a faked ``sys.frozen`` / ``sys._MEIPASS``, so no packaging and no Windows are
needed.

    python tests/test_layout.py
"""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import app_paths  # noqa: E402


def _install_fake_core():
    """Provide a stand-in ``app`` module before importing webview_backend.

    ``webview_backend`` (and therefore ``webview_server``) does
    ``import app as core`` for the ASR engine, and ``app.py`` imports Tk at
    module level. This suite only exercises path resolution, so a minimal core
    exposing the same path surface keeps it free of GUI dependencies - the same
    approach tests/test_integration.py already uses.
    """
    import types
    if "app" in sys.modules:
        return
    fake = types.ModuleType("app")
    fake.BASE_DIR = app_paths.app_dir()
    fake.resource_path = app_paths.resource_path
    fake.user_path = app_paths.user_path
    fake.SETTINGS_FILE = app_paths.user_path("settings.json")
    fake.SRT_DIR = app_paths.user_path("subtitles")
    fake._DEFAULT_MODEL_DIR = app_paths.user_path("ov_models")
    fake._CHATLLM_DIR = app_paths.user_path("chatllm")
    fake._BIN_PATH = app_paths.user_path("GPUModel", "qwen3-asr-1.7b.bin")
    fake.ASREngine = object
    sys.modules["app"] = fake


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


class Layout:
    """Temporarily fake a frozen layout and reload app_paths against it."""

    def __init__(self, root: Path, meipass: Path | None, exe: Path | None):
        self.root, self.meipass, self.exe = root, meipass, exe
        self._saved = {}

    def __enter__(self):
        self._saved = {
            k: getattr(sys, k) for k in ("frozen", "_MEIPASS", "executable") if hasattr(sys, k)
        }
        if self.exe is None:
            for k in ("frozen", "_MEIPASS"):
                if hasattr(sys, k):
                    delattr(sys, k)
        else:
            sys.frozen = True
            sys.executable = str(self.exe)
            if self.meipass is not None:
                sys._MEIPASS = str(self.meipass)
            elif hasattr(sys, "_MEIPASS"):
                del sys._MEIPASS
        return importlib.reload(app_paths)

    def __exit__(self, *exc):
        for k in ("frozen", "_MEIPASS", "executable"):
            if k in self._saved:
                setattr(sys, k, self._saved[k])
            elif hasattr(sys, k):
                delattr(sys, k)
        importlib.reload(app_paths)
        return False


def main() -> int:
    r = Result()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # ── 1. development: everything relative to the source tree ──────────
        print("\n[G2] development layout")
        with Layout(tmp / "src", None, None) as ap:
            r.check("not frozen", ap.is_frozen() is False)
            r.check("app_dir is the source tree", ap.app_dir() == ROOT,
                    str(ap.app_dir()))
            r.check("resource_root is the source tree", ap.resource_root() == ROOT)
            r.check("models.json resolves", (ap.resource_path("models.json")).is_file())
            r.check("no _internal in the resource path",
                    "_internal" not in str(ap.resource_path("models.json")))

        # ── 2. onedir: launcher on top, payload under _internal/ ────────────
        print("\n[G2] onedir layout")
        pkg = tmp / "onedir" / "QwenASR-WebView"
        exe = _touch(pkg / "QwenASR-WebView.exe")
        internal = pkg / "_internal"
        _touch(internal / "models.json")
        _touch(internal / "prompt_template.json")
        _touch(internal / "ov_models" / "mel_filters.npy")
        _touch(internal / "ov_models" / "silero_vad_v4.onnx")
        _touch(internal / "webview_assets" / "index.html")
        _touch(internal / "app_paths.py")
        # user data that a previous version left beside the launcher
        _touch(pkg / "settings.json")

        with Layout(tmp / "src", internal, exe) as ap:
            r.check("frozen detected", ap.is_frozen() is True)
            r.check("user data stays beside the launcher", ap.app_dir() == pkg,
                    str(ap.app_dir()))
            r.check("user data is NOT inside _internal",
                    "_internal" not in str(ap.user_path("settings.json")))
            r.check("models.json found under _internal",
                    ap.resource_path("models.json") == internal / "models.json")
            r.check("prompt_template.json found under _internal",
                    ap.resource_path("prompt_template.json").is_file())
            r.check("mel_filters.npy found under _internal",
                    ap.resource_path("ov_models", "mel_filters.npy").is_file())
            r.check("silero VAD found under _internal",
                    ap.resource_path("ov_models", "silero_vad_v4.onnx").is_file())
            r.check("front-end assets found under _internal",
                    ap.resource_path("webview_assets", "index.html").is_file())

            # Cross-module behaviour: the real resolvers, not just app_paths.
            _install_fake_core()
            import webview_server
            web_dir = webview_server.resolve_web_dir()
            r.check("webview_server resolves _internal/webview_assets",
                    (web_dir / "index.html").is_file() and web_dir.name == "webview_assets",
                    str(web_dir))

            import model_catalog
            cat = model_catalog.catalog_path()
            r.check("model_catalog resolves _internal/models.json",
                    cat == internal / "models.json", str(cat))

            # A catalog beside the launcher is the user-repair path and must
            # win over the bundled copy (also how pre-G2 packages were laid out).
            _touch(pkg / "models.json")
            r.check("catalog beside the launcher overrides the bundled one",
                    model_catalog.catalog_path() == pkg / "models.json",
                    str(model_catalog.catalog_path()))
            (pkg / "models.json").unlink()
            r.check("bundled catalog is used again once the override is gone",
                    model_catalog.catalog_path() == internal / "models.json")

            # ffmpeg_utils imports customtkinter for its desktop progress
            # dialog only; the path helpers are what matters here.
            import types
            if "customtkinter" not in sys.modules:
                ctk = types.ModuleType("customtkinter")

                class _Widget:
                    def __init__(self, *a, **k):
                        pass

                for n in ("CTkToplevel", "CTkLabel", "CTkProgressBar",
                          "CTkButton", "CTkFrame"):
                    setattr(ctk, n, _Widget)
                sys.modules["customtkinter"] = ctk
            import ffmpeg_utils
            ff_dir = ffmpeg_utils.get_default_ffmpeg_dest()
            r.check("ffmpeg download target is beside the launcher", ff_dir == pkg / "ffmpeg",
                    str(ff_dir))

            import updater
            r.check("updater install_dir is beside the launcher",
                    updater.install_dir() == pkg, str(updater.install_dir()))

            # A legacy package that kept a resource beside the launcher must
            # still work (pre-G2 ZIPs and hand-edited overrides).
            _touch(pkg / "override_catalog.json")
            r.check("resource beside the launcher still wins",
                    ap.resource_path("override_catalog.json") == pkg / "override_catalog.json")

        # ── 3. onefile: launcher + temp extraction dir ──────────────────────
        print("\n[G2] onefile layout (kept working, not used by the release)")
        one = tmp / "onefile"
        one_exe = _touch(one / "QwenASR-WebView.exe")
        temp_extract = tmp / "_MEI12345"
        _touch(temp_extract / "models.json")
        _touch(temp_extract / "webview_assets" / "index.html")
        with Layout(tmp / "src", temp_extract, one_exe) as ap:
            r.check("user data beside the launcher, not in the temp dir",
                    ap.app_dir() == one, str(ap.app_dir()))
            r.check("resource_root is the temp dir", ap.resource_root() == temp_extract)
            r.check("models.json found in the temp dir",
                    ap.resource_path("models.json") == temp_extract / "models.json")

        # ── 4. defensive: frozen but no _MEIPASS ────────────────────────────
        print("\n[G2] frozen without _MEIPASS")
        bare = tmp / "bare"
        bare_exe = _touch(bare / "QwenASR-WebView.exe")
        with Layout(tmp / "src", None, bare_exe) as ap:
            r.check("app_dir falls back to the launcher dir", ap.app_dir() == bare)
            r.check("resource_root falls back to the launcher dir",
                    ap.resource_root() == bare)
            r.check("no crash when the resource is absent",
                    isinstance(ap.resource_path("models.json"), Path))

        # ── 5. the release script and the packaged layout must agree ────────
        print("\n[G2] build script matches the resolved layout")
        br = (ROOT / "build_release.py").read_text(encoding="utf-8")
        required = br.split("REQUIRED_FILES = [")[1].split("]")[0]
        for rel in ("_internal/models.json", "_internal/webview_assets/index.html",
                    "_internal/ov_models/silero_vad_v4.onnx",
                    "_internal/longform_runner.py"):
            r.check(f"required file listed: {rel}", rel in required)
        r.check("launcher itself is required", 'f"{APP_NAME}.exe"' in required,
                required.strip().splitlines()[0].strip())
        r.check("static data still lands in the resource root",
                '("webview", "webview_assets")' in br and '("models.json", ".")' in br)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
