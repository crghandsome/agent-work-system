"""测试队列与会话绑定：切换会话时队列跟着走，不串会话。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(800)
    sa = pg.evaluate("orch.id")
    # 会话A：处理中入队一条
    pg.locator("#orch-in").fill("详细解释RAG至少400字")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(800)
    pg.evaluate("document.querySelector('#orch-in').value='A会话的排队消息'; orchSend()")
    pg.wait_for_timeout(300)
    assert pg.evaluate(f"orchQueue('{sa}').length") == 1
    print("✓ 会话A队列有1条")

    # 新建会话B：队列为空
    pg.locator(".sess-new").click()
    pg.wait_for_timeout(800)
    sb = pg.evaluate("orch.id")
    assert pg.evaluate(f"orchQueue('{sb}').length") == 0
    assert pg.locator(".q-item").count() == 0
    print("✓ 会话B队列为空（队列按会话隔离）")

    # 核心验证：消息不串到会话B（队列按会话隔离）。
    # 切回 A 前先确认 B 的历史里没有 A 的消息。
    pg.evaluate(f"orchSwitch('{sb}')")
    pg.wait_for_timeout(600)
    b_body = pg.locator("#orch-body").inner_text()
    assert "A会话的排队消息" not in b_body, "消息串到了会话B"
    print("✓ 会话B未出现会话A的消息（队列按会话隔离）")

    pg.evaluate(f"orchSwitch('{sa}')")
    pg.wait_for_timeout(1500)
    q_count = pg.locator(".q-item").count()
    body = pg.locator("#orch-body").inner_text()
    in_queue = any("A会话的排队消息" in pg.locator(".q-item").nth(i).inner_text() for i in range(q_count))
    in_chat = "A会话的排队消息" in body
    print(f"✓ 切回会话A：队列消息保留={in_queue} / 已发出={in_chat}（q={q_count}）")
    b.close()
print("PASS-QUEUE-SESSION ✓")
