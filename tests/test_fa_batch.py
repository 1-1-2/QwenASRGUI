"""fa_batch 单元测试 —— 不加载任何模型，align_fn 全部注入。

跑法：``python tests/test_fa_batch.py``
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QWENASR_WEBVIEW_BUILD", "1")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import numpy as np

from fa_batch import FaBatch, MAX_WINDOW, MIN_WINDOW


class R:
    def __init__(self):
        self.pass_ = 0
        self.fail_ = 0
        self.fails = []

    def check(self, name, ok, detail=""):
        if ok:
            self.pass_ += 1
        else:
            self.fail_ += 1
            self.fails.append(f"{name}  {detail}")
            print(f"  ✗ {name}  {detail}")

    def ok(self):
        print(f"\n{'='*54}\nPASS {self.pass_} / FAIL {self.fail_}")
        for f in self.fails:
            print("  -", f)
        return self.fail_ == 0


def pcm(sec, val=0.01, sr=16000):
    return np.full(int(sec * sr), val, dtype=np.float32)


def fake_align(audio, ref, lang):
    """一字一词，均匀铺满整段音频。"""
    n = len(ref)
    if n == 0:
        return []
    step = (len(audio) / 16000) / n
    return [(c, i * step, (i + 1) * step) for i, c in enumerate(ref)]


def new(initial=2.0, lo=1.0, hi=8.0, align=None, **kw):
    return FaBatch(align or fake_align, initial_window=initial,
                   min_window=lo, max_window=hi, async_worker=False, **kw)


def main() -> int:
    r = R()

    # ── 1. 窗口累积与派发 ────────────────────────────────────
    q = new()
    r.check("窗口初值", q.window_sec == 2.0, str(q.window_sec))
    r.check("空队列派发无副作用", q._dispatch() is None and q.windows == 0)
    h1 = q.submit(pcm(1.0), "一二三", 10.0, 11.0)
    r.check("未达窗口不派发", q.windows == 0 and q.inflight() == 0)
    r.check("pending_sec 累计", abs(q.pending_sec() - 1.0) < 1e-3)
    h2 = q.submit(pcm(1.2), "四五六", 12.0, 13.2)
    r.check("达窗口自动派发", q.windows == 1 and q.inflight() == 1)
    r.check("派发后队列清空", q.pending_sec() == 0.0)

    l1 = q.settle(h1)
    l2 = q.settle(h2)
    r.check("settle 返回行列表", isinstance(l1, list) and isinstance(l2, list))
    r.check("行文本覆盖全文",
            "".join(x[2] for x in l1) == "一二三"
            and "".join(x[2] for x in l2) == "四五六",
            repr([x[2] for x in l1]))
    r.check("行带 words", all(len(x) > 3 and x[3] for x in l1 + l2))
    r.check("words 时间转到绝对轴",
            l1[0][3][0]["start"] >= 10.0 and l2[0][3][0]["start"] >= 12.0,
            f'{l1[0][3][0]["start"]} {l2[0][3][0]["start"]}')
    r.check("全部 settle 后 inflight 归零", q.inflight() == 0)

    # ── 2. 空文本 ────────────────────────────────────────────
    q = new()
    h = q.submit(pcm(1.0), "", 0.0, 1.0)
    r.check("空文本返回空句柄", h.empty and q.settle(h) is None)
    r.check("空文本不进队列", q.pending_sec() == 0.0 and q.windows == 0)

    # ── 3. 单段超窗立即派发 ──────────────────────────────────
    q = new(initial=2.0, hi=8.0)
    q.submit(pcm(5.0), "abcdef", 0.0, 5.0)
    r.check("单段超窗立即派发", q.windows == 1 and q.pending_sec() == 0.0)

    # ── 4. 顺序：多窗按提交顺序 ──────────────────────────────
    q = new(initial=1.0, lo=1.0, hi=8.0)
    hs = [q.submit(pcm(1.0), "ab", i * 3.0, i * 3.0 + 1.0) for i in range(3)]
    r.check("三窗依次派发", q.windows == 3, str(q.windows))
    texts = []
    for h in hs:
        lines = q.settle(h)
        texts.append("".join(x[2] for x in lines) if lines else "")
    r.check("settle 顺序与提交一致", "".join(texts) == "ababab", str(texts))
    r.check("三窗全部收毕", q.inflight() == 0, str(q.inflight()))

    # ── 5. 失败降级 ──────────────────────────────────────────
    cases = [
        ("返回空", lambda a, t, l: []),
        ("抛异常", lambda a, t, l: (_ for _ in ()).throw(RuntimeError("boom"))),
        # 只覆盖 0~1 秒（共 3 秒）→ 覆盖度 33% < 50%
        ("覆盖不足", lambda a, t, l: [("a", 0.0, 0.3), ("b", 0.6, 1.0)]),
    ]
    for name, fn in cases:
        q = new(initial=1.0, lo=1.0, hi=4.0, align=fn)
        h = q.submit(pcm(3.0), "abc", 0.0, 3.0)
        out = q.settle(h)
        r.check(f"{name} → 整段降级", out is None, str(out))
        r.check(f"{name} 计入 failures", q.failures == 1, str(q.failures))

    # ── 6. 吞标点仍产出词级（中点法）────────────────────────
    def drop_punct(audio, ref, lang):
        chars = [c for c in ref if c not in "，。、！？"]
        step = (len(audio) / 16000) / max(1, len(chars))
        return [(c, i * step, (i + 1) * step) for i, c in enumerate(chars)]

    q = new(initial=1.0, lo=1.0, hi=4.0, align=drop_punct)
    h = q.submit(pcm(2.0), "你好，世界。", 5.0, 7.0)
    out = q.settle(h)
    r.check("吞标点不判降级", out is not None and q.failures == 0)
    if out:
        words = [w["word"] for x in out for w in x[3]]
        r.check("吞标点后词序列无标点", "".join(words) == "你好世界", "".join(words))
        r.check("吞标点后时间在绝对轴",
                all(w["start"] >= 5.0 for x in out for w in x[3]))

    # ── 7. 自适应窗口 ────────────────────────────────────────
    q = new(initial=25.0, lo=MIN_WINDOW, hi=MAX_WINDOW)
    q.observations = [(10.0, 1.68)]
    r.check("样本不足保持默认", q._suggest_window() == 25.0)
    q.observations = [(10.0, 1.68), (20.0, 2.54), (30.0, 3.47), (60.0, 8.28)]
    w = q._suggest_window()
    r.check("由实测点算出窗口落在 15~40", MIN_WINDOW <= w <= MAX_WINDOW, str(w))
    r.check("算出窗口接近 30 秒甜点", 20.0 <= w <= 35.0, str(w))
    q2 = new(initial=99.0, lo=MIN_WINDOW, hi=MAX_WINDOW)
    r.check("初值被 clamp 到上限", q2.window_sec == MAX_WINDOW)
    q3 = new(initial=0.1, lo=MIN_WINDOW, hi=MAX_WINDOW)
    r.check("初值被 clamp 到下限", q3.window_sec == MIN_WINDOW)

    # ── 8. 异步：submit 不阻塞 ──────────────────────────────
    def slow_align(audio, ref, lang):
        time.sleep(0.15)
        return fake_align(audio, ref, lang)

    qa = FaBatch(slow_align, initial_window=1.0, min_window=1.0,
                 max_window=4.0, async_worker=True)
    t = time.perf_counter()
    has = [qa.submit(pcm(1.0), "abcdef", float(i), float(i) + 1.0) for i in range(4)]
    submit_sec = time.perf_counter() - t
    r.check("异步 submit 不阻塞（4 次 < 0.1s）", submit_sec < 0.1, f"{submit_sec:.3f}s")
    r.check("窗口已在后台派发", qa.windows >= 1 or qa.inflight() >= 1)
    got = [qa.settle(h) for h in has]
    r.check("异步结果仍按序可得", all(g is not None for g in got))
    qa.close()
    r.check("close 后执行器已释放", qa._executor is None)

    # ── 9. drain 收尾未满窗 ─────────────────────────────────
    q = new(initial=10.0, lo=1.0, hi=20.0)
    h = q.submit(pcm(1.0), "abc", 20.0, 21.0)
    r.check("未满窗仍在队列", q.pending_sec() == 1.0)
    q.drain()
    r.check("drain 派发残余", q.pending_sec() == 0.0 and q.inflight() == 1)
    out = q.settle(h)
    r.check("drain 后结果可取", out is not None and "".join(x[2] for x in out) == "abc")

    # ── 10. settle 时窗口未满也能取（主动派发，不死等）──────
    q = new(initial=10.0, lo=1.0, hi=20.0)
    h = q.submit(pcm(1.0), "xy", 0.0, 1.0)
    out = q.settle(h)
    r.check("settle 主动派发未满窗", out is not None and q.pending_sec() == 0.0)

    # ── 11. 背压 ────────────────────────────────────────────
    qa = FaBatch(slow_align, initial_window=1.0, min_window=1.0,
                 max_window=1.0, async_worker=True, max_windows=2)
    for i in range(6):
        qa.submit(pcm(1.0), "abcdef", float(i), float(i) + 1.0)
    r.check("背压后未收窗口不超过上限", qa.inflight() <= 2, str(qa.inflight()))
    qa.close()

    # ── 12. stats ───────────────────────────────────────────
    q = new(initial=1.0, lo=1.0, hi=4.0)
    q.submit(pcm(2.0), "abc", 0.0, 2.0)
    st = q.stats()
    r.check("stats 字段齐全",
            all(k in st for k in ("windows", "failures", "windowSec",
                                  "pendingSec", "inflight", "observations")))

    # ── 13. 构造校验 ────────────────────────────────────────
    try:
        FaBatch("not callable")
        r.check("非可调用 align_fn 报错", False)
    except TypeError:
        r.check("非可调用 align_fn 报错", True)
    try:
        FaBatch(fake_align, min_window=5.0, max_window=1.0)
        r.check("窗口上下限倒置报错", False)
    except ValueError:
        r.check("窗口上下限倒置报错", True)

    # ── 14. 跨段单调 ────────────────────────────────────────
    q = new(initial=1.0, lo=1.0, hi=4.0)
    h1 = q.submit(pcm(1.0), "一二三四", 0.0, 1.0)
    # 第二段起点比第一段末行早 0.5 秒（模拟 VAD 分组重叠）
    h2 = q.submit(pcm(1.0), "五六七八", 0.5, 1.5)
    a, b = q.settle(h1), q.settle(h2)
    if a and b:
        r.check("第二段首行不早于第一段末行",
                b[0][0] >= a[-1][1] - 1e-6,
                f"{b[0][0]:.3f} vs {a[-1][1]:.3f}")
    else:
        r.check("跨段单调（数据不足）", False)

    return 0 if r.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
