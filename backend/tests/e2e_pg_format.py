"""验证 Playground 对话输出格式与智能对话一致（turn/bubble/content 结构）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for a in c.get("/agents").json():
    if a["name"] == "fmt-test": c.delete("/agents/"+a["id"])
c.post("/agents", json={"name": "fmt-test", "description": "格式测试"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(label="fmt-test v1")
    pg.locator("#pg-in").fill("你好")
    pg.locator("#pg-in").press("Enter")
    pg.wait_for_function("() => { const b=document.querySelector('#pg-go'); return b && !b.disabled; }", timeout=120000)
    pg.wait_for_timeout(300)

    # 用户消息用 .turn.user .bubble；AI 用 .turn.ai .content
    assert pg.locator("#pg-body .turn.user .bubble").count() >= 1, "用户消息结构不对"
    assert pg.locator("#pg-body .turn.ai .content").count() >= 1, "AI 消息结构不对"
    # 不应再有旧结构气泡
    assert pg.locator("#pg-body .bubble.me").count() == 0, "仍有旧 bubble.me"
    print("✓ Playground 使用与智能对话一致的结构")
    assert not errs, errs[:3]
    b.close()

for a in c.get("/agents").json():
    if a["name"] == "fmt-test": c.delete("/agents/"+a["id"])
print("PASS-PG-FORMAT ✓")
