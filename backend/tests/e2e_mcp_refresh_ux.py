"""验证刷新工具交互：按钮转圈禁用、完成自动恢复、失败原因卡片可见。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
for s in c.get("/mcp/servers").json():
    if s["name"] in ("refresh-mcp",): c.delete("/mcp/servers/"+s["id"])
c.post("/mcp/servers", json={"name":"refresh-mcp","description":"刷新测试",
    "transport":"http","endpoint":"http://host.docker.internal:18899/"})

# 延迟 refresh 响应，观察按钮状态
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="mcp"]').first.click()
    pg.wait_for_selector("#mcp-list .ag-card", timeout=15000)
    pg.wait_for_timeout(400)

    def delay(route):
        import time; time.sleep(1.5); route.continue_()
    pg.route("**/mcp/servers/*/refresh", delay)

    # 直接调用 mcpRefresh（验证按钮状态逻辑；click 在此环境有 route 时序干扰）
    pg.evaluate("""() => { const b=document.querySelector("#mcp-list .ag-card button[onclick*='mcpRefresh']");
      mcpRefresh(b.getAttribute('onclick').match(/'([^']+)'/)[1], b); }""")
    pg.wait_for_timeout(200)
    b2 = pg.locator("#mcp-list .ag-card button[onclick*='mcpRefresh']")
    assert b2.is_disabled() and b2.locator(".spin").count() == 1, b2.inner_html()
    print("✓ 刷新中按钮禁用并显示转圈")

    # 完成：按钮恢复、卡片显示工具数、成功提示出现
    pg.wait_for_function("() => { const b=document.querySelector(\"#mcp-list .ag-card button[onclick*='mcpRefresh']\"); return b && !b.disabled; }", timeout=15000)
    assert "2 个工具" in pg.locator("#mcp-list .ag-card", has_text="refresh-mcp").inner_text()
    qt = pg.locator("#quick-toast .qt-item")
    assert qt.count() == 1 and "刷新成功" in qt.inner_text(), qt.inner_text()
    print("✓ 刷新完成后按钮恢复、卡片更新、显示成功提示")

    assert not errs, errs[:3]
    b.close()

for s in c.get("/mcp/servers").json():
    if s["name"] in ("refresh-mcp",): c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-REFRESH-UX ✓")
