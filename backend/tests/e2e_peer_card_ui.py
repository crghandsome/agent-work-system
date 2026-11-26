"""验证前端 Peer 自动发现：URL→识别回显→注册→卡片显示能力。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for p in c.get("/a2a/peers").json(): c.delete("/a2a/peers/"+p["id"])

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="a2a"]').first.click()
    pg.wait_for_selector("#pe-u", timeout=15000)
    pg.wait_for_timeout(500)

    # 填 URL → 自动识别
    pg.locator("#pe-u").fill("http://host.docker.internal:18898")
    pg.locator("button", has_text="自动识别 Agent 信息").click()
    pg.wait_for_selector("#pe-preview .card", timeout=15000)
    prev = pg.locator("#pe-preview").inner_text()
    assert "外部客服机器人" in prev and "订单查询" in prev, prev[:300]
    print("✓ 自动识别回显名称与能力")

    # 注册（不手填名称）
    pg.locator("button", has_text="注册").first.click()
    pg.wait_for_function("() => document.querySelector('#pe-msg')?.textContent?.includes('注册成功')", timeout=15000)
    pg.wait_for_timeout(400)
    assert pg.locator("#pe-list .ag-card", has_text="外部客服机器人").count() == 1
    print("✓ 注册成功，卡片显示自动识别的 Peer")

    # 卡片显示能力
    card = pg.locator("#pe-list .ag-card", has_text="外部客服机器人").first
    assert "自动识别" in card.inner_text()
    card.locator("summary").click()
    pg.wait_for_timeout(200)
    assert "订单查询" in card.inner_text() and "退款指引" in card.inner_text()
    print("✓ 卡片展示自动发现的能力清单")

    assert not errs, errs[:3]
    b.close()

for p in c.get("/a2a/peers").json(): c.delete("/a2a/peers/"+p["id"])
print("PASS-PEER-CARD-UI ✓")
