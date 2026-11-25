"""验证 Playground 测试 Agent 时显示加载转圈（与智能对话一致）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
a = c.post("/agents", json={"name": f"pg-spin-{U}", "description": "转圈测试"}).json()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(a["id"])

    # 延迟 playground 响应
    def delay(route):
        import time; time.sleep(2)
        route.continue_()
    pg.route("**/agents/*/playground", delay)

    pg.locator("#pg-in").fill("你好")
    pg.locator("#pg-in").press("Enter")

    # 思考中：转圈出现
    pg.wait_for_selector("#pg-body .spin", timeout=5000)
    assert "Agent 思考中" in pg.locator("#pg-body").inner_text()
    print("✓ 思考中显示转圈（与智能对话同款 .spin）")

    # 完成：等转圈消失（响应回来后 pending 被移除）
    pg.wait_for_function("() => !document.querySelector('#pg-body .spin')", timeout=30000)
    assert pg.locator("#pg-body .spin").count() == 0
    assert pg.locator("#pg-body .bubble.ai").count() >= 1
    print("✓ 完成后转圈消失，回复显示")

    assert not errs, errs[:3]
    b.close()

for ag in c.get("/agents").json():
    if ag["name"] == a["name"]: c.delete(f"/agents/{ag['id']}")
print("PASS-PLAYGROUND-SPIN ✓")
