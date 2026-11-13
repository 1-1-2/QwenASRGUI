"""同名檔案策略（suffix / overwrite / hash）與斷點續跑的檔名記憶。

這裡鎖住三件容易退化的行為：

1. **預設不覆蓋**：suffix 策略下重跑要得到「001 (1).srt」，而不是把舊稿蓋掉。
2. **續跑寫回同一份**：取消後重跑必須接在同一份檔案後面，不能每跑一次就多一
   個 (1)、(2) —— 這就是 checkpoint 要記 out_name 的原因。
3. **跨目錄同名不互撞**：輸出只取基名，不同資料夾的 001.mp4 是兩支不同的
   音檔；hash 策略靠來源路徑推導後綴把它們分開。

    python tests/test_out_naming.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from longform_runner import (Checkpoint, OUT_NAMING_POLICIES,  # noqa: E402
                             resolve_out_srt)


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


def main() -> int:
    r = Result()

    print("\n[1] 預設策略是 suffix（保留舊稿）")
    r.check("suffix 在清單裡且是第一個", OUT_NAMING_POLICIES[0] == "suffix")

    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        p0 = resolve_out_srt(d, "001", policy="suffix")
        r.check("空目錄 → 001.srt", p0.name == "001.srt", p0.name)
        p0.write_text("", encoding="utf-8")
        p1 = resolve_out_srt(d, "001", policy="suffix")
        r.check("已存在 → 001 (1).srt", p1.name == "001 (1).srt", p1.name)
        p1.write_text("", encoding="utf-8")
        p2 = resolve_out_srt(d, "001", policy="suffix")
        r.check("再存在 → 001 (2).srt", p2.name == "001 (2).srt", p2.name)
        r.check("三次解析都不同檔", len({p0, p1, p2}) == 3)

    print("\n[2] overwrite 就是舊行為")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        (d / "001.srt").write_text("old", encoding="utf-8")
        p = resolve_out_srt(d, "001", policy="overwrite")
        r.check("已存在仍回 001.srt", p.name == "001.srt", p.name)

    print("\n[3] hash 由來源推導：穩定、且跨目錄不互撞")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        a1 = resolve_out_srt(d, "001", policy="hash", task_id="aaaaaa111111")
        a2 = resolve_out_srt(d, "001", policy="hash", task_id="aaaaaa111111")
        b = resolve_out_srt(d, "001", policy="hash", task_id="bbbbbb222222")
        r.check("同一來源兩次呼叫同名（續跑找得回）", a1 == a2, a1.name)
        r.check("不同來源不同名（不互撞）", a1 != b, f"{a1.name} / {b.name}")
        r.check("後綴是 6 位十六進位", a1.stem == "001-aaaaaa", a1.stem)
        a1.write_text("", encoding="utf-8")
        r.check("hash 策略不再改名（同名即覆蓋本任務）",
                resolve_out_srt(d, "001", policy="hash",
                                task_id="aaaaaa111111").name == a1.name)

    print("\n[4] 真續跑沿用 checkpoint 記下的檔名")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        work = d / "state"
        ck = Checkpoint(work / "checkpoints.db", "task-x")
        ck.save(100, 5, "paused", fingerprint="fp-1", vad_state="",
                out_name="001 (3).srt")
        r.check("load_out_name 讀得回", ck.load_out_name() == "001 (3).srt",
                ck.load_out_name())
        # 目標檔不存在也要沿用（不然續跑會接在一個新檔上，前半段消失）
        p = resolve_out_srt(d, "001", policy="suffix", work_dir=work,
                            task_id="task-x", fingerprint="fp-1", resume=True)
        r.check("續跑沿用 001 (3).srt", p.name == "001 (3).srt", p.name)

    print("\n[5] 參數變了就不是續跑：suffix 另開一份")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw)
        work = d / "state"
        ck = Checkpoint(work / "checkpoints.db", "task-y")
        ck.save(100, 5, "paused", fingerprint="fp-old", vad_state="",
                out_name="001.srt")
        (d / "001.srt").write_text("old", encoding="utf-8")
        p = resolve_out_srt(d, "001", policy="suffix", work_dir=work,
                            task_id="task-y", fingerprint="fp-new", resume=True)
        r.check("指紋不同 → 001 (1).srt", p.name == "001 (1).srt", p.name)
        r.check("舊稿還在（沒被清空）",
                (d / "001.srt").read_text(encoding="utf-8") == "old")
        # resume=False（試跑／使用者選擇重來）同樣不沿用
        p2 = resolve_out_srt(d, "001", policy="suffix", work_dir=work,
                             task_id="task-y", fingerprint="fp-old", resume=False)
        r.check("resume=False 也不沿用", p2.name == "001 (1).srt", p2.name)

    print("\n[6] 安全性：只取基名，不能跳出輸出目錄")
    with tempfile.TemporaryDirectory() as raw:
        d = Path(raw) / "out"
        d.mkdir()
        p = resolve_out_srt(d, "../../etc/passwd", policy="overwrite")
        r.check("路徑被壓成基名", p.parent == d and p.name == "passwd.srt", str(p))
        p2 = resolve_out_srt(d, "", policy="overwrite")
        r.check("空名字退回 transcript", p2.name == "transcript.srt", p2.name)
        # 副檔名是「替換」不是「疊加」：舊版 out_ref.with_suffix(".srt") 的行為，
        # 換成 001.mp4.srt 會讓使用者找不到檔。
        r.check("001.mp4 → 001.srt",
                resolve_out_srt(d, "001.mp4", policy="overwrite").name == "001.srt")
        r.check("a.b.mp4 → a.b.srt",
                resolve_out_srt(d, "a.b.mp4", policy="overwrite").name == "a.b.srt")

    print("\n[7] 舊資料庫可升級（新增 out_name 欄）")
    with tempfile.TemporaryDirectory() as raw:
        db = Path(raw) / "legacy.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE progress (task_id TEXT PRIMARY KEY, "
                    "next_frame INTEGER, next_index INTEGER, status TEXT, "
                    "updated_at REAL, fingerprint TEXT DEFAULT '', "
                    "vad_state TEXT DEFAULT '')")
        con.execute("INSERT INTO progress VALUES ('t',10,3,'paused',0,'fp','')")
        con.commit()
        con.close()
        ck = Checkpoint(db, "t")
        r.check("舊庫 out_name 讀成空字串", ck.load_out_name() == "", ck.load_out_name())
        ck.save(10, 3, "paused", out_name="new name.srt")
        r.check("寫入後讀得回", ck.load_out_name() == "new name.srt")
        r.check("舊資料沒被清掉", ck.load() == (10, 3, "paused"), str(ck.load()))

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
