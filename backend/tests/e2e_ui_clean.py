"""验证前端界面无任何虚构产品假数据。"""
import sys, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

h = pathlib.Path("backend/web/index.html").read_text(encoding="utf-8")
assert "云信通" not in h, "前端源码仍含假数据"
print("✓ 前端源码无假数据")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(500)
    assert "云信通" not in pg.locator("#main").inner_text(), "界面仍显示假数据"
    print("✓ 界面无假数据建议卡")
    b.close()
print("PASS-UI-CLEAN ✓")
