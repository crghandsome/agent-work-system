"""验证：插件运行机制说明、所有插件可改可删、key 概念、设计指南。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="plugins"]').first.click()
    pg.wait_for_selector("#pl-list .pl-card", timeout=15000)
    pg.wait_for_timeout(800)

    # 1. 设计指南入口 + 内容
    pg.locator("button", has_text="查看设计指南").click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    guide = pg.locator(".modal").inner_text()
    assert "plugin.json" in guide and "behavior" in guide and "key" in guide, guide[:200]
    print("✓ 设计指南：清单示例、behavior、字段说明")
    pg.locator(".modal-mask").last.locator("#modal-ok").click()
    pg.wait_for_timeout(300)

    # 2. key 概念说明
    card = pg.locator('.pl-card[data-key="memory_simple"]')
    assert "唯一标识" in card.inner_text(), "卡片未说明 key 含义"
    print("✓ 卡片说明 key 是唯一标识")

    # 3. 运行机制详情
    card.locator(".pl-act", has_text="详情/机制").click()
    pg.wait_for_timeout(300)
    detail = pg.locator(".modal").last.inner_text()
    assert "运行机制" in detail and "LLM" in detail, detail[:300]
    print("✓ 详情展示运行机制（如何驱动 LLM）")
    pg.locator(".modal-mask").last.locator("#modal-ok").click()
    pg.wait_for_timeout(300)

    # 4. 内置插件可修改与删除
    card.locator(".pl-act", has_text="修改").click()
    pg.wait_for_timeout(300)
    assert pg.locator("#pl-e-mech").count() == 1, "修改框缺运行机制字段"
    pg.locator("#pl-e-desc").fill("改后的内存插件简介")
    pg.locator("#pl-e-mech").fill("改后的运行机制说明")
    pg.locator(".modal-mask").last.locator("#modal-ok").click()
    pg.wait_for_timeout(600)
    assert "改后的内存插件简介" in pg.locator('.pl-card[data-key="memory_simple"]').inner_text()
    print("✓ 内置插件可修改（含运行机制）")

    # 5. 删除内置插件（验证后有卸载按钮）
    assert card.locator(".pl-act", has_text="卸载").count() == 1
    print("✓ 内置插件有卸载按钮")

    assert not errs, errs[:3]
    b.close()
print("PASS-PLUGIN-DETAILS ✓")
