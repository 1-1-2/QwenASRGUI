"""Wave 4 / B4 regression: persistent server-side batch queue.

Runs without OpenVINO weights, network or a running HTTP server. A fake
backend records call order and lets individual files fail on demand.

    python tests/test_batch_queue.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from batch_queue import BatchQueue  # noqa: E402


class Result:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail=""):
        self.items.append(bool(ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self):
        return all(self.items)


class FakeBackend:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def transcribe(self, opts, progress_cb=None):
        name = Path(opts["name"]).name
        self.calls.append(name)
        if progress_cb:
            progress_cb(50, "half")
            progress_cb(100, "完成")
        if name in self.fail_on:
            raise RuntimeError("mock engine failure")
        return {"srtPath": f"/subtitles/{name}.srt", "totalSegments": 7}


def upload(tmp: Path, name: str) -> Path:
    p = tmp / name
    p.write_bytes(b"RIFF-fake-wav")
    return p


def wait_done(q: BatchQueue, timeout: float = 4.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not q.running:
            return True
        time.sleep(0.01)
    return False


def main() -> int:
    r = Result()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state = tmp / "state"
        events = []
        be = FakeBackend()
        q = BatchQueue(be, on_event=lambda ev, payload: events.append((ev, payload)),
                       state_dir=state)

        print("\n[B4] register and persist")
        a = q.add(upload(tmp, "a.wav"), "a.wav", {"language": "zh", "path": "/evil/injected.wav"})
        b = q.add(upload(tmp, "b.mp3"), "b.mp3", {"hint": "hello"})
        r.check("two items registered", len(q.items) == 2)
        r.check("ids are unique", a["id"] != b["id"])
        r.check("client cannot inject a stored path",
                a["opts"]["path"] != "/evil/injected.wav" and
                Path(a["opts"]["path"]).parent == state / "batch_uploads",
                a["opts"]["path"])
        r.check("stored name keeps the original extension",
                a["opts"]["name"] == "a.wav" and a["opts"]["path"].endswith(".wav"))
        r.check("opts allowlist drops unknown keys",
                "vadThreshold" not in a["opts"] and a["opts"].get("language") == "zh")
        r.check("uploads copied into the queue dir",
                Path(a["path"]).exists() and Path(b["path"]).exists())
        r.check("state file written", q.state_file.is_file())
        snap = q.snapshot()
        r.check("snapshot has summary and items",
                snap["summary"]["total"] == 2 and len(snap["items"]) == 2)

        print("\n[B4] serial execution")
        q.start()
        r.check("worker reports running", q.running is True or wait_done(q))
        r.check("all files processed", wait_done(q), str(q.running))
        r.check("executed in insertion order", be.calls == ["a.wav", "b.mp3"], str(be.calls))
        r.check("all items done", all(i["status"] == "done" for i in q.items),
                str([i["status"] for i in q.items]))
        r.check("srt paths recorded", q.items[0]["srtPath"].endswith("a.wav.srt"))
        r.check("progress saturated at 1.0", all(i["progress"] == 1.0 for i in q.items))
        r.check("SSE batch events emitted", any(e == "batch" for e, _ in events),
                str(len(events)))
        r.check("start with nothing pending is a no-op", q.start()["running"] is False)

        print("\n[B4] failure isolation")
        fail_dir = tmp / "state2"
        be2 = FakeBackend(fail_on=["bad.wav"])
        q2 = BatchQueue(be2, state_dir=fail_dir)
        q2.add(upload(tmp, "bad.wav"), "bad.wav")
        q2.add(upload(tmp, "good.wav"), "good.wav")
        q2.start()
        r.check("worker finished", wait_done(q2))
        r.check("one failed, one done",
                [i["status"] for i in q2.items] == ["failed", "done"],
                str([i["status"] for i in q2.items]))
        r.check("later item still ran after a failure", be2.calls == ["bad.wav", "good.wav"])
        r.check("error message retained", "mock engine failure" in q2.items[0]["error"])
        r.check("failed item can be retried", q2.retry(q2.items[0]["id"]) is True)
        r.check("retry resets progress", q2.items[0]["status"] == "pending"
                and q2.items[0]["progress"] == 0.0)

        print("\n[B4] restart recovery")
        # Simulate a process death: state says "running", no worker alive.
        import json
        data = json.loads(q2.state_file.read_text(encoding="utf-8"))
        data["items"][0]["status"] = "running"
        q2.state_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        q3 = BatchQueue(FakeBackend(), state_dir=fail_dir)
        r.check("running item becomes pending on reload",
                q3.items[0]["status"] == "pending", q3.items[0]["status"])
        r.check("state survives reload", len(q3.items) == 2)

        print("\n[B4] corrupt / missing state")
        broken = tmp / "broken"
        broken.mkdir()
        (broken / "batch_queue.json").write_text("{not json", encoding="utf-8")
        q4 = BatchQueue(FakeBackend(), state_dir=broken)
        r.check("corrupt state does not crash startup", q4.items == [])
        missing = tmp / "missing"
        missing.mkdir()
        (missing / "batch_queue.json").write_text(
            '{"items":[{"id":"x","name":"gone.wav","status":"pending",'
            '"path":"/definitely/not/here.wav"}]}', encoding="utf-8")
        be5 = FakeBackend()
        q5 = BatchQueue(be5, state_dir=missing)
        r.check("missing upload is marked failed, not silently retried",
                q5.items[0]["status"] == "failed", q5.items[0]["status"])

        print("\n[B4] remove / clear / cancel")
        q5.items = []
        real = q5.add(upload(tmp, "r.wav"), "r.wav")
        r.check("remove works", q5.remove(real["id"]) is True and not q5.items)
        q5.add(upload(tmp, "d.wav"), "d.wav")
        q5.items[0]["status"] = "done"
        r.check("clear_finished removes only terminal items",
                q5.clear_finished() == 1 and not q5.items)
        q5.add(upload(tmp, "c1.wav"), "c1.wav")
        q5.add(upload(tmp, "c2.wav"), "c2.wav")
        q5.items[0]["status"] = "running"
        q5.active_id = q5.items[0]["id"]
        r.check("cannot remove a running item", q5.remove(q5.items[0]["id"]) is False)
        q5.cancel()
        # Wave 11／問題 4：舊語義把所有 pending 一起標成 canceled，使用者按
        # 「停止」再按「開始」時沒有任何 pending 可跑，佇列看起來被清空。
        # 新語義是「暫停」：pending 原封不動、running 退回 pending。
        r.check("cancel keeps pending and puts running back to pending",
                all(i["status"] == "pending" for i in q5.items),
                str([i["status"] for i in q5.items]))
        r.check("cancel calls backend.cancel()", be5.cancelled is True)
        q5.start()
        r.check("start after cancel resumes the pending items",
                wait_done(q5) and all(i["status"] == "done" for i in q5.items),
                str([i["status"] for i in q5.items]))

        print("\n[Wave 11] 本機路徑直引、副本清理、孤兒清掃")
        import threading
        w11 = tmp / "state_w11"
        be6 = FakeBackend()
        q6 = BatchQueue(be6, state_dir=w11)
        local = upload(tmp, "local.m4a")
        it = q6.add_path(local, opts={"language": "zh", "trial": True})
        r.check("add_path 直接引用原檔、不複製",
                Path(it["path"]).resolve() == local.resolve() and
                it["ownedPath"] is False and it["localPath"] is True, it["path"])
        r.check("add_path 只保留白名單 opts（試跑不可注入）",
                it["opts"].get("language") == "zh" and "trial" not in it["opts"])

        def _reject(fn):
            try:
                fn()
                return False
            except ValueError:
                return True

        r.check("add_path 拒絕不存在的檔案",
                _reject(lambda: q6.add_path(tmp / "nope.wav")))
        txt = tmp / "readme.txt"
        txt.write_text("hi", encoding="utf-8")
        r.check("add_path 拒絕非音訊副檔名", _reject(lambda: q6.add_path(txt)))
        r.check("add_path 拒絕 UNC 網路路徑",
                _reject(lambda: q6.add_path(r"\\server\share\a.wav")))

        up = q6.add(upload(tmp, "copied.wav"), "copied.wav")
        up_path = Path(up["path"])
        r.check("上傳副本標記為自有", up["ownedPath"] is True and up_path.is_file())
        orphan = q6.upload_dir / "orphan.wav"
        orphan.write_bytes(b"orphan")
        q6._sweep_orphans()
        r.check("啟動清掃刪掉孤兒副本", not orphan.exists())
        r.check("清掃保留仍被引用的副本", up_path.is_file())
        r.check("remove 連自有副本一起刪",
                q6.remove(up["id"]) is True and not up_path.exists())
        r.check("remove 絕不刪使用者本機原檔",
                q6.remove(it["id"]) is True and local.is_file())
        r.check("兩個都移除後佇列為空", q6.items == [])
        up2 = q6.add(upload(tmp, "finished.wav"), "finished.wav")
        up2_path = Path(up2["path"])
        q6.items[0]["status"] = "done"
        r.check("clear_finished 也刪自有副本",
                q6.clear_finished() == 1 and not up2_path.exists())

        print("\n[Wave 11] 停止後立即再開始：generation 不互相覆蓋")
        class GenBackend:
            """第 1、2 次 transcribe 各用一個 gate 卡住，模擬舊 worker 晚收尾。"""
            def __init__(self):
                self.calls, self.gates, self.scopes = [], [], []
                self._lk = threading.Lock()

            def set_progress_scope(self, s):
                with self._lk:
                    self.scopes.append(s)

            def cancel(self):
                with self._lk:
                    if self.gates:
                        self.gates[0].set()      # 只放行舊 worker

            def transcribe(self, opts, progress_cb=None):
                with self._lk:
                    n = len(self.calls)
                    self.calls.append(opts.get("name"))
                if n <= 1:
                    g = threading.Event()
                    with self._lk:
                        self.gates.append(g)
                    g.wait(3.0)
                if progress_cb:
                    progress_cb(100, "done")
                return {"srtPath": "/x.srt", "totalSegments": 1}

        be7 = GenBackend()
        q7 = BatchQueue(be7, state_dir=tmp / "state_w11b")
        for n in ("b1.wav", "b2.wav"):
            q7.add(upload(tmp, n), n)
        q7.start()
        deadline = time.time() + 2
        while time.time() < deadline and not be7.calls:
            time.sleep(0.01)
        r.check("舊 worker 已進入 transcribe", len(be7.calls) >= 1)
        q7.cancel()
        r.check("cancel 後兩個項目都是 pending",
                all(i["status"] == "pending" for i in q7.items),
                str([i["status"] for i in q7.items]))
        q7.start()
        time.sleep(0.35)      # 讓舊 worker 收尾；新 worker 仍卡在 gate[1]
        r.check("舊 worker 收尾不會把新 worker 的 running 改成 False",
                q7.running is True, str(q7.running))
        r.check("批次任務標記 scope=batch 並在結束後還原",
                "batch" in be7.scopes and None in be7.scopes, str(be7.scopes))
        with be7._lk:
            if len(be7.gates) > 1:
                be7.gates[1].set()
        r.check("新 worker 最終正常收網", wait_done(q7, 4.0), str(q7.running))

        print("\n[B4] summary")
        q5.items = [
            {"id": "1", "status": "done", "progress": 1.0},
            {"id": "2", "status": "pending", "progress": 0.0},
            {"id": "3", "status": "failed", "progress": 0.0},
        ]
        s = q5.summary()
        r.check("summary counts", s["total"] == 3 and s["done"] == 1 and s["failed"] == 1,
                str(s))
        r.check("summary average progress", abs(s["progress"] - 1 / 3) < 0.01,
                str(s["progress"]))

    print(f"\nResult: {sum(r.items)}/{len(r.items)} passed")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
