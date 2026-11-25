"""验证 MCP 市场前端粘贴 JSON 配置接入。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="mcp"]').first.click()
    pg.wait_for_selector("#mcp-config", timeout=15000)
    assert pg.locator("#mcp-config").count() == 1
    print("✓ 粘贴 JSON 配置框存在")

    cfg = {"mcpServers": {"ui-json-mcp": {"url": "http://host.docker.internal:18899/"}}}
    pg.locator("#mcp-config").fill(json.dumps(cfg))
    pg.locator("button", has_text="解析并接入").click()
    pg.wait_for_function("() => document.querySelector('#mcp-msg')?.textContent?.includes('成功')", timeout=30000)
    pg.wait_for_timeout(500)
    assert pg.locator("#mcp-list .ag-card").count() == 1
    assert "ui-json-mcp" in pg.locator("#mcp-list").inner_text()
    print("✓ 粘贴 JSON 接入成功，卡片显示")

    assert not errs, errs[:3]
    b.close()

for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-JSON-UI ✓")
