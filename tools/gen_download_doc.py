"""Generate docs/模型下载清单.md from models.json (single source of truth).

The catalog, not this script, owns the data. Re-run after any models.json edit:

    python tools/gen_download_doc.py

Generated documents are deliberately NOT translated (see docs/风格规范.md).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "模型下载清单.md"


def _fmt_size(v) -> str:
    if isinstance(v, int) and v > 0:
        return f"{v / 1024 / 1024:.1f} MiB ({v} bytes)"
    return "未知"


def main() -> int:
    doc = json.loads((ROOT / "models.json").read_text(encoding="utf-8"))
    sources = doc.get("sources", {})
    lines = [
        "# 模型下载清单",
        "",
        "> 本文件由 `tools/gen_download_doc.py` 从 `models.json` 生成，**请勿手工编辑**。",
        "> 生成物不翻译；`大小未知` / `SHA256 未知` 表示该值尚未与远端文件核实，不会用估计值冒充。",
        "",
        "「下载中心」→「输出下载清单」可在程序内导出带 aria2c 命令的 `.txt` 版本；",
        "本页是同样的信息，便于在仓库里直接查阅。",
        "",
        "## 镜像",
        "",
        "| 端点 | 地址 | 已验证 |",
        "|---|---|---|",
    ]
    for m in doc.get("mirrors", {}).get("candidates", []):
        lines.append(f"| {m.get('id', '')} | `{m.get('base', '')}` | "
                     f"{'是' if m.get('verified') else '否（未实测）'} |")
    lines += ["", "## 下载源", "", "| ID | base | kind |", "|---|---|---|"]
    for key, s in sources.items():
        lines.append(f"| `{key}` | `{s.get('base', '')}` | {s.get('kind', '')} |")

    for b in doc.get("bundles", []):
        lines += [
            "", f"## {b.get('label', b.get('id', ''))}", "",
            f"- ID：`{b.get('id', '')}`",
            f"- 适用模式：{', '.join('`' + str(x) + '`' for x in b.get('modes', [])) or '—'}",
            f"- 目标目录：`{b.get('target_dir', '')}`",
            f"- 用于：{', '.join(b.get('required_for', [])) or '—'}",
            f"- 大小提示：{b.get('size_hint') or '未知'}"
            f"（估计值，不用于进度计算）",
            f"- 提供者：{b.get('provider') or '—'}",
            f"- 可选：{'是' if b.get('optional') else '否'}",
            "",
            "| 文件 | 大小 | SHA256 | 主源 | 备用源 |",
            "|---|---|---|---|---|",
        ]
        for f in b.get("files", []):
            src = sources.get(f.get("source", ""), {})
            fb = sources.get(f.get("fallback_source", ""), {})
            base = src.get("base", "")
            fbbase = fb.get("base", "")
            name = f.get("name", "")
            primary = f"`{base}/{name}`" if base else "—"
            fallback = f"`{fbbase}/{name}`" if fbbase else "—"
            sha = f.get("sha256") or "未知"
            if sha != "未知":
                sha = f"`{sha[:16]}…`"
            lines.append(f"| `{name}` | {_fmt_size(f.get('size_bytes'))} | {sha} | "
                         f"{primary} | {fallback} |")

    lines += [
        "", "## 说明", "",
        "- `size_bytes: null` 表示尚未与远端核实，下载层会退回 `Content-Length`，不拿估计值算进度。",
        "- 程序内「导入本地模型」会执行 `full_verify()`，逐文件校验大小与 SHA256。",
        "- 模型权重不内嵌进发行包；装好之后可完全离线使用。",
        "- 第三方许可状态见 [NOTICE](../NOTICE)。",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(lines)} lines, "
          f"{len(doc.get('bundles', []))} bundles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
