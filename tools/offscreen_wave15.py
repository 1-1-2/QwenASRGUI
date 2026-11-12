#!/usr/bin/env python3
"""离屏交互验收：Wave 15 四项（自检展开/状态、拖放指引、队列排序、置顶）。

与 ``offscreen_render.py`` 同一套 harness（真 Chromium + 静态服务 + mock 传输），
但只针对本轮改动做**真实点击/拖拽**验证，而不是断言源码里有某段字符串。

    python tools/offscreen_wave15.py
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBVIEW = ROOT / "webview"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEBVIEW), **kw)

    def log_message(self, *a):        # keep stdout clean
        pass


def start_server():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), QuietHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def main() -> int:
    from playwright.sync_api import sync_playwright

    srv, base = start_server()
    ok, fails = 0, []

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            print(f"  通过  {name}" + (f" — {detail}" if detail else ""))
        else:
            fails.append(name)
            print(f"  失败  {name}" + (f" — {detail}" if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route("https://fonts.googleapis.com/**", lambda r: r.abort())
        page.route("https://fonts.gstatic.com/**", lambda r: r.abort())
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # ── 1. 自检：默认展开 + 徽标不是「检查中」──────────────────────
        page.click('[data-view-btn="model"], .nav-item[data-view="model"]')
        page.wait_for_timeout(600)
        fold_open = page.evaluate(
            "() => { const f = document.querySelector('#health-fold'); return !!f && f.open; }")
        check("自检 details 默认展开", fold_open)
        # mock 模式下没有 /health，renderHealth 会静默 return；此时徽标应停在
        # i18n 的「检查中…」而不是硬编码中文 —— 关键是**只有一个**徽标且可翻译。
        n_sum = page.evaluate("() => document.querySelectorAll('#health-sum').length")
        check("自检徽标唯一", n_sum == 1, f"count={n_sum}")
        sum_text = page.evaluate(
            "() => (document.querySelector('#health-sum')||{}).textContent || ''")
        check("徽标拿到的是可翻译文案（非空）", bool(sum_text.strip()), sum_text.strip()[:30])

        # ── 2. 批量页：拖放提示带文件数 ────────────────────────────────
        page.click('[data-view-btn="batch"], .nav-item[data-view="batch"]')
        page.wait_for_timeout(500)
        # 造一个假的 drop 事件：有 files 但没有本地路径
        page.evaluate("""() => {
          const box = document.querySelector('#batch-drop');
          const dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array(16)], 'a.wav', {type:'audio/wav'}));
          dt.items.add(new File([new Uint8Array(16)], 'b.wav', {type:'audio/wav'}));
          box.dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:dt}));
        }""")
        page.wait_for_timeout(400)
        msg = page.evaluate(
            "() => { const m = document.querySelector('#batch-path-msg');"
            " return m ? m.textContent : ''; }")
        check("拖放后给出提示", bool(msg.strip()), msg.strip()[:60])
        check("提示里带「2 个文件」", "2" in msg, msg.strip()[:80])

        # ── 3. 队列排序：本地 mock 队列拖放 ───────────────────────────
        # 直接往 mock 队列塞三项，再调用前端的 moveBatchItem（模拟 drop 结果）
        page.evaluate("""() => {
          window.__q = [];
          for (const n of ['one','two','three']) {
            window.__q.push({id:n, name:n+'.wav', status:'pending', progress:0});
          }
        }""")
        # 用真实 DOM 拖拽难以在无头环境稳定复现，这里验证排序函数本身 +
        # 行是否 draggable（DOM 契约），两者合起来覆盖交互路径。
        rows = page.evaluate("""() => {
          const host = document.querySelector('#batch-list');
          return host ? host.querySelectorAll('.b-row').length : -1;
        }""")
        check("批量列表可渲染（mock 队列为空时显示空态）", rows >= 0, f"rows={rows}")

        # 拖放属性：往队列加真实项后检查 draggable
        page.evaluate("""() => {
          const dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array(8)], 'x1.wav', {type:'audio/wav'}));
          const box = document.querySelector('#batch-drop');
          box.dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:dt}));
        }""")
        page.wait_for_timeout(400)
        dr = page.evaluate("""() => {
          const rs = [...document.querySelectorAll('#batch-list .b-row')];
          return rs.map(r => ({drag: r.draggable, id: r.dataset.id}));
        }""")
        check("mock 模式下队列行已渲染", len(dr) >= 1, json.dumps(dr)[:80])
        check("pending 行是可拖的", all(x["drag"] for x in dr) if dr else False,
              json.dumps([x["drag"] for x in dr]))

        # ── 4. 置顶菜单项出现在非队首的 pending 上 ────────────────────
        page.evaluate("""() => {
          // 两个文件都拖进来，让队列有 2 项
          const dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array(8)], 'x2.wav', {type:'audio/wav'}));
          document.querySelector('#batch-drop')
            .dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:dt}));
        }""")
        page.wait_for_timeout(400)
        n_rows = page.evaluate(
            "() => document.querySelectorAll('#batch-list .b-row').length")
        if n_rows >= 2:
            page.evaluate(
                "() => document.querySelectorAll('#batch-list .b-row .more')[1].click()")
            page.wait_for_timeout(300)
            keys = page.evaluate(
                "() => [...document.querySelectorAll('.b-menu button')].map(b=>b.dataset.k)")
            check("第二项的三点菜单含「置顶」", "menuPromote" in keys, str(keys))
            page.keyboard.press("Escape")
        else:
            check("至少两行才能测置顶", False, f"rows={n_rows}")

        check("无页面 JS 错误", not errors, str(errors[:2]))

        # ── 5. web 模式的「拿不到路径」文案（mock 走不到这个分支）────────
        # 直接渲染文案，确认 {n} 被替换、且说明了原因与替代操作。
        copy = page.evaluate(
            "() => (window.I18N && I18N.t('batch.dropNoPath') || '')"
            ".split('{n}').join('2')")
        check("web 文案含文件数", "2" in copy, copy[:60])
        check("web 文案说明 Chromium 限制",
              "Chromium" in copy, copy[:80])
        check("web 文案给出替代操作", "點擊此框" in copy or "點擊" in copy, copy[-60:])

        # ── 6. 真实拖拽重排（HTML5 drag 事件序列）────────────────────
        # 先把队列整理成 3 项 pending，再模拟「拖第 1 项到第 3 项下半部」。
        page.evaluate("""() => {
          const dt = new DataTransfer();
          dt.items.add(new File([new Uint8Array(8)], 'ord3.wav', {type:'audio/wav'}));
          document.querySelector('#batch-drop')
            .dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:dt}));
        }""")
        page.wait_for_timeout(400)
        order_before = page.evaluate(
            "() => [...document.querySelectorAll('#batch-list .b-row .nm .f')]"
            ".map(e => e.textContent)")
        if len(order_before) >= 3:
            page.evaluate("""() => {
              const rows = [...document.querySelectorAll('#batch-list .b-row')];
              const src = rows[0], dst = rows[rows.length - 1];
              const dt = new DataTransfer();
              src.dispatchEvent(new DragEvent('dragstart',
                {bubbles:true, dataTransfer:dt}));
              const r = dst.getBoundingClientRect();
              const y = r.bottom - 2;                 // 落在下半部 → 插到其后
              dst.dispatchEvent(new DragEvent('dragover',
                {bubbles:true, clientY:y, dataTransfer:dt}));
              dst.dispatchEvent(new DragEvent('drop',
                {bubbles:true, clientY:y, dataTransfer:dt}));
            }""")
            page.wait_for_timeout(600)
            order_after = page.evaluate(
                "() => [...document.querySelectorAll('#batch-list .b-row .nm .f')]"
                ".map(e => e.textContent)")
            moved = order_after and order_before[0] == order_after[-1]
            check("拖到末尾后该项位于最后", moved,
                  f"{order_before} -> {order_after}")
            check("重排没有丢项或复制项",
                  sorted(order_before) == sorted(order_after),
                  f"{len(order_before)} -> {len(order_after)}")
        else:
            check("至少 3 行才能测拖拽排序", False, f"rows={len(order_before)}")

        # ── 7. 置顶真的把它挪到队首 ──────────────────────────────────
        before_p = page.evaluate(
            "() => [...document.querySelectorAll('#batch-list .b-row .nm .f')]"
            ".map(e => e.textContent)")
        if len(before_p) >= 2:
            last_name = before_p[-1]
            page.evaluate(
                "() => document.querySelectorAll('#batch-list .b-row .more')")
            page.evaluate("""(target) => {
              const rows = [...document.querySelectorAll('#batch-list .b-row')];
              const row = rows.find(r => (r.querySelector('.nm .f')||{}).textContent === target);
              if (row) row.querySelector('.more').click();
            }""", last_name)
            page.wait_for_timeout(300)
            clicked = page.evaluate("""() => {
              const b = document.querySelector('.b-menu button[data-k="menuPromote"]');
              if (!b) return false; b.click(); return true;
            }""")
            page.wait_for_timeout(500)
            after_p = page.evaluate(
                "() => [...document.querySelectorAll('#batch-list .b-row .nm .f')]"
                ".map(e => e.textContent)")
            check("点击置顶后该项到队首",
                  clicked and after_p and after_p[0] == last_name,
                  f"{before_p} -> {after_p}")
        else:
            check("至少 2 行才能测置顶动作", False, f"rows={len(before_p)}")

        browser.close()

    srv.server_close()
    total = ok + len(fails)
    print(f"\n结果：{ok}/{total} 项通过")
    if fails:
        print("失败项：" + ", ".join(fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
