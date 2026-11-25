"""测试：A2A 对外地址展示与 Agent Card URL 生成。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
u = c.get("/a2a/public-url").json()
assert "tasks_url" in u and "hint" in u
print("✓ public-url 端点:", u["configured"])

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="a2a"]').first.click()
    pg.wait_for_selector("#my-a2a-url", timeout=15000)
    pg.wait_for_timeout(600)
    txt = pg.locator("#my-a2a-url").inner_text()
    if u["configured"]:
        assert u["tasks_url"] in txt
        print("✓ 页面显示任务端点:", u["tasks_url"])
    else:
        assert "AWS_A2A_PUBLIC_BASE_URL" in txt and "localhost" not in txt
        print("✓ 页面提示配置外部地址（不再误导用 localhost）")
    # 注册 Peer 说明仍存在
    assert "管理员提供" in pg.locator("#main").inner_text()
    assert not errs, errs[:3]
    b.close()
print("PASS-A2A-PUBLIC-URL ✓")
