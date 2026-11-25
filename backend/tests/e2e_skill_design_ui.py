"""前端测试：智能对话中主 Agent 经 A2A 委托 Skill 设计子 Agent。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(800)
    # 新建会话，避免历史会话的残留队列阻塞
    pg.locator(".sess-new").click()
    pg.wait_for_timeout(800)

    # 发起设计 Skill 的请求
    pg.locator("#orch-in").fill("帮我设计一个 skill，用于把技术文档翻译成简洁的中文摘要")
    pg.locator("#orch-go").click()
    # 等待委托轨迹出现（主 Agent 调用了 skill-designer）
    pg.wait_for_selector(".deli", timeout=120000)
    deli = pg.locator(".deli").last.inner_text()
    print("✓ 委托轨迹:", deli.replace("\n", " ")[:80])
    assert "skill-designer" in deli or "已委托" in deli, deli

    # 等待最终回复（内容不再是排队/处理中占位）
    pg.wait_for_function(
        "() => { const els=[...document.querySelectorAll('.turn.ai .content')];"
        " const c=els[els.length-1];"
        " return c && c.textContent.length>10 && !c.querySelector('.cursor')"
        "   && !c.textContent.includes('排队中') && !c.textContent.includes('处理中'); }", timeout=180000)
    reply = pg.locator(".turn.ai .content").last.inner_text()
    print("✓ 回复:", reply[:120].replace("\n", " "))
    assert "skill" in reply.lower(), reply

    assert not errs, errs[:3]
    b.close()
print("PASS-SKILL-DESIGN ✓")
