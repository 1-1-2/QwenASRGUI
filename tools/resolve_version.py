"""resolve_version.py — 计算发行包版本标签，写入 GITHUB_OUTPUT（简体中文）。

独立脚本而非工作流内嵌 heredoc：Windows runner 的 Git Bash 对 heredoc
处理容易出兼容问题，独立文件在本地也能直接回归测试。

输出：
    version  版本标签，如 2.0.0-longform.90d04b2
    sha      提交短码
    base     version.py 中的基础版本（如 2.0.0）
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path



def _force_utf8():
    """Windows 控制台默认 cp1252 会让中文 print 抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def main() -> int:
    _force_utf8()
    root = Path(__file__).resolve().parent.parent
    src = (root / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', src, re.M)
    base = m.group(1) if m else "2.0.0"

    sha = (os.environ.get("GITHUB_SHA") or "local")[:7]
    suffix = (os.environ.get("INPUT_VERSION_SUFFIX") or "").strip()
    version = suffix or f"{base}-longform.{sha}"

    print(f"基础版本：{base}")
    print(f"提交短码：{sha}")
    print(f"版本标签：{version}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"version={version}\n")
            f.write(f"sha={sha}\n")
            f.write(f"base={base}\n")
        print(f"已写入 GITHUB_OUTPUT：{out}")
    else:
        print("未设置 GITHUB_OUTPUT，仅打印（本地调试）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
