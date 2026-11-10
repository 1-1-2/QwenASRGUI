"""verify_package.py — 校验发行包完整性（简体中文）。

检查：
  1. PyInstaller 包内容验证结果里没有缺失的关键动态导入模块
  2. 构建清单包含 longform_runner.py（长音频流式模块）
  3. release_pkg/ 下恰好一个 ZIP
  4. EXE 体积合理（> 20MB，防止空壳构建被当成成功）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    pkg = ROOT / "release_pkg"
    v = json.loads((pkg / "archive_verify.json").read_text(encoding="utf-8"))
    print("包内容验证状态：", v["状态"])
    print("命中：", v["命中"])
    print("缺失：", v["缺失"])
    print("验证方式：", v.get("方式", "-"))
    if v["状态"] not in ("通过", "存在缺失"):
        print(f"错误：包内容验证未能执行：{v['状态']}")
        return 1
    if v["缺失"]:
        print(f"错误：EXE 内缺少关键模块：{v['缺失']}")
        return 1

    m = json.loads((pkg / "QwenASR-WebView" / "build_manifest.json")
                   .read_text(encoding="utf-8"))
    src = m.get("随附 Python 源码", [])
    print(f"随包 .py 共 {len(src)} 个")
    if "longform_runner.py" not in src:
        print("错误：构建清单缺少 longform_runner.py")
        return 1

    zips = sorted(pkg.glob("*.zip"))
    if len(zips) != 1:
        print(f"错误：期望 1 个 ZIP，实际 {len(zips)}")
        return 1
    exe = ROOT / "dist" / "webview" / "QwenASR-WebView.exe"
    if not exe.exists():
        print(f"错误：找不到 EXE：{exe}")
        return 1
    size = exe.stat().st_size
    if size < 20 * 1024 * 1024:
        print(f"错误：EXE 仅 {size/1024/1024:.1f} MB，疑似构建不完整")
        return 1

    print(f"ZIP：{zips[0].name}  {zips[0].stat().st_size/1024/1024:.1f} MB")
    print(f"EXE：{size/1024/1024:.1f} MB")
    print("校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
