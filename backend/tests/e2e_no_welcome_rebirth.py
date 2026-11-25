"""验证：发消息后立刻切走再切回，欢迎面板不重现（pending 消息保留）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(500)

    # 用全新会话，确保有 welcome
    pg.locator("button.sess-new").click(); pg.wait_for_timeout(400)
    sidA = pg.evaluate("orch.id")
    assert pg.locator("#orch-body .welcome").count() == 1, "新会话应有欢迎面板"

    # 切到会话 B
    pg.locator("button.sess-new").click(); pg.wait_for_timeout(400)
    sidB = pg.evaluate("orch.id")

    # 切回 A，发一条消息后立刻切到 B 再立刻切回 A（不停顿，任务未完成）
    pg.evaluate(f"orchSwitch('{sidA}')")
    pg.locator("#orch-in").fill("测试欢迎面板不重现")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(50)  # 极短：任务远未完成、后端未落库
    pg.evaluate(f"orchSwitch('{sidB}')")
    pg.wait_for_timeout(50)
    pg.evaluate(f"orchSwitch('{sidA}')")
    pg.wait_for_timeout(300)

    welcome = pg.locator("#orch-body .welcome").count()
    body = pg.locator("#orch-body").inner_text()
    print("切回后 welcome 数:", welcome, "body:", body[:100])
    assert welcome == 0, "欢迎面板错误地重新出现"
    assert "测试欢迎面板不重现" in body, "刚发的消息丢失"
    print("✓ 立即切走再切回：欢迎面板不重现，刚发消息仍在")

    assert not errs, errs[:3]
    b.close()
print("PASS-NO-WELCOME-REBIRTH ✓")
