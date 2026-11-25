"""测试：提示自动消失、删除后提示清除、重新组装/团队取消按钮。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=120)
U = uuid.uuid4().hex[:6]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#tm-list", timeout=15000)
    pg.wait_for_timeout(1000)

    # 1. 创建团队 → 立即删除 → 成功提示消失
    tn = f"cache-team-{U}"
    pg.locator("#tm-n").fill(tn)
    pg.locator("button", has_text="创建团队").click()
    pg.wait_for_function("() => document.querySelector('#tm-msg')?.textContent?.includes('团队创建成功')", timeout=10000)
    print("✓ 创建团队提示出现")
    # 删除该团队
    card = pg.locator("#tm-list .ag-card", has_text=tn).first
    card.locator("button", has_text="删除团队").click()
    pg.wait_for_selector(".modal-mask", timeout=5000)
    pg.locator("#modal-ok").click()
    pg.wait_for_timeout(600)
    tm_msg = pg.locator("#tm-msg").inner_text()
    assert "团队创建成功" not in tm_msg, f"删除后仍显示创建成功: {tm_msg}"
    print("✓ 删除团队后旧提示已清除")

    # 2. 提示自动消失（5秒 deadline）
    pg.locator("#tm-n").fill(f"cache-team2-{U}")
    pg.locator("button", has_text="创建团队").click()
    pg.wait_for_function("() => document.querySelector('#tm-msg')?.textContent?.includes('团队创建成功')", timeout=10000)
    pg.wait_for_timeout(5500)
    assert "团队创建成功" not in pg.locator("#tm-msg").inner_text(), "提示未自动消失"
    print("✓ 提示 5 秒后自动消失")

    # 3. 重新组装 Agent 有取消按钮
    pg.locator("#ag-n").fill(f"cache-agent-{U}")
    pg.locator("button", has_text="校验并发布").first.click()
    pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('发布成功')", timeout=10000)
    pg.wait_for_timeout(500)
    ag = next(a for a in c.get("/agents").json() if a["name"] == f"cache-agent-{U}")
    card = pg.locator(".ag-card", has_text=f"cache-agent-{U}").first
    card.locator("button", has_text="重新组装").click()
    pg.wait_for_timeout(400)
    assert pg.locator("#ag-cancel-btn").is_visible(), "重新组装无取消按钮"
    assert pg.locator("#ag-submit-btn").inner_text() == "保存修改"
    print("✓ 重新组装有取消按钮")
    pg.locator("#ag-cancel-btn").click()
    pg.wait_for_timeout(300)
    assert pg.locator("#ag-cancel-btn").is_hidden()
    print("✓ 取消编辑恢复表单")

    # 4. 团队编辑有取消按钮
    tcard = pg.locator("#tm-list .ag-card", has_text=f"cache-team2-{U}").first
    tcard.locator("button", has_text="编辑成员/信息").click()
    pg.wait_for_timeout(400)
    assert pg.locator("#tm-cancel-btn").is_visible(), "团队编辑无取消按钮"
    print("✓ 团队编辑有取消按钮")

    assert not errs, errs[:3]
    b.close()

# 清理
for t in c.get("/agents/teams").json():
    if t["name"].startswith("cache-team"):
        c.delete(f"/agents/teams/{t['name']}")
for ag in c.get("/agents").json():
    if ag["name"].startswith(("cache-agent",)) or ag["name"].endswith("-leader") and "cache-team" in ag["name"]:
        c.delete(f"/agents/{ag['id']}")
print("PASS-MSG-DEADLINE ✓")
