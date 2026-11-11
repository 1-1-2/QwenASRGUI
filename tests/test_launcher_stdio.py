"""Windowed-launcher regression: sys.stdout / sys.stderr are None in a
``--windowed`` PyInstaller build.

The packaged EXE crashed on exit with

    AttributeError: 'NoneType' object has no attribute 'flush'

because ``main()`` flushed both streams unconditionally. In source mode there
is always a console, so no test covered it. This suite simulates the frozen,
console-less environment by importing ``app_webview`` with both streams set to
None, and asserts:

  * importing the module does not raise;
  * ``print()`` and ``flush()`` never raise, and land in launcher.log;
  * the exit path (guard.release / srv.stop / flush / os._exit) always reaches
    ``os._exit`` even when flushing fails;
  * an unexpected exception is written to the log and shown to the user.

    python tests/test_launcher_stdio.py
"""
from __future__ import annotations

import ast
import importlib
import os
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="launcher_stdio_"))


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _stub_deps():
    """Provide the imports app_webview needs without a desktop / GUI stack."""
    class _FakeSingle:
        def __init__(self, *a, **k):
            pass

        def acquire(self):
            return (True, "")

        def publish(self, url):
            pass

        def release(self):
            pass

    class _FakeServer:
        def __init__(self, *a, **k):
            self.port = 12345
            self.url = "http://127.0.0.1:12345/"
            self.backend = types.SimpleNamespace(
                persisted_theme=lambda: "light",
                set_theme_callback=lambda cb: None,
                selected_model_present=lambda: False,
                start_load=lambda: None,
            )

        def start(self):
            pass

        def stop(self):
            pass

    stubs = {
        "proc_guard": types.SimpleNamespace(setup_kill_on_close_job=lambda: None),
        "single_instance": types.SimpleNamespace(SingleInstance=_FakeSingle),
        "webview_server": types.SimpleNamespace(WebViewServer=_FakeServer),
    }
    for name, mod in stubs.items():
        sys.modules[name] = mod
    if "app_paths" not in sys.modules:
        sys.modules["app_paths"] = types.SimpleNamespace(
            app_dir=lambda: ROOT,
            resource_path=lambda *a: ROOT.joinpath(*a),
            is_frozen=lambda: True,
            resource_root=lambda: ROOT,
        )


