"""验证 Playground 展示 ReAct 工作轨迹（思考→工具→结果）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=180)
U = uuid.uuid4().hex[:6]
a = c.post("/agents", json={"name": f"pg-loop-{U}", "description": "测试工作轨迹"}).json()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="playground"]').first.click()
    pg.wait_for_selector("#pg-in", timeout=15000)
    pg.locator("#pg-ag").select_option(a["id"])
    pg.locator("#pg-in").fill("你好")
    pg.locator("#pg-in").press("Enter")
    pg.wait_for_function("() => !document.querySelector('#pg-body .spin')", timeout=120000)
    pg.wait_for_timeout(500)

    trace = pg.locator("#pg-trace").inner_text()
    assert "工作轨迹" in trace, trace[:300]
    assert "步骤" in trace and ("工具调用" in trace or "最终答案" in trace), trace[:300]
    print("✓ Playground 展示工作轨迹:", trace[:150].replace("\n"," "))

    assert not errs, errs[:3]
    b.close()

for ag in c.get("/agents").json():
    if ag["name"] == a["name"]: c.delete("/agents/"+ag["id"])
print("PASS-PLAYGROUND-TRACE-UI ✓")
