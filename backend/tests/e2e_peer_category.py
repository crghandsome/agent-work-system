"""测试：外部 Peer 简介、自动分类、按分类分组展示、删除。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
# 后端：分类
r1 = c.post("/a2a/peers", json={"name": f"peer-a-{U}", "base_url": "https://a.example", "description": "负责文档写作与报告生成的外部Agent"})
r2 = c.post("/a2a/peers", json={"name": f"peer-b-{U}", "base_url": "https://b.example", "description": "负责代码审查和重构的外部Agent", "category": "编程"})
assert r1.json()["category"] == "写作", r1.json()
assert r2.json()["category"] == "编程", r2.json()
print("✓ 后端自动分类与自定义分类")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="a2a"]').first.click()
    pg.wait_for_selector("#pe-desc", timeout=15000)
    pg.wait_for_timeout(800)
    # 分组显示
    txt = pg.locator("#pe-list").inner_text()
    assert "写作" in txt and "编程" in txt and "文档写作" in txt, txt[:300]
    print("✓ Peer 按分类分组并展示简介")

    # 注册带简介
    pg.locator("#pe-n").fill(f"peer-ui-{U}")
    pg.locator("#pe-u").fill("https://ui.example")
    pg.locator("#pe-desc").fill("用于数据分析的外部Agent")
    pg.locator("button", has_text="注册").click()
    pg.wait_for_function("() => document.querySelector('#pe-msg')?.textContent?.includes('注册成功')", timeout=10000)
    pg.wait_for_timeout(500)
    assert "数据分析" in pg.locator("#pe-list").inner_text()
    print("✓ 注册表单填写简介并展示")

    # 删除
    ps = c.get("/a2a/peers").json()
    target = next(p for p in ps if p["name"] == f"peer-ui-{U}")
    card = pg.locator(".ag-card", has_text=f"peer-ui-{U}").first
    card.locator("button", has_text="删除").click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(500)
    assert not any(x["name"] == f"peer-ui-{U}" for x in c.get("/a2a/peers").json())
    print("✓ 删除 Peer")
    assert not errs, errs[:3]
    b.close()

for x in c.get("/a2a/peers").json():
    if x["name"].startswith(f"peer-"):
        c.delete(f"/a2a/peers/{x['id']}")
print("PASS-PEER-CATEGORY ✓")
