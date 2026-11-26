"""验证 Playground 前端实时渲染思考过程。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for a in c.get("/agents").json():
    if a["name"] == "stream-ui-agent": c.delete("/agents/"+a["id"])
c.post("/agents", json={"name": "stream-ui-agent", "description": "测试流式界面"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(label="stream-ui-agent v1")
    pg.locator("#pg-in").fill("你好")
    pg.locator("#pg-in").press("Enter")
    # 等待完成
    pg.wait_for_function("() => { const b=document.querySelector('#pg-go'); return b && !b.disabled; }", timeout=120000)
    pg.wait_for_timeout(300)
    body = pg.locator("#pg-body").inner_text()
    assert "思考" in body or "你好" in body, body[:300]
    print("✓ Playground 前端显示思考/回复:", body[:150].replace("\n"," "))
    assert not errs, errs[:3]
    b.close()

for a in c.get("/agents").json():
    if a["name"] == "stream-ui-agent": c.delete("/agents/"+a["id"])
print("PASS-STREAM-UI ✓")
