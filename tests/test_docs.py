"""Wave 6 regression: documentation completeness and bilingual pairing.

Checks that every user-facing document has a Traditional/Simplified pair with a
top-of-page cross link, that generated artefacts are reproducible, and that the
verification-boundary / feature-matrix pages actually state what they must.

    python tests/test_docs.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def main() -> int:
    r = Result()
    docs = ROOT / "docs"

    print("\n[Wave 6] bilingual pairs")
    pairs = [
        ("README.md", "README.zh-Hant.md"),
        ("docs/README.md", "docs/README.zh-Hant.md"),
        ("docs/架构说明.md", "docs/架构说明.zh-Hant.md"),
        ("docs/打包与升级.md", "docs/打包與升級.zh-Hant.md"),
        ("docs/功能矩阵.md", "docs/功能矩陣.zh-Hant.md"),
        ("docs/验证边界.md", "docs/驗證邊界.zh-Hant.md"),
        ("docs/风格规范.md", "docs/風格規範.zh-Hant.md"),
        ("docs/多语文案契约.md", "docs/多語文案契約.zh-Hant.md"),
        ("docs/模型输入约束.md", "docs/模型輸入約束.zh-Hant.md"),
    ]
    for hans, hant in pairs:
        a, b = ROOT / hans, ROOT / hant
        r.check(f"pair exists: {hans}", a.is_file() and b.is_file())
        if not (a.is_file() and b.is_file()):
            continue
        ta, tb = a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")
        r.check(f"  {hans} links to its counterpart",
                b.name in ta.split("\n", 4)[0] + ta[:200])
        r.check(f"  {hant} links back", a.name in tb[:200])

    print("\n[Wave 6] single-language / generated documents")
    single = ["docs/长音频流式方案.md", "docs/legacy/README_upstream.md",
              "docs/模型下载清单.md", "docs/architecture.mmd",
              "docs/architecture.svg", "docs/architecture-dark.svg",
              "CHANGELOG.md", "NOTICE"]
    for f in single:
        r.check(f"exists: {f}", (ROOT / f).is_file())
    r.check("upstream README archived verbatim",
            (docs / "legacy/README_upstream.md").read_text(encoding="utf-8")
            .startswith("# 聲音辨識小工具"))
    r.check("NOTICE states the licensing blocker",
            "does\nNOT contain a LICENSE" in (ROOT / "NOTICE").read_text(encoding="utf-8")
            or "NOT contain a LICENSE" in (ROOT / "NOTICE").read_text(encoding="utf-8"))
    r.check("CHANGELOG covers waves 1-6",
            all(f"Wave {i}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
                for i in range(1, 7)))

    print("\n[Wave 6] boundary-first content")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    r.check("README leads with boundaries",
            readme.index("先说边界") < readme.index("## 这是什么"))
    r.check("README has an Important SmartScreen block",
            "Important · SmartScreen" in readme and "未签名" in readme)
    r.check("README links the verification boundary", "docs/验证边界.md" in readme)
    boundary = (docs / "验证边界.md").read_text(encoding="utf-8")
    r.check("verification page separates verified / unverified",
            "## 已验证" in boundary and "## 未验证" in boundary)
    r.check("verification page admits no real weights",
            "真实 0.6B" in boundary and "mock ASR" in boundary)
    matrix = (docs / "功能矩阵.md").read_text(encoding="utf-8")
    r.check("feature matrix marks legacy items", "遗留未迁移" in matrix)
    style = (docs / "风格规范.md").read_text(encoding="utf-8")
    r.check("style guide enforces boundary-first + measured numbers",
            "边界前置" in style and "性能宣称必附实测数字" in style)
    pkg = (docs / "打包与升级.md").read_text(encoding="utf-8")
    r.check("packaging doc documents rollback and signing",
            "rollback" in pkg and "未签名" in pkg and "app.prev/" in pkg)
    r.check("both SVG themes exist and differ",
            (docs / "architecture.svg").read_bytes() !=
            (docs / "architecture-dark.svg").read_bytes())
    r.check("docs index uses <picture> for theme switching",
            "<picture>" in (docs / "README.md").read_text(encoding="utf-8") and
            "prefers-color-scheme" in (docs / "README.zh-Hant.md").read_text(encoding="utf-8"))

    print("\n[Wave 6] generated artefact is reproducible")
    before = (docs / "模型下载清单.md").read_bytes()
    out = subprocess.run([sys.executable, "tools/gen_download_doc.py"], cwd=ROOT,
                         capture_output=True, text=True)
    r.check("generator exits 0", out.returncode == 0, out.stderr[:120])
    r.check("re-generation is byte-identical",
            (docs / "模型下载清单.md").read_bytes() == before)
    listing = (docs / "模型下载清单.md").read_text(encoding="utf-8")
    r.check("generated list is explicitly marked as generated",
            "请勿手工编辑" in listing and "models.json" in listing)
    r.check("generated list keeps unknown values honest",
            "未知" in listing and "不会用估计值冒充" in listing)

    print("\n[Wave 6] planning documents stay out of the repository")
    for banned in ("开发计划.md", "PR拆分说明.md", "上游繁体issue提案.md"):
        r.check(f"no {banned} in repo", not (ROOT / banned).exists() and
                not (docs / banned).exists())

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
