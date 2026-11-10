"""fetch_ffmpeg.py — 可选：为发行包下载 Windows 版 ffmpeg.exe（简体中文）。

设计为**永不阻断构建**：任一来源失败或解压不到 ffmpeg.exe 都只打印警告，
退出码仍为 0；由 workflow 与「使用说明.txt」告知用户首次处理视频时会自动下载。

来源按优先级尝试：
  1. gyan.dev 官方静态构建（ffmpeg-release-essentials.zip）
  2. 项目自带下载源（dseditor/Collection 的 ffmpeg.zip）
  3. hf-mirror 镜像（同一份 zip，便于国内网络）

用法：
    python tools/fetch_ffmpeg.py --out ffmpeg/ffmpeg.exe
"""

from __future__ import annotations

import argparse
import io
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

SOURCES = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://huggingface.co/dseditor/Collection/resolve/main/ffmpeg.zip",
    "https://hf-mirror.com/dseditor/Collection/resolve/main/ffmpeg.zip",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def find_ffmpeg_in_zip(raw: bytes) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist()
                 if n.lower().replace("\\", "/").endswith("/ffmpeg.exe")
                 or n.lower().replace("\\", "/") == "ffmpeg.exe"]
        if not names:
            return None
        # 优先选路径最短的（通常是 bin/ffmpeg.exe）
        names.sort(key=lambda n: (n.count("/"), len(n)))
        return z.read(names[0])



def _force_utf8():
    """Windows 控制台默认 cp1252 会让中文 print 抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def main() -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="下载 ffmpeg.exe（失败不阻断）")
    ap.add_argument("--out", default="ffmpeg/ffmpeg.exe",
                    help="输出路径，默认 ffmpeg/ffmpeg.exe")
    ap.add_argument("--timeout", type=int, default=180, help="单源超时秒数")
    args = ap.parse_args()
    out = Path(args.out)

    if out.exists() and out.stat().st_size > 1024 * 1024:
        print(f"已存在，跳过下载：{out} ({out.stat().st_size/1024/1024:.1f} MB)")
        return 0

    for url in SOURCES:
        try:
            print(f"尝试下载：{url}")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=args.timeout,
                                       context=ssl_ctx()) as resp:
                raw = resp.read()
            print(f"  收到 {len(raw)/1024/1024:.1f} MB，解压查找 ffmpeg.exe……")
            exe = find_ffmpeg_in_zip(raw)
            if not exe:
                print("  zip 内未找到 ffmpeg.exe，换下一个来源")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(exe)
            print(f"  成功：{out} ({len(exe)/1024/1024:.1f} MB)")
            return 0
        except Exception as e:
            print(f"  失败（{type(e).__name__}: {str(e)[:160]}），换下一个来源")

    print("所有来源均失败。构建继续；发行包不含 ffmpeg.exe。")
    print("WAV 输入不受影响；MP4/MKV 首次处理时程序会自行下载 ffmpeg。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
