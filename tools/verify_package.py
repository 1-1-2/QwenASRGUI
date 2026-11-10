"""verify_package.py — 校验发行包完整性（简体中文）。

检查：
  1. 分层校验结果（release_pkg/archive_verify.json）没有缺失条目
  2. 构建清单包含 longform_runner.py（长音频流式模块）
  3. release_pkg/ 下恰好一个 ZIP
  4. 分层产物结构正确：启动器在包根，依赖在 _internal/，且 _internal/ 体积合理
     （onefile 时代用「EXE > 20MB」防空壳；onedir 下启动器本身只有几 MB，
      体积门槛改看 _internal/）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent



def _force_utf8():
    """Windows 控制台默认 cp1252 会让中文 print 抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def main() -> int:
    _force_utf8()
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
    stage = pkg / "QwenASR-WebView"
    exe = stage / "QwenASR-WebView.exe"
    internal = stage / "_internal"
    if not exe.exists():
        print(f"错误：找不到启动器：{exe}")
        return 1
    if not internal.is_dir():
        print(f"错误：缺少分层目录：{internal}")
        return 1

    exe_size = exe.stat().st_size
    if exe_size < 1024 * 1024:
        print(f"错误：启动器仅 {exe_size/1024/1024:.1f} MB，疑似构建不完整")
        return 1
    internal_size = sum(p.stat().st_size for p in internal.rglob("*") if p.is_file())
    if internal_size < 150 * 1024 * 1024:
        print(f"错误：_internal/ 仅 {internal_size/1024/1024:.1f} MB，"
              f"疑似依赖未完整收集")
        return 1

    # User data must live beside the launcher, never inside _internal/.
    for d in ("ov_models", "subtitles", "longform_state"):
        if not (stage / d).is_dir():
            print(f"错误：缺少用户数据目录 {d}/")
            return 1
        if (internal / d).exists():
            print(f"错误：{d}/ 不应出现在 _internal/ 内")
            return 1

    print(f"ZIP：{zips[0].name}  {zips[0].stat().st_size/1024/1024:.1f} MB")
    print(f"启动器：{exe_size/1024/1024:.1f} MB")
    print(f"_internal/：{internal_size/1024/1024:.1f} MB")
    print("校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
