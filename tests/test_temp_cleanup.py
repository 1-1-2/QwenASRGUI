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
from webview_server import (prune_checkpoints,    # noqa: E402
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

    print("\n[2] 拖放（上传副本）：靠内容哈希认出同一个档案 → 可接续")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        payload = b"RIFF" + b"\x11\x22" * 500_000
        a = d / "up1" / "upload.wav"
        b = d / "up2" / "upload.wav"
        for p in (a, b):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
        id_a = WebBackend._source_identity(a, "課程.wav", upload=True)
        id_b = WebBackend._source_identity(b, "課程.wav", upload=True)
        r.check("内容相同、临时目录不同 → 同一个任务（可接续）",
                id_a[0] == id_b[0], f"{id_a[0]} / {id_b[0]}")
        r.check("指紋的 src 分量也相同（不会误判成参数变更）", id_a[1] == id_b[1],
                f"{id_a[1]} / {id_b[1]}")

        c = d / "up3" / "upload.wav"
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_bytes(payload[:-1] + b"\x99")            # 尾端差一个 byte
        r.check("内容不同 → 不同任务",
                WebBackend._source_identity(c, "課程.wav", upload=True)[0] != id_a[0])
        r.check("档名不同 → 不同任务",
                WebBackend._source_identity(a, "另一堂.wav", upload=True)[0] != id_a[0])

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

    print("\n[4] 斷點記錄：按「保留天數」清理久未續跑的記錄（任何狀態都適用）")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        old = time.time() - 40 * 86400
        _mk(d / "done_old.segments.jsonl", 1000, 40 * 86400)
        _mk(d / "paused_old.segments.jsonl", 2000, 40 * 86400)
        _mk(d / "done_fresh.segments.jsonl", 4000, 60)
        _mk(d / "orphan_old.segments.jsonl", 8000, 40 * 86400)   # 沒有斷點列
        con = sqlite3.connect(d / "checkpoints.db")
        con.execute("CREATE TABLE progress (task_id TEXT PRIMARY KEY, "
                    "next_frame INTEGER, next_index INTEGER, status TEXT, "
                    "updated_at REAL, fingerprint TEXT DEFAULT '', "
                    "vad_state TEXT DEFAULT '', out_name TEXT DEFAULT '')")
        con.executemany("INSERT INTO progress VALUES (?,0,1,?,?,'','','')", [
            ("done_old", "done", old), ("paused_old", "paused", old),
            ("done_fresh", "done", time.time())])
        con.commit(); con.close()
        # days=0 → 使用者選了永久保留，什麼都不動
        r.check("保留天數 0 = 永久保留",
                prune_checkpoints(0, state_dir=d)["removed"] == 0)
        res = prune_checkpoints(30, state_dir=d)
        r.check("刪了 3 份（done 舊、paused 舊、無斷點孤兒）", res["removed"] == 3, str(res))
        r.check("釋放位元組算對", res["freed"] == 11000, str(res["freed"]))
        r.check("久未續跑的 paused 也會清（按天數，不看狀態）",
                not (d / "paused_old.segments.jsonl").exists())
        r.check("30 天內的不刪", (d / "done_fresh.segments.jsonl").is_file())
        con = sqlite3.connect(d / "checkpoints.db")
        try:
            rows = {r[0] for r in con.execute("SELECT task_id FROM progress")}
        finally:
            con.close()
        r.check("被刪記錄對應的斷點列也清掉",
                "done_old" not in rows and "paused_old" not in rows, str(sorted(rows)))
        r.check("保留的任務斷點列還在", "done_fresh" in rows, str(sorted(rows)))
        r.check("重複呼叫安全", prune_checkpoints(30, state_dir=d)["removed"] == 0)

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
