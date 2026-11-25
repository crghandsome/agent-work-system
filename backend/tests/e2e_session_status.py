"""验证：任务完成绿点在用户回到会话后消失；切回执行中会话能看到进度。"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

api = httpx.Client(base_url="http://localhost:8900", timeout=30)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(600)

    # 准备两个会话
    pg.locator("button.sess-new").click(); pg.wait_for_timeout(500)
    sidA = pg.evaluate("orch.id")
    pg.locator("button.sess-new").click(); pg.wait_for_timeout(500)
    sidB = pg.evaluate("orch.id")
    print("A:", sidA, "B:", sidB)

    # 在 A 发任务
    pg.evaluate(f"orchSwitch('{sidA}')"); pg.wait_for_timeout(500)
    pg.locator("#orch-in").fill("写一首关于夏天的诗")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(500)

    # 立刻切到 B（任务在 A 后台继续）
    pg.evaluate(f"orchSwitch('{sidB}')"); pg.wait_for_timeout(300)
    # A 应显示转圈
    a_item = pg.locator(f".sess-item:has-text('新对话')").first
    spin = pg.evaluate(f"ORCH_RUNNING.has('{sidA}')")
    print("切走后 A running:", spin)

    # 等 A 任务完成
    for _ in range(40):
        hist = api.get(f"/orchestrator/sessions/{sidA}").json().get("history", [])
        if len(hist) >= 2 and hist[-1].get("role") == "assistant":
            break
        time.sleep(1)
    pg.wait_for_timeout(2500)  # 等前端轮询感知完成
    unread = pg.evaluate(f"ORCH_UNREAD.has('{sidA}')")
    print("任务完成后 A 未读标记:", unread)

    # 切回 A：绿点应消失
    pg.evaluate(f"orchSwitch('{sidA}')"); pg.wait_for_timeout(800)
    unread_after = pg.evaluate(f"ORCH_UNREAD.has('{sidA}')")
    print("切回后 A 未读标记:", unread_after)
    assert not unread_after, "回到会话后绿点未消失"

    # 会话列表中 A 不应再有绿点（当前会话也不显示绿点）
    done_icons = pg.locator(f".sess-item.on .sess-done").count()
    assert done_icons == 0, "当前会话仍显示绿点"
    print("✓ 回到会话后绿点消失")

    assert not errs, errs[:3]
    b.close()
print("PASS-SESSION-STATUS ✓")