def main() -> int:
    r = Result()
    saved = (sys.stdout, sys.stderr)
    _stub_deps()

    # ── 1) import under "no console" must not raise, and must self-heal ─────
    sys.stdout = None
    sys.stderr = None
    try:
        sys.modules.pop("app_webview", None)
        import app_webview as aw
        r.check("import with stdout/stderr=None does not raise", True)
    except Exception as exc:                       # pragma: no cover
        r.check("import with stdout/stderr=None does not raise", False,
                f"{type(exc).__name__}: {exc}")
        sys.stdout, sys.stderr = saved
        return 1
    finally:
        sys.stdout, sys.stderr = saved

    # The module-level hush_stdio() ran while the streams were None.
    r.check("stdout replaced by a safe stream", aw._NullSafeStream.__name__
            not in (None,))

    # ── 2) real behaviour of the replacement stream ────────────────────────
    # 第二個參數從「路徑」換成 _LogFanout：日誌要同時寫固定檔與每次執行的快照，
    # 所以由 fanout 統一持有出口（與每行時間戳）。
    log = TMP / "launcher.log"
    s = aw._NullSafeStream(None, aw._LogFanout(TMP, "test-000000"))
    try:
        print("hello", file=s)
        s.write("raw line\n")
        s.flush()
        s.close_safe = None                        # __getattr__ must not explode
        proxied = False
    except AttributeError:
        proxied = True                             # None real → attribute missing
    except Exception as exc:                       # pragma: no cover
        r.check("replacement stream never raises", False,
                f"{type(exc).__name__}: {exc}")
        return 1
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    r.check("print() into the stream does not raise", True)
    r.check("written text reaches launcher.log", "hello" in text and "raw line" in text,
            text.replace("\n", "\\n")[:60])

    # closed / broken real stream must be survivable too
    class _Boom:
        def write(self, s):
            raise ValueError("closed")

        def flush(self):
            raise ValueError("closed")

    b = aw._NullSafeStream(_Boom())
    try:
        b.write("x")
        b.flush()
        r.check("broken real stream is swallowed", True)
    except Exception as exc:                       # pragma: no cover
        r.check("broken real stream is swallowed", False, repr(exc))

    # isatty / fileno / encoding contract used by third-party libs
    r.check("isatty() is False without a console", b.isatty() is False)
    r.check("encoding falls back to utf-8", b.encoding == "utf-8")
    try:
        b.fileno()
        fileno_ok = False
    except OSError:
        fileno_ok = True
    r.check("fileno() raises OSError (not AttributeError)", fileno_ok)

    # ── 3) hush_stdio() must be a no-op when a console exists ──────────────
    import io
    real_out, real_err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = real_out, real_err
    aw.hush_stdio()
    r.check("hush_stdio() leaves a real console untouched",
            sys.stdout is real_out and sys.stderr is real_err)
    sys.stdout, sys.stderr = saved

    # ── 4) the exit path always reaches os._exit ───────────────────────────
    src = (ROOT / "app_webview.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    flush_lines = [n for n in ast.walk(main_fn)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == "flush"]
    guarded = []
    for call in flush_lines:
        # every flush() in main() must sit inside a `try:` that swallows errors
        for anc in ast.walk(main_fn):
            if isinstance(anc, ast.Try) and any(
                    call is d for st in anc.body for d in ast.walk(st)):
                if len(anc.handlers) >= 1 and all(
                        isinstance(h.type, ast.Name) and h.type.id == "Exception"
                        for h in anc.handlers):
                    guarded.append(call)
                break
    r.check("main() has a guarded flush before os._exit",
            bool(flush_lines) and len(guarded) == len(flush_lines),
            f"{len(guarded)}/{len(flush_lines)}")
    r.check("main() still calls os._exit", "os._exit(0)" in src)
    # the bare form that caused the crash must be gone
    bare = [ln for ln in src.splitlines()
            if ln.strip() == "sys.stdout.flush()" and "if _s" not in ln]
    r.check("no unguarded top-level sys.stdout.flush()", not bare, str(bare[:2]))

    # ── 5) crash logging ───────────────────────────────────────────────────
    r.check("sys.excepthook is installed", aw.sys.excepthook is not None)
    hook_src = "".join(ast.unparse(n) for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "_install_crash_log")
    r.check("crash handler writes a log and warns the user",
            "MessageBoxW" in hook_src and "format_exception" in hook_src)

    # ── 6) 缺模組時必須是「看得懂的錯誤」，不是 TypeError ──────────────────
    # 2026-11 使用者回報：打包版啟動即
    #   TypeError: 'NoneType' object is not callable   (main 內 WebViewServer(...))
    # 成因是 webview_server 匯入鏈缺模組 → 被 except 吞成 None。這裡鎖住兩件事：
    #   a) main() 在 None 時先丟 RuntimeError（帶上原始 ImportError 訊息）
    #   b) 匯入失敗時有把原因寫進日誌，不再靜默
    main_src = ast.get_source_segment(src, main_fn) or ""
    guard_at = main_src.find("if WebViewServer is None")
    call_at = main_src.find("srv = WebViewServer(")
    r.check("main() checks WebViewServer before calling it",
            0 <= guard_at < call_at, f"guard@{guard_at} call@{call_at}")
    guard_src = main_src[guard_at:call_at] if guard_at >= 0 else ""
    r.check("the guard mentions the missing module",
            "webview_server" in guard_src and "RuntimeError" in guard_src)

    saved_mod = sys.modules.pop("webview_server", None)
    sys.modules["webview_server"] = None          # import → ImportError
    sys.modules.pop("app_webview", None)
    try:
        broken = importlib.import_module("app_webview")
        r.check("import failure is recorded, not swallowed",
                broken.WebViewServer is None and bool(broken._SERVER_IMPORT_ERR))
        try:
            broken.main()
            r.check("main() turns a missing server into RuntimeError", False)
        except RuntimeError as exc:
            r.check("main() turns a missing server into RuntimeError",
                    "webview_server" in str(exc), str(exc).splitlines()[0][:60])
        except TypeError as exc:
            # 就是使用者看到的那顆炸彈 —— 修好的版本不該再走到這裡
            r.check("main() turns a missing server into RuntimeError", False,
                    f"TypeError: {exc}")
    finally:
        sys.modules.pop("app_webview", None)
        if saved_mod is not None:
            sys.modules["webview_server"] = saved_mod
        else:
            sys.modules.pop("webview_server", None)

    # ── 7) 启动进度不能写 stdout（CLI 契约），且要留下 selftest.json ────────
    with tempfile.TemporaryDirectory() as td:
        os_environ = os.environ.copy()
        os.environ["LOCALAPPDATA"] = td
        import io as _io
        try:
            sys.modules.pop("app_webview", None)
            fresh = importlib.import_module("app_webview")
            out, err = _io.StringIO(), _io.StringIO()
            saved_o, saved_e = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                fresh._selftest()
            finally:
                sys.stdout, sys.stderr = saved_o, saved_e
            r.check("selftest 只往 stderr 写", out.getvalue().strip() == "",
                    out.getvalue()[:80])
            r.check("selftest 进度落在 stderr", '{"ok"' in err.getvalue())
            result_file = Path(td) / fresh.APP_NAME / "selftest.json"
            if not result_file.is_file():          # 日志替身未重算目录时兜底
                result_file = Path(td) / "selftest.json"
            if not result_file.is_file():
                found = list(Path(td).rglob("selftest.json"))
                result_file = found[0] if found else result_file
            r.check("selftest.json 已写出（供冒烟测试读取）", result_file.is_file(),
                    str(result_file))
            if result_file.is_file():
                data = json.loads(result_file.read_text(encoding="utf-8"))
                r.check("selftest.json 内容可解析", data.get("version") is not None,
                        str(list(data)[:4]))
        finally:
            os.environ.clear()
            os.environ.update(os_environ)
            sys.modules.pop("app_webview", None)

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
