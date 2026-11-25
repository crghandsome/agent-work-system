"""验证 MCP 市场：无手动表单、卡片 switch 启停。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
c.post("/mcp/servers", json={"name":"switch-mcp","description":"计算工具",
    "transport":"http","endpoint":"http://host.docker.internal:18899/"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="mcp"]').first.click()
    pg.wait_for_selector("#mcp-config", timeout=15000)
    pg.wait_for_timeout(500)

    # 无手动表单
    assert pg.locator("#mcp-name").count() == 0 and pg.locator("#mcp-transport").count() == 0
    print("✓ 手动添加表单已移除")

    # 卡片 switch 启停（和插件同款）
    card = pg.locator("#mcp-list .ag-card").first
    sw = card.locator("button.switch")
    assert sw.count() == 1 and "on" in (sw.get_attribute("class") or "")
    print("✓ 卡片使用 switch 开关（当前启用）")

    sw.click()  # 停用
    pg.wait_for_timeout(1200)
    print("后端 enabled:", c.get("/mcp/servers").json()[0]["enabled"])
    sw2 = pg.locator("#mcp-list .ag-card").first.locator("button.switch")
    print("switch class:", sw2.get_attribute("class"))
    assert c.get("/mcp/servers").json()[0]["enabled"] is False
    assert "on" not in (sw2.get_attribute("class") or "")
    print("✓ 点击 switch 停用成功")

    sw2.click()  # 再启用
    pg.wait_for_timeout(500)
    assert c.get("/mcp/servers").json()[0]["enabled"] is True
    print("✓ 点击 switch 启用成功")

    assert not errs, errs[:3]
    b.close()

for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-SWITCH ✓")
