"""前端测试：组装 Agent 时装意图插件，运行自动执行（无需手写 workflow）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]
NAME = f"auto-ui-{U}"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)

    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-plugin-menu", timeout=15000)
    pg.wait_for_timeout(800)

    # 从插件菜单选择意图插件（新 UI：菜单点击，不再是 checkbox）
    pg.locator("#ag-plugin-q").fill("intent")
    pg.wait_for_timeout(300)
    pg.locator("#ag-plugin-list .menu-item", has_text="intent_keyword").first.click()
    pg.locator("#ag-n").fill(NAME)
    pg.locator("#ag-d").fill("自动挂载验证")
    pg.locator("button", has_text="校验并发布").first.click()
    pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('发布成功')", timeout=15000)
    print("✓ 组装时勾选意图插件并发布")

    # Playground 运行，检查轨迹含 intent
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=10000)
    pg.locator("#pg-in").fill("你好")
    pg.locator("#pg-go").click()
    pg.wait_for_selector("#pg-trace .card", timeout=120000)
    trace = pg.locator("#pg-trace").inner_text()
    assert "intent" in trace, f"运行轨迹无 intent: {trace[:200]}"
    print("✓ 运行轨迹显示 intent 步骤自动执行")

    assert not errs, errs[:3]
    b.close()
print("PASS-AUTO-MOUNT-UI ✓")
