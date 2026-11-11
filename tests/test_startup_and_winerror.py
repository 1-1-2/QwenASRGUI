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
    # 消息里必须引用到收集到的 stderr（否则还是原来那句裸的退出码）
    if raised:
        msg_src = ast.dump(raised[0].exc)
        c.check("异常消息带上 ffmpeg 原话",
                "reason" in msg_src or "stderr" in msg_src or "tail" in msg_src,
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
    print()
    print(f"{c.total - c.failed}/{c.total} 通过")
    return 1 if c.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
