"""首啟閃退 / WinError 5 三处根因的回归测试。

对应 2026-11 使用者回报：
  1. 第一次打开闪退，但 settings.json 已生成；第二次启动成功。
  2. 切分预检与正式转换都报 `[WinError 5] 拒绝访问`，没有任何上下文。

三条结论各自锁定一处代码事实，避免以后被"改动顺手"改回去：
  · 首启种子写的量化值必须与全模块读取时的缺省值一致，
    否则「下载 q4 → 检查 q8 → 再下一遍 q8」，而模型没到位时自动加载
    路径会在启动当下拉起一次数百 MB 的下载。
  · ffmpeg 的 stderr 不能再丢进 DEVNULL：Windows 的错误文本只在那里，
    丢掉就只剩一句没有主词的 `[WinError 5] 拒绝访问`。
  · 临时目录清理不能 `ignore_errors=True`：子程序仍持有句柄时 rmtree 会
    失败，静默吞掉就等于每跑一次留一份音频副本在 %TEMP%。

为何用 AST 而不是搜源码文本：这些断言的关键词（DEVNULL、ignore_errors）
恰好也出现在解释"为什么不能这么写"的注释里，纯文本匹配会同时误报和漏报。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Checker:
    def __init__(self):
        self.failed = 0
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  ok   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name}" + (f"  ({detail})" if detail else ""))


def _tree(rel: str) -> ast.Module:
    src = (ROOT / rel).read_text(encoding="utf-8")
    # 只解析代码：注释本来就不进 AST，正好符合我们要的口径
    return ast.parse(src)


def _funcs(tree: ast.Module) -> dict:
    """按名字收集函数（含类方法 / 嵌套定义，`_cleanup_upload` 就在 handler 类里）。"""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, n)
    return out


def _const_assigns(tree: ast.Module) -> dict:
    """模块级常量赋值（含 class 内的不算，常量都在模块级）。"""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n.value
    return out


BL = _tree("webview_backend.py")
RN = _tree("longform_runner.py")
SV = _tree("webview_server.py")
DL = _tree("downloader.py")


def _const_str(tree, name):
    """取模块级 `NAME = "literal"` 的值。"""
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    if isinstance(n.value, ast.Constant):
                        return n.value.value
    return None


# ─────────────────────────────────────────────────────────────
# 1. 量化缺省值一致性
# ─────────────────────────────────────────────────────────────
def _seed_quant_arg(tree) -> str | None:
    """_seed_defaults() 里 cur.setdefault("crisp_qwen_quant", X) 的 X 值。

    X 可以是字面量，也可以引用模块常量（推荐：读取侧引同一个常量）。
    """
    fn = _funcs(tree).get("_seed_defaults")
    if not fn:
        return None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "crisp_qwen_quant"):
            val = node.args[1]
            if isinstance(val, ast.Constant):
                return val.value
            if isinstance(val, ast.Name):
                return _const_str(tree, val.id)
    return None


def _reader_defaults(tree) -> list[tuple[int, str]]:
    """所有 `.get("crisp_qwen_quant", <缺省>)` 的缺省值（行号, 值）。"""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "crisp_qwen_quant"):
            val = node.args[1]
            if isinstance(val, ast.Constant):
                out.append((node.lineno, val.value))
            elif isinstance(val, ast.Name):
                out.append((node.lineno, _const_str(tree, val.id)))
    return out


def test_quant_default_consistency(c: Checker):
    """种子值与所有「读取时缺省」必须同一个量化。"""
    seeded = _seed_quant_arg(BL)
    c.check("_seed_defaults 有写 crisp_qwen_quant", seeded is not None, str(seeded))
    if seeded is None:
        return

    readers = _reader_defaults(BL)
    c.check("能枚举读取侧缺省值", len(readers) >= 4, f"只找到 {len(readers)} 处")
    bad = [(ln, v) for ln, v in readers if v != seeded]
    c.check(
        "种子量化 == 全部读取缺省（否则下完一个又去找另一个）",
        not bad,
        f"种子={seeded}，读取侧不一致：{bad}",
    )


def test_seeded_quant_is_a_real_variant(c: Checker):
    """种子值必须在 downloader 的档名表里，不能是笔误。"""
    keys = set()
    for n in DL.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "_QWEN3_ASR_GGUF_FILES":
                    keys = {k.value for k in n.value.keys}
    seeded = _seed_quant_arg(BL)
    c.check("种子量化在档名表内", seeded in keys, f"{seeded} 不在 {sorted(keys)}")


# ─────────────────────────────────────────────────────────────
# 2. ffmpeg stderr 必须留下
# ─────────────────────────────────────────────────────────────
def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def test_ffmpeg_stderr_kept(c: Checker):
    fn = _funcs(RN).get("ffmpeg_pcm_stream")
    c.check("找到 ffmpeg_pcm_stream", fn is not None)
    if fn is None:
        return

    popen_calls = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "Popen"]
    c.check("ffmpeg 以 Popen 启动", bool(popen_calls))
    if not popen_calls:
        return

    # kw dict 里写 stderr=PIPE，或者 Popen(stderr=PIPE)
    devnull, pipe = False, False
    for call in popen_calls:
        for kw in call.keywords:
            if kw.arg == "stderr":
                code = ast.dump(kw.value)
                devnull |= "DEVNULL" in code
                pipe |= "PIPE" in code
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "kw":
                    if isinstance(node.value, ast.Call):
                        for kw in node.value.keywords:
                            if kw.arg == "stderr":
                                code = ast.dump(kw.value)
                                devnull |= "DEVNULL" in code
                                pipe |= "PIPE" in code

    c.check("stderr 不指向 DEVNULL", not devnull)
    c.check("stderr 以管道收集", pipe)

    raised = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call)
              and isinstance(n.exc.func, ast.Name)
              and n.exc.func.id == "RuntimeError"]
    c.check("失败时有 RuntimeError", bool(raised))
    # 消息里必须引用到收集到的 stderr（否则还是原来那句裸的退出码）。
    # 这里的 raise 有两种形态：
    #   1. Popen 失败（except OSError）→ 消息含 ff_path / cmd 等诊断上下文
    #   2. ffmpeg 退出码非 0 → 消息含 _ffmpeg_reason(tail, path)
    # 两者都必须能告诉使用者「到底哪里出问题」，不能只是「退出码 N」。
    if raised:
        msg_src = ast.dump(raised[0].exc)
        has_context = any(k in msg_src for k in (
            "reason", "stderr", "tail", "ff_path", "ff_abs", "cmd"))
        c.check("异常消息带上 ffmpeg 原话或诊断上下文", has_context,
                msg_src[:80])

    # 必须有抽干 stderr 的线程：管道写满会把 ffmpeg 堵死
    thr = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute)
           and n.func.attr == "Thread"]
    c.check("stderr 由独立线程持续抽干", bool(thr))


def test_ffmpeg_reason_helper(c: Checker):
    """_ffmpeg_reason 能把 Windows 原文翻成一句可动手的话。"""
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        from longform_runner import _ffmpeg_reason
    except Exception as exc:
        c.check("可导入 _ffmpeg_reason", False, repr(exc))
        return
    from pathlib import Path as _P
    got = _ffmpeg_reason(
        ["[in#0 @ 0000021c] Error opening input: Permission denied",
         "Error opening input file 002.m4a."], _P("002.m4a"))
    c.check("保留 ffmpeg 原文", "Permission denied" in got)
    c.check("附上可读原因", "占用" in got or "權限" in got or "权限" in got, got[:60])
    c.check("空输出也有兜底", _ffmpeg_reason([], _P("a.wav")).strip() != "")


# ─────────────────────────────────────────────────────────────
# 3. 临时目录清理不能静默
# ─────────────────────────────────────────────────────────────
def test_cleanup_not_silent(c: Checker):
    fn = _funcs(SV).get("_cleanup_upload")
    c.check("找到 _cleanup_upload", fn is not None)
    if fn is None:
        return

    ignore = False
    for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "rmtree"]:
        v = _kw(call, "ignore_errors")
        if isinstance(v, ast.Constant) and v.value is True:
            ignore = True
    c.check("rmtree 不再 ignore_errors=True", not ignore,
            "静默失败 = 每跑一次留一份音频在 %TEMP%")

    c.check("清理失败会退让重试",
            any(isinstance(n, ast.For) for n in ast.walk(fn))
            and any(isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "sleep" for n in ast.walk(fn)))

    told = any(isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr in ("publish", "add", "warning")
               for n in ast.walk(fn))
    c.check("仍失败会告知使用者（不无声）", told)


def test_main_keeps_survivor_free(c: Checker):
    """转写/预检的 finally 仍必须调用清理（别在改代码时漏掉）。"""
    for name in ("_transcribe", "_vad_preview"):
        fn = _funcs(SV).get(name)
        ok = bool(fn) and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_cleanup_upload" for n in ast.walk(fn))
        c.check(f"{name} 仍会清理临时目录", ok)


# ─────────────────────────────────────────────────────────────
# 4. 使用说明里的日志路径必须是真实路径
# ─────────────────────────────────────────────────────────────
def test_readme_log_path_matches_code(c: Checker):
    """使用说明告诉使用者去哪看闪退日志 —— 那个路径必须真的存在。

    之前这里写的是 `%LOCALAPPDATA%\\QwenASR-WebView\\launcher.log`，而
    `app_webview._log_dir()` 实际用的是 `APP_NAME`（聲音辨識小工具）。
    于是使用者按说明去翻日志，翻到的是空目录，闪退就彻底「无痕」了。
    """
    import re as _re
    app = (ROOT / "app_webview.py").read_text(encoding="utf-8")
    m = _re.search(r'^APP_NAME\s*=\s*["\'](.+?)["\']', app, re.M)
    c.check("能读到 APP_NAME", m is not None)
    if not m:
        return
    app_name = m.group(1)

    br = (ROOT / "build_release.py").read_text(encoding="utf-8")
    # 找说明里出现的 %LOCALAPPDATA%\<名字>\launcher.log
    claimed = set(_re.findall(
        r"%LOCALAPPDATA%\\\\?([^\\/\s]+)\\\\?launcher\.log", br))
    c.check("使用说明有写日志路径", bool(claimed), str(claimed))
    c.check(
        "说明里的目录名 == APP_NAME",
        claimed == {app_name},
        f"说明写 {sorted(claimed)}，程序实际用 {app_name}",
    )
    c.check("说明不再提已废弃的 QwenASR-WebView 数据目录",
            "%LOCALAPPDATA%\\\\QwenASR-WebView" not in br)


# ─────────────────────────────────────────────────────────────
# 5. 升級不得讓老使用者白下載一次模型
# ─────────────────────────────────────────────────────────────
def test_adopt_existing_quant(c: Checker):
    """settings 沒寫量化時，必須沿用磁碟上已有的那份。

    背景：缺省值從 q8 改成 q4 之後，一個早就下好 q8 的老使用者會被判成
    「模型不就緒」，然後在啟動當下開始下載另一個 q4 —— 明明手邊那份能用。
    `_adopt_existing_quant()` 就是為了堵這件事。
    """
    import json
    import sys
    import tempfile

    sys.path.insert(0, str(ROOT))
    import webview_backend as wb

    def run(settings, files):
        td = Path(tempfile.mkdtemp())
        md = td / "ov_models"
        md.mkdir()
        sf = td / "settings.json"
        sf.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        for n in files:
            (md / n).write_bytes(b"x" * 5000)      # 够大，绕过 LFS pointer 判定
        old = (wb.core.SETTINGS_FILE, getattr(wb.core, "_DEFAULT_MODEL_DIR", None))
        wb.core.SETTINGS_FILE = sf
        wb.core._DEFAULT_MODEL_DIR = md
        try:
            class Fake(wb.WebBackend):
                def __init__(self):
                    pass
            Fake()._adopt_existing_quant()
            return json.loads(sf.read_text(encoding="utf-8")).get("crisp_qwen_quant")
        finally:
            wb.core.SETTINGS_FILE, wb.core._DEFAULT_MODEL_DIR = old

    base = {"backend": "crispasr", "crisp_model": "qwen3"}
    q8f, q4f = "qwen3-asr-1.7b-q8_0.gguf", "qwen3-asr-1.7b-q4_k.gguf"

    c.check("已有 q8 → 沿用 q8（不重下）", run(base, [q8f]) == "q8")
    c.check("已有 q4 → 沿用 q4", run(base, [q4f]) == "q4")
    c.check("都没有 → 不写（交给缺省）", run(base, []) is None)
    c.check("两个都有 → 优先 q8（旧缺省，覆盖面更大）",
            run(base, [q8f, q4f]) == "q8")
    c.check("显式 q4 不被磁盘上的 q8 覆盖",
            run({**base, "crisp_qwen_quant": "q4"}, [q8f]) == "q4")
    c.check("显式 q8 不被磁盘上的 q4 覆盖",
            run({**base, "crisp_qwen_quant": "q8"}, [q4f]) == "q8")
    c.check("非 crispasr 后端不动它", run({"backend": "openvino"}, [q8f]) is None)
    c.check("breeze 模型不动它（它用 crisp_quant）",
            run({**base, "crisp_model": "breeze"}, [q8f]) is None)


def test_adopt_is_wired(c: Checker):
    """迁移必须在 __init__ 里被调用，否则等于没写。"""
    fn = _funcs(BL).get("__init__")
    called = bool(fn) and any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_adopt_existing_quant" for n in ast.walk(fn))
    c.check("__init__ 调用 _adopt_existing_quant", called)


# ─────────────────────────────────────────────────────────────
# 6. launcher.log 必须能分清「哪一行属于哪一次启动」
# ─────────────────────────────────────────────────────────────
def test_log_lines_carry_timestamps(c: Checker):
    """每行盖时间戳：单文件 append 模式下，三次启动的日志首尾相接。"""
    import sys
    import tempfile

    sys.path.insert(0, str(ROOT))
    import app_webview as aw

    d = Path(tempfile.mkdtemp())
    f = aw._LogFanout(d, "20260101-120000")
    f.write("===== 啟動 PID 123 =====\n")
    f.write("第一行\n")
    f.write("多行\n第二行\n")
    f.write("半行")
    f.write("補完\n")

    lines = [ln for ln in (d / "launcher.log").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    c.check("写了 5 行", len(lines) == 5, str(lines))
    stamp = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] ")
    c.check("每行都以时间戳开头", all(stamp.match(ln) for ln in lines), str(lines[:2]))
    c.check("半行拼接后只保留一个时间戳", lines[-1].count("] ") == 1, lines[-1])
    f2 = aw._LogFanout(d, "20260101-120001")
    f2.write("A\n\nB\n")
    body = (d / f"launcher-20260101-120001-p{__import__('os').getpid()}.log"
            ).read_text(encoding="utf-8").splitlines()
    c.check("空行不盖时间戳（不刷屏）", body[1] == "", repr(body))


def test_log_snapshot_per_run(c: Checker):
    """每次运行一份带时间戳的快照，且只保留最近 N 份。"""
    import sys
    import tempfile

    sys.path.insert(0, str(ROOT))
    import app_webview as aw

    d = Path(tempfile.mkdtemp())
    for i in range(15):
        aw._LogFanout(d, f"20260101-1200{i:02d}").write(f"第 {i} 次\n")

    snaps = sorted(p.name for p in d.glob("launcher-*.log"))
    c.check("只保留最近 12 份（不无限增长）", len(snaps) == 12, str(len(snaps)))
    c.check("快照名内含完整启动时间与 PID",
            all(re.fullmatch(r"launcher-\d{8}-\d{6}-p\d+\.log", n) for n in snaps),
            str(snaps[:2]))
    c.check("清掉的是最旧的那几份",
            "20260101-120014" in snaps[-1] and "20260101-120000" not in snaps)
    c.check("最新快照内容完整",
            "第 14 次" in (d / snaps[-1]).read_text(encoding="utf-8"))


def test_log_rotation_keeps_previous(c: Checker):
    """超 1MB 时轮换成 .1，不再直接 unlink —— 上一次崩潰往往正是要对照的。"""
    import sys
    import tempfile

    sys.path.insert(0, str(ROOT))
    import app_webview as aw

    d = Path(tempfile.mkdtemp())
    (d / "launcher.log").write_bytes(b"x" * 1_048_577)
    aw._LogFanout(d, "20260101-130000").write("轮换后\n")
    c.check("旧内容保留为 launcher.log.1", (d / "launcher.log.1").exists())
    c.check("新文件从这次启动开始",
            (d / "launcher.log").stat().st_size < 300,
            str((d / "launcher.log").stat().st_size))


def test_log_writer_wired(c: Checker):
    """hush_stdio 必须建 fanout 并写启动横幅，否则使用者还是分不清哪次。"""
    tree = _tree("app_webview.py")
    fns = _funcs(tree)
    hush = fns.get("hush_stdio")
    c.check("找到 hush_stdio", hush is not None)
    if not hush:
        return
    calls = {n.func.id for n in ast.walk(hush)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    c.check("hush_stdio 建立了 _LogFanout", "_LogFanout" in calls, str(calls))
    src = ast.get_source_segment((ROOT / "app_webview.py").read_text(encoding="utf-8"), hush) or ""
    c.check("写入启动横幅（含 PID）", "PID" in src and "啟動" in src)






def test_vad_preview_gives_actionable_error(c: Checker):
    """模型未載入（vad_sess=None）時，錯誤訊息要告訴使用者下一步做什麼。

    背景：使用者在模型未下載時點「切分預檢」，拿到的是
    `ValueError: vad_sess is required for VAD preview` —— 完全看不出要幹嘛。
    現在改成明確提示「請先到模型頁下載並載入模型」。
    """
    import sys
    import tempfile
    sys.path.insert(0, str(ROOT))
    import webview_backend as wb

    td = Path(tempfile.mkdtemp())
    old = (wb.core.SETTINGS_FILE, getattr(wb.core, "_DEFAULT_MODEL_DIR", None))
    wb.core.SETTINGS_FILE = td / "settings.json"
    wb.core._DEFAULT_MODEL_DIR = td / "ov_models"
    (td / "ov_models").mkdir(parents=True, exist_ok=True)
    (td / "settings.json").write_text(
        '{"backend":"crispasr","crisp_model":"qwen3","crisp_qwen_quant":"q4"}',
        encoding="utf-8")
    try:
        b = wb.WebBackend()
        b.engine = wb.core.ASREngine()   # 空殼：vad_sess=None
        f = td / "fake.m4a"
        f.write_bytes(b"x" * 100)
        try:
            b.vad_preview({"path": str(f)})
            c.check("vad_preview 在 vad_sess=None 時應拋例外", False)
        except Exception as e:
            msg = str(e)
            c.check("錯誤訊息含「模型尚未載入」", "模型尚未載入" in msg, msg[:60])
            c.check("錯誤訊息含「下載」動作指引", "下載" in msg, msg[:60])
            c.check("錯誤不是 ValueError（內部技術詞）",
                    not isinstance(e, ValueError), type(e).__name__)
    finally:
        wb.core.SETTINGS_FILE, wb.core._DEFAULT_MODEL_DIR = old


def main() -> int:
    c = Checker()
    print("== 首启量化一致性 ==")
    test_quant_default_consistency(c)
    test_seeded_quant_is_a_real_variant(c)
    print("== ffmpeg 错误文本 ==")
    test_ffmpeg_stderr_kept(c)
    test_ffmpeg_reason_helper(c)
    print("== 临时目录清理 ==")
    test_cleanup_not_silent(c)
    test_main_keeps_survivor_free(c)
    print("== 使用说明指向的日志路径 ==")
    test_readme_log_path_matches_code(c)
    print("== 升级不害老用户重下模型 ==")
    test_adopt_existing_quant(c)
    test_adopt_is_wired(c)
    print("== 预检错误是否可操作 ==")
    test_vad_preview_gives_actionable_error(c)
    print("== 日志能否分清哪次启动 ==")
    test_log_lines_carry_timestamps(c)
    test_log_snapshot_per_run(c)
    test_log_rotation_keeps_previous(c)
    test_log_writer_wired(c)
    print()
    print(f"{c.total - c.failed}/{c.total} 通过")
    return 1 if c.failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
