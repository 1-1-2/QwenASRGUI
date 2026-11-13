"""暫存清掃 + 上傳來源的「內容身分」。

兩件事一起測，因為它們是同一個問題的兩半：

* 上傳的音檔會整份複製到 ``%TEMP%\\asr_web_*``，正常結束會刪；崩潰／被強制
  結束就留下來 → 需要 ``sweep_stale_uploads()`` 補掃。
* 上傳路徑每次都不一樣（隨機臨時目錄）。如果拿它當 task_id，「取消後重選
  同一支檔案」就是另一個任務，斷點永遠接不上 → 上傳改用內容身分。

    python tests/test_temp_cleanup.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# webview_server 會 import app（打包版路徑）；發行版本來就設這個變數。
os.environ.setdefault("QWENASR_WEBVIEW_BUILD", "1")

from webview_backend import WebBackend            # noqa: E402
from webview_server import (prune_finished_state,  # noqa: E402
                            sweep_stale_uploads)


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def _mk(p: Path, size: int, age_sec: float) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    t = time.time() - age_sec
    os.utime(p.parent, (t, t))
    os.utime(p, (t, t))


def main() -> int:
    r = Result()

    print("\n[1] 只清本程式的目錄，而且只清舊的")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _mk(root / "asr_web_old" / "upload.mp4", 1024, 3600)
        _mk(root / "asr_api_old" / "upload.wav", 2048, 3600)
        _mk(root / "asr_web_fresh" / "upload.mp4", 4096, 0)
        _mk(root / "someone_else" / "data.bin", 8192, 3600)
        res = sweep_stale_uploads(min_age=600, root=root)
        r.check("刪了 2 個舊目錄", res["removed"] == 2, str(res))
        r.check("釋放大小算對（1KB+2KB）", res["freed"] == 3072, str(res["freed"]))
        r.check("10 分鐘內的不動", (root / "asr_web_fresh").is_dir())
        r.check("別人的目錄不動", (root / "someone_else" / "data.bin").is_file())
        r.check("舊目錄真的沒了",
                not (root / "asr_web_old").exists()
                and not (root / "asr_api_old").exists())
        # 再跑一次：沒有東西可刪，也不能報錯
        res2 = sweep_stale_uploads(min_age=600, root=root)
        r.check("重複呼叫安全", res2["removed"] == 0 and res2["failed"] == 0, str(res2))

    print("\n[2] 拖放（上传副本）：每次都是独立任务 —— 也就是「不支持接续」")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        payload = b"RIFF" + b"\x11\x22" * 500_000
        a = d / "up1" / "upload.wav"
        b = d / "up2" / "upload.wav"
        for p in (a, b):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
        id_a = WebBackend._source_identity(a)
        id_b = WebBackend._source_identity(b)
        r.check("内容相同、临时目录不同 → 不同任务（不接续）",
                id_a[0] != id_b[0], f"{id_a[0]} / {id_b[0]}")
        r.check("同一路径重复调用稳定（同一任务内不会漂移）",
                WebBackend._source_identity(a)[0] == id_a[0])

    print("\n[3] 本機路徑以路徑為身分（點選／批次可續跑）")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        x = d / "a.wav"; y = d / "b.wav"
        for p in (x, y):
            p.write_bytes(b"\0" * 100)
        ix = WebBackend._source_identity(x)
        iy = WebBackend._source_identity(y)
        r.check("同內容不同路徑 → 不同任務（照舊以路徑為身分）", ix[0] != iy[0])
        # 舊算法是 src=大小/修改時間：mtime 一變，指紋就該失效（代表會從頭跑）
        old = time.time() - 5000
        os.utime(y, (old, old))
        iy2 = WebBackend._source_identity(y)
        r.check("src 分量仍是 size/mtime 且跟著 mtime 變",
                ix[1].startswith("src=100/") and ix[1] != iy2[1],
                f"{ix[1]} vs {iy2[1]}")

    print("\n[4] longform_state：只清「已完成且很久沒動」的逐段記錄")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        _mk(d / "done_old.segments.jsonl", 1000, 40 * 86400)
        _mk(d / "paused_old.segments.jsonl", 2000, 40 * 86400)
        _mk(d / "done_fresh.segments.jsonl", 4000, 60)
        _mk(d / "orphan_old.segments.jsonl", 8000, 40 * 86400)   # 沒有斷點列
        con = sqlite3.connect(d / "checkpoints.db")
        con.execute("CREATE TABLE progress (task_id TEXT PRIMARY KEY, "
                    "next_frame INTEGER, next_index INTEGER, status TEXT, "
                    "updated_at REAL, fingerprint TEXT DEFAULT '', "
                    "vad_state TEXT DEFAULT '', out_name TEXT DEFAULT '')")
        con.executemany("INSERT INTO progress VALUES (?,0,1,?,0,'','','')", [
            ("done_old", "done"), ("paused_old", "paused"), ("done_fresh", "done")])
        con.commit(); con.close()
        res = prune_finished_state(max_age_days=30, state_dir=d)
        r.check("刪了 2 份（done+舊、無斷點孤兒）", res["removed"] == 2, str(res))
        r.check("釋放位元組算對", res["freed"] == 9000, str(res["freed"]))
        r.check("未完成的任務絕不刪", (d / "paused_old.segments.jsonl").is_file())
        r.check("30 天內的不刪", (d / "done_fresh.segments.jsonl").is_file())
        con = sqlite3.connect(d / "checkpoints.db")
        try:
            rows = {r[0] for r in con.execute("SELECT task_id FROM progress")}
        finally:
            con.close()
        r.check("被刪記錄對應的斷點列也清掉", "done_old" not in rows, str(sorted(rows)))
        r.check("保留的任務斷點列還在",
                {"paused_old", "done_fresh"} <= rows, str(sorted(rows)))
        r.check("重複呼叫安全",
                prune_finished_state(max_age_days=30, state_dir=d)["removed"] == 0)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
