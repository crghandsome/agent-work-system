"""测试：子 Agent URL 为空；Agent Card 覆盖弹窗可开关。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
n = c.post("/agents", json={"name": f"cardov-{U}", "description": "覆盖测试"}).json()["name"]
d = c.get(f"/a2a/cards/{n}").json()
assert d["url"] == "" and d["exposure"] == "internal", d
print("✓ 子 Agent URL 为空")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", timeout=15000)
    pg.wait_for_timeout(800)
    card = pg.locator(".ag-card", has_text=n).first
    card.locator("button", has_text="Agent Card").click()
    pg.wait_for_selector(".card-overlay pre", timeout=10000)
    geo = pg.evaluate("""() => {
      const c = [...document.querySelectorAll('.ag-card')].find(e=>e.textContent.includes('cardov-'));
      const o = c.querySelector('.card-overlay');
      const cr=c.getBoundingClientRect(), or_=o.getBoundingClientRect();
      return {cw:cr.width, ch:cr.height, ow:or_.width, oh:or_.height};
    }""")
    assert abs(geo["cw"]-geo["ow"]) < 3 and abs(geo["ch"]-geo["oh"]) < 3, geo
    print("✓ 覆盖层与信息卡同尺寸", geo)
    # 关闭恢复
    pg.locator(".co-close").first.click()
    pg.wait_for_timeout(300)
    assert pg.locator(".card-overlay").count() == 0
    assert "覆盖测试" in pg.locator(".ag-card", has_text=n).first.inner_text()
    print("✓ 点 × 退出后恢复信息卡")
    assert not errs, errs[:3]
    b.close()

for a in c.get("/agents").json():
    if a["name"] == n: c.delete(f"/agents/{a['id']}")
print("PASS-CARD-OVERLAY ✓")
