"""验证 MCP 市场前端：页面、接入表单、卡片展示工具。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
# 通过 API 接入一个（前端接入表单另测）
c.post("/mcp/servers", json={"name":"ui-mcp","description":"计算工具",
    "transport":"http","endpoint":"http://host.docker.internal:18899/"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="mcp"]').first.click()
    pg.wait_for_selector("#mcp-config", timeout=15000)
    pg.wait_for_timeout(600)
    # 接入区只有 JSON 配置框（手动表单已移除）
    assert pg.locator("#mcp-config").count() == 1
    assert pg.locator("#mcp-name").count() == 0
    print("✓ MCP 市场页面（JSON 配置框，无手动表单）")

    # 卡片展示
    assert pg.locator("#mcp-list .ag-card").count() == 1
    card = pg.locator("#mcp-list .ag-card").first
    assert "ui-mcp" in card.inner_text()
    assert "2 个工具" in card.inner_text()
    card.locator("summary").click()
    pg.wait_for_timeout(300)
    tools_text = card.inner_text()
    assert "calculate" in tools_text and "get_weather" in tools_text
    print("✓ 卡片展示功能介绍与工具清单（calculate、get_weather）")

    assert not errs, errs[:3]
    b.close()

for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-UI ✓")
