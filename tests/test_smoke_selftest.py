"""回归：tools/smoke_test.py 自己的判定逻辑。

为什么单独测它
--------------
冒烟测试是「发布包的最后一闸」。它自己判错，代价是两种：

  1. 假绿 —— 坏包被放行（2026-11-11 那次崩溃就这样发出去了）；
  2. 假红 —— 好包被判死，逼着人反复等 20 分钟的 Windows 构建，久而久之
     这道闸就会被当成噪音绕过去。

这两个坑都踩过：

  · 诊断脚本用了 `python -I`，隔离模式忽略 sys.path 注入，四个模块全部
    ModuleNotFoundError —— 诊断跑了等于没跑。
  · 判定靠「日志里有没有 No module named」—— 修好之后，只要日志里残留一
    【已处理的】旧告警就会假红。真正的判据应该看后果：端口有没有绑上、
    /health 有没有 200。

这里不去跑真 exe（Linux 上没有），只测这三件事：
  · DIAG 脚本能跑、能区分 OK/FAIL、能打印出异常类型
  · dump_log_errors 只挑异常行，不乱倒整篇日志
  · 页面里绝不能再用「日志文本」当通过条件

    python tests/test_smoke_selftest.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import smoke_test as st  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'通过' if ok else '失败'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def run_diag(tmp: Path) -> str:
    """用 DIAG 脚本对一个「什么都缺」的空目录跑一遍，检查输出的形态。"""
    code = st.DIAG.format(host=str(tmp), internal=str(tmp))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, cwd=str(tmp))
    return (p.stdout or "") + (p.stderr or "")


def main() -> int:
    r = Result()
    print("\n[冒烟测试自身] 诊断与判定逻辑")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        out = run_diag(tmp)

    # 1) 诊断必须真的把每个模块都试一遍，而不是一进 sys.path 就全挂。
    #    空目录里这些模块本来就不存在，所以应当全部 FAIL —— 关键是每个都
    #    有一条自己的结论行，说明循环跑完了。
    lines = [ln for ln in out.splitlines() if ln.startswith(("OK   ", "FAIL "))]
    want = ("app_paths", "webview_server", "webview_backend", "app", "ffmpeg_utils")
    got = {ln.split()[1] for ln in lines}
    r.check("诊断对每个模块都给出结论", set(want) <= got, f"{len(got)} 个：{sorted(got)}")
    r.check("缺模块时结论是 FAIL 而不是崩掉",
            all(ln.startswith("FAIL ") for ln in lines if ln.split()[1] in want),
            "; ".join(ln[:80] for ln in lines[:2]))
    # 2) 结论里要带异常类型 + 报错位置，否则看日志的人还得自己猜
    r.check("FAIL 行带异常类型", "Error" in out or "Exception" in out,
            next((ln for ln in lines if ln.startswith("FAIL ")), "")[:120])
    # 3) 诊断脚本不许再用 -I（隔离模式），否则上面的循环根本进不去
    src = (ROOT / "tools" / "smoke_test.py").read_text(encoding="utf-8")
    r.check("诊断不再用 python -I 隔离模式",
            not re.search(r'"-I"', src), "")

    # 4) dump_log_errors：只挑异常行
    sample = "\n".join([
        "[声音] http://127.0.0.1:1234",
        "[声音] 已載入模型",
        "Traceback (most recent call last):",
        '  File "x.py", line 1',
        "RuntimeError: boom",
        "No module named 'tkinter'",
        "[声音] 正常日志一行",
    ])
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        st.dump_log_errors(sample)
    dumped = buf.getvalue()
    r.check("异常行被列出", "RuntimeError" in dumped and "No module named" in dumped)
    r.check("正常行不被倒出来", "已載入模型" not in dumped and "正常日志一行" not in dumped)

    # 5) 判定只看后果：不许再把日志里有没有 "No module named" 当通过条件
    guard = [ln for ln in src.splitlines()
             if "No module named" in ln and ('check(' in ln or '"not in"' in ln)]
    r.check("不再用日志文本判定缺模块", not guard, "; ".join(guard[:2]))
    r.check("改成探测 /health", "/health" in src and "urlopen" in src)

    passed = sum(r.items)
    print(f"\n结果：{passed}/{len(r.items)} 项通过")
    print("✅ 全部通过" if r.ok else "❌ 存在失败项")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
