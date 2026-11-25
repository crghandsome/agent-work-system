"""测试：Playground 对话切页面保留、切换 Agent 才切换对话。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=120)
U = uuid.uuid4().hex[:6]
a1 = c.post("/agents", json={"name": f"pg-persist-a-{U}", "description": "Agent A"}).json()["id"]
a2 = c.post("/agents", json={"name": f"pg-persist-b-{U}", "description": "Agent B"}).json()["id"]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(a1)
    pg.wait_for_timeout(400)

    # 1. 给 Agent A 发消息
    pg.locator("#pg-in").fill("这是 Agent A 的测试消息")
    pg.locator("#pg-in").press("Enter")
    pg.wait_for_selector("#pg-body .bubble.me", timeout=5000)
    assert "这是 Agent A 的测试消息" in pg.locator("#pg-body").inner_text()
    print("✓ Agent A 对话已建立")

    # 2. 切到其他页面再回来 → 对话保留
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector("#sk-list", state="attached", timeout=10000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=10000)
    pg.wait_for_timeout(500)
    assert "这是 Agent A 的测试消息" in pg.locator("#pg-body").inner_text(), "切页面后对话丢失"
    assert pg.locator("#pg-ag").input_value() == a1
    print("✓ 切页面后 Playground 对话保留")

    # 3. 切换到 Agent B → 对话切换（不显示 A 的消息）
    pg.locator("#pg-ag").select_option(a2)
    pg.wait_for_timeout(400)
    assert "这是 Agent A 的测试消息" not in pg.locator("#pg-body").inner_text(), "切换 Agent 后仍显示 A 的对话"
    print("✓ 切换 Agent 后对话切换")

    # 4. 切回 Agent A → 原对话还在
    pg.locator("#pg-ag").select_option(a1)
    pg.wait_for_timeout(400)
    assert "这是 Agent A 的测试消息" in pg.locator("#pg-body").inner_text(), "切回 A 对话丢失"
    print("✓ 切回 Agent A 原对话恢复")

    assert not errs, errs[:3]
    b.close()

for aid in (a1, a2):
    c.delete(f"/agents/{aid}")
print("PASS-PLAYGROUND-PERSIST ✓")
