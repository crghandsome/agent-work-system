"""前端测试：插件市场（简介/启停/安装/修改/卸载）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]
KEY = f"ui-plugin-{U}"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="plugins"]').first.click()
    pg.wait_for_selector("#pl-list .pl-card", timeout=15000)
    pg.wait_for_timeout(600)

    # 1. 卡片有简介和来源
    first = pg.locator(".pl-card").first
    txt = first.inner_text()
    assert "内置" in txt or "市场" in txt, txt
    assert len(txt) > 20, "卡片应有功能简介"
    print("✓ 插件卡片显示简介与来源")

    # 2. 安装市场插件
    pg.locator("#pl-key").fill(KEY)
    pg.get_by_role("button", name="安装").first.click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    assert pg.locator("#pl-i-name").count() == 1
    pg.locator("#pl-i-desc").fill("这是一个用于前端测试的插件说明")
    pg.locator("#modal-ok").click()
    pg.wait_for_function("() => [...document.querySelectorAll('.pl-card')].some(c => c.textContent.includes('ui-plugin-'))", timeout=10000)
    print("✓ 市场安装成功")

    # 3. 启停
    card = pg.locator(f'.pl-card[data-key="{KEY}"]')
    sw = card.locator(".switch")
    assert "on" in (sw.get_attribute("class") or "")
    sw.click()
    pg.wait_for_timeout(500)
    assert "on" not in (card.locator(".switch").get_attribute("class") or ""), "停用失败"
    card.locator(".switch").click()
    pg.wait_for_timeout(500)
    assert "on" in (card.locator(".switch").get_attribute("class") or ""), "启用失败"
    print("✓ 停用/启用正常")

    # 4. 修改
    card.locator(".pl-act", has_text="修改").click()
    pg.wait_for_selector("#pl-e-name", timeout=5000)
    pg.locator("#pl-e-name").fill("测试插件改名")
    pg.locator("#pl-e-desc").fill("修改后的功能简介")
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(600)
    assert "测试插件改名" in pg.locator(f'.pl-card[data-key="{KEY}"]').inner_text()
    print("✓ 用户插件可修改")

    # 5. 卸载
    card.locator(".pl-act", has_text="卸载").click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(700)
    assert pg.locator(f'.pl-card[data-key="{KEY}"]').count() == 0, "卸载失败"
    print("✓ 插件可卸载")

    # 6. 内置插件也有卸载按钮（所有插件都可卸载）
    bi = pg.locator('.pl-card[data-key="memory_simple"]')
    assert bi.locator(".pl-act", has_text="卸载").count() == 1
    print("✓ 内置插件也有卸载按钮")

    assert not errs, errs[:3]
    b.close()
print("PASS-PLUGIN-UI ✓")
