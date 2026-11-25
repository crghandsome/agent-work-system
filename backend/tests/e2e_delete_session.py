"""回归：会话删除的缓存及时更新（快速连续删除不丢失）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(800)

    for _ in range(4):
        c.post("/orchestrator/sessions")
    before = len(c.get("/orchestrator/sessions").json())
    print("✓ 准备会话，当前总数:", before)

    # 快速连续删除 3 个（不等待列表刷新完成）
    for _ in range(3):
        pg.locator(".sess-item .del").first.click(force=True)
        pg.wait_for_selector(".modal-mask", timeout=5000)
        pg.locator("#modal-ok").last.click()
        pg.wait_for_timeout(150)
    pg.wait_for_timeout(1500)

    after = len(c.get("/orchestrator/sessions").json())
    assert before - after == 3, f"快速连删丢失：before={before} after={after}"
    print("✓ 快速连续删除 3 个全部成功（缓存及时更新）")

    # 删除当前会话后，界面还有一个可用会话
    assert pg.locator("#orch-in").count() == 1, "删除当前会话后输入框消失"
    assert pg.evaluate("!!orch.id"), "删除当前会话后未切换到有效会话"
    print("✓ 删除当前会话后自动切换到有效会话")

    assert not errors, errors[:3]
    b.close()
print("PASS-DELETE-SESSION ✓")
