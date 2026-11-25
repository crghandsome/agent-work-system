"""测试：Playground 交互与智能对话一致（Enter 发送 / Shift+Enter 换行）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
r = c.post("/agents", json={"name": f"pg-ui-{U}", "description": "Playground 交互测试"})
aid = r.json()["id"]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(aid)
    pg.wait_for_timeout(500)

    # 1. placeholder 与按钮文案
    assert "Enter 发送" in pg.locator("#pg-in").get_attribute("placeholder")
    assert pg.locator("#pg-go").inner_text() == "发送"
    print("✓ 文案与智能对话一致")

    # 2. Shift+Enter 不发送（只换行）
    pg.locator("#pg-in").fill("第一行")
    pg.locator("#pg-in").press("Shift+Enter")
    pg.locator("#pg-in").type("第二行")
    assert "第一行\n第二行" in pg.locator("#pg-in").input_value(), pg.locator("#pg-in").input_value()
    assert pg.locator("#pg-body .bubble.me").count() == 0, "Shift+Enter 不应发送"
    print("✓ Shift+Enter 换行不发送")

    # 3. Enter 发送
    pg.locator("#pg-in").press("Enter")
    pg.wait_for_selector("#pg-body .bubble.me", timeout=5000)
    assert pg.locator("#pg-body .bubble.me").count() == 1
    assert pg.locator("#pg-in").input_value() == ""
    print("✓ Enter 发送消息")

    # 4. 按钮发送仍可用
    pg.wait_for_selector("#pg-trace .card", timeout=120000)
    pg.locator("#pg-in").fill("按钮发送测试")
    pg.locator("#pg-go").click()
    pg.wait_for_timeout(500)
    assert pg.locator("#pg-body .bubble.me").count() == 2
    print("✓ 按钮发送正常")

    assert not errs, errs[:3]
    b.close()

c.delete(f"/agents/{aid}")
print("PASS-PLAYGROUND-KEYS ✓")
