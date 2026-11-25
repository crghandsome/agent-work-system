"""测试：Agent Card 按主/子 Agent 区分 URL；查看入口在 Agent 卡片。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
# 子 Agent
sub = c.post("/agents", json={"name": f"card-sub-{U}", "description": "子Agent"}).json()["name"]
# 主 Agent（orchestrator）
main = c.post("/agents", json={"name": f"card-main-{U}", "description": "主Agent", "is_orchestrator": True}).json()["name"]

d1 = c.get(f"/a2a/cards/{sub}").json()
d2 = c.get(f"/a2a/cards/{main}").json()
assert d1["exposure"] == "internal" and d1["url"] == "", d1
assert d2["exposure"] == "public" and d2["url"] == "", d2
print("✓ 子Agent/main URL 均为空（内部通信不依赖 URL）")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", timeout=15000)
    pg.wait_for_timeout(800)
    card = pg.locator(".ag-card", has_text=sub).first
    assert card.locator("button", has_text="Agent Card").count() == 1
    card.locator("button", has_text="Agent Card").click()
    pg.wait_for_selector(".card-overlay pre", timeout=10000)
    txt = pg.locator(".card-overlay pre").first.inner_text()
    assert '"protocol": "a2a/1.0"' in txt and '"exposure": "internal"' in txt, txt[:300]
    print("✓ Agent Card 在 Agent 卡片内覆盖查看")
    # A2A 页面不再有 card-agent
    pg.locator('nav button[data-p="a2a"]').first.click()
    pg.wait_for_selector("#trace-select", timeout=10000)
    assert pg.locator("#card-agent").count() == 0
    print("✓ A2A 页面无 Agent Card 查看器")
    assert not errs, errs[:3]
    b.close()

for n in (sub, main):
    for a in c.get("/agents").json():
        if a["name"] == n: c.delete(f"/agents/{a['id']}")
print("PASS-CARD-PLACEMENT ✓")
