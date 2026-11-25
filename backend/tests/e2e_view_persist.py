"""测试：刷新后保留当前页面（localStorage 记忆）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1500, "height": 1000})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(800)

    # 依次切到多个页面，每个都刷新验证
    cases = [
        ("skills", "#sk-list"),
        ("plugins", "#pl-list"),
        ("agents", "#ag-list"),
        ("rag", "#doc-file-name"),
        ("obs", "#main table"),
    ]
    for view, marker in cases:
        pg.locator(f'nav button[data-p="{view}"]').first.click()
        pg.wait_for_selector(marker, state="attached", timeout=15000)
        pg.reload(wait_until="networkidle")
        pg.wait_for_selector(marker, state="attached", timeout=15000)
        assert "on" in (pg.locator(f'nav button[data-p="{view}"]').first.get_attribute("class") or ""), \
            f"{view} 刷新后导航未高亮"
        print(f"✓ 刷新后仍在 {view}")

    # 智能对话也验证
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector("#orch-in", timeout=15000)
    print("✓ 刷新后仍在 智能对话")

    # 全新会话（清空 localStorage）默认回 chat
    pg.evaluate("localStorage.removeItem('aws-view')")
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector("#orch-in", timeout=15000)
    print("✓ 无记忆时默认进入智能对话")

    assert not errs, errs[:3]
    b.close()
print("PASS-VIEW-PERSIST ✓")
