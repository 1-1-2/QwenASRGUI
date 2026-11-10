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
        summ = q5.cancel()
        r.check("cancel marks pending/running as canceled",
                all(i["status"] == "canceled" for i in q5.items),
                str([i["status"] for i in q5.items]))
        r.check("cancel calls backend.cancel()", be5.cancelled is True)

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
