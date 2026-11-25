"""验证：切回正在执行的会话时，能看到进行中的任务消息（进度重建）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(600)

    # 构造一个"后台任务进行中"的快照（模拟切到别的会话后任务仍在跑）
    pg.evaluate("""() => {
      window.__testSid = orch.id;
      window.__testMsg = {id: 'msg_fake', text: '帮我分析季度报告', state: 'running',
        events: [{type:'queued'},{type:'status',stage:'thinking'},
                 {type:'content',delta:'我正在分析'},{type:'content',delta:'报告数据'}]};
    }""")
    # 直接调用重建函数，立即断言渲染（async 函数在第一个 await 前同步执行了 DOM 插入）
    pg.evaluate("() => { const b=document.querySelector('#orch-body'); b.innerHTML=''; orchResumeRunning(window.__testSid, window.__testMsg); }")
    body = pg.locator("#orch-body").inner_text()  # 立即读取，赶在重连失败拉历史前
    assert "帮我分析季度报告" in body, body[:300]
    assert "我正在分析报告数据" in body, body[:300]
    print("✓ 切回执行中会话：重建用户输入+进行中的回复进度")

    assert not errs, errs[:3]
    b.close()
print("PASS-RESUME-PROGRESS ✓")
