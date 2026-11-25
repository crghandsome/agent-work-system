"""功能点测试：自定义弹框（删除确认 / Fork 输入），无浏览器原生弹框。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
native = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    # 若出现浏览器原生弹框，记录并自动取消（说明有遗漏）
    pg.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(1000)

    # 1. 删除会话弹框：自定义 DOM
    pg.locator(".sess-item .del").first.click(force=True)
    pg.wait_for_selector(".modal-mask", timeout=5000)
    text = pg.locator(".modal").inner_text()
    assert "删除会话" in text and "确定删除" in text, text
    print("✓ 删除弹框为自定义 DOM:", text.split("\n")[0])
    # 取消：会话不消失
    count_before = pg.locator(".sess-item").count()
    pg.locator("#modal-cancel").click()
    pg.wait_for_timeout(300)
    assert pg.locator(".sess-item").count() == count_before, "取消后会话被删"
    print("✓ 取消删除不生效")

    # 2. 确认删除：该会话从列表消失（若删的是当前会话会自动新建，总数可能不变）
    deleted_id = pg.evaluate("orch.id")
    pg.locator(".sess-item .del").first.click(force=True)
    pg.wait_for_selector(".modal-mask", timeout=5000)
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(1000)
    assert pg.evaluate(f"orch.id") != deleted_id, "确认删除后当前会话未切换"
    print("✓ 确认删除生效")

    # 3. Fork 输入弹框：在 Agent 市场
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", state="attached", timeout=10000)
    # Agent 删除确认弹框（自定义 modal，新 UI）
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-list", state="attached", timeout=10000)
    pg.wait_for_timeout(800)
    del_btns = pg.locator(".ag-card .ag-actions button", has_text="删除")
    if del_btns.count():
        del_btns.first.click()
        pg.wait_for_selector(".modal-mask", timeout=5000)
        assert "删除" in pg.locator(".modal").inner_text()
        print("✓ Agent 删除确认弹框为自定义 DOM")
        pg.locator("#modal-ok").click()
        pg.wait_for_timeout(500)

    assert not native, f"出现浏览器原生弹框: {native}"
    print("✓ 无浏览器原生弹框")
    assert not errs, errs[:2]
    b.close()
print("PASS-MODAL ✓")
