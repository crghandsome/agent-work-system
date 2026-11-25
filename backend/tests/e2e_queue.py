"""前端测试：LLM处理中继续发消息 → 待发队列（显示/编辑/删除/自动消费）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

URL = "http://localhost:8900/console"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(800)

    # 1. 发一条会较慢的任务
    before_turns = pg.locator(".turn.user").count()
    pg.locator("#orch-in").fill("请详细解释消息队列的原理和工程实践，至少300字")
    pg.locator("#orch-go").click()
    pg.wait_for_selector("#orch-stop:not(.hidden)", timeout=10000)
    pg.wait_for_timeout(500)
    assert pg.locator(".turn.user").count() == before_turns + 1
    print("✓ 首条消息开始处理")

    # 2. 处理中继续发两条 → 进入队列显示
    pg.locator("#orch-in").fill("队列第二条问题")
    pg.locator("#orch-go").click()
    pg.locator("#orch-in").fill("队列第三条问题")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(600)
    assert not pg.locator("#send-queue").is_hidden(), "队列区未显示"
    items = pg.locator(".q-item")
    assert items.count() == 2, f"队列应有2条，实际 {items.count()}"
    assert "队列第二条问题" in items.nth(0).inner_text()
    print("✓ 处理中消息进入队列（2条），未立即发送")
    assert pg.locator(".turn.user").count() == before_turns + 1, "队列中消息不应立即出现在对话流"

    # 3. 编辑队列第二条
    items.nth(0).locator(".qop", has_text="编辑").click()
    inp = pg.locator(".q-item input.qt")
    inp.fill("编辑后的队列问题")
    inp.press("Enter")
    pg.wait_for_timeout(300)
    assert "编辑后的队列问题" in pg.locator(".q-item").nth(0).inner_text()
    print("✓ 队列消息可编辑")

    # 4. 删除队列第三条
    pg.locator(".q-item").nth(1).locator(".qop", has_text="删除").click()
    pg.wait_for_timeout(300)
    assert pg.locator(".q-item").count() == 1, "删除后应剩1条"
    print("✓ 队列消息可删除")

    # 5. 等待当前任务完成，队列自动消费（编辑后的那条自动发出）
    pg.wait_for_function(
        "() => { const u=[...document.querySelectorAll('.turn.user')].map(e=>e.textContent);"
        " return u.some(t=>t.includes('编辑后的队列问题')); }", timeout=180000)
    print("✓ 当前任务完成后队列消息自动发送")
    # 队列清空
    pg.wait_for_function("() => document.querySelectorAll('.q-item').length === 0", timeout=5000)
    print("✓ 队列消费后清空")

    assert not errs, errs[:3]
    b.close()
print("PASS-QUEUE ✓")
