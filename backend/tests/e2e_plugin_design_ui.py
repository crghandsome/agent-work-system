"""前端测试：智能对话中开发插件（委托轨迹 + 结果展示）。"""
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
    pg.locator(".sess-new").click()
    pg.wait_for_timeout(600)

    pg.locator("#orch-in").fill("帮我开发一个插件，识别用户输入里的紧急关键词，比如加急、马上、紧急")
    pg.locator("#orch-go").click()

    # 委托轨迹出现
    pg.wait_for_selector(".deli", timeout=180000)
    deli = pg.locator(".deli").last.inner_text()
    print("✓ 委托轨迹:", deli.replace("\n", " ")[:80])
    assert "plugin-designer" in deli, deli

    # 最终回复
    pg.wait_for_function(
        "() => { const els=[...document.querySelectorAll('.turn.ai .content')];"
        " const c=els[els.length-1];"
        " return c && c.textContent.length>20 && !c.querySelector('.cursor')"
        "   && !c.textContent.includes('处理中'); }", timeout=180000)
    reply = pg.locator(".turn.ai .content").last.inner_text()
    print("✓ 回复:", reply[:140].replace("\n", " "))
    assert "插件" in reply, reply

    assert not errs, errs[:3]
    b.close()
print("PASS-PLUGIN-DESIGN-UI ✓")
