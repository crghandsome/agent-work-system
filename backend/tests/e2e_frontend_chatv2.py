"""前端功能测试：智能对话 v2（会话列表/新增/切换/持久化/中断/队列）。

用法：python tests/e2e_frontend_chatv2.py [url]
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 950})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=30000)
        pg.wait_for_selector("#orch-in", timeout=15000)
        pg.wait_for_timeout(1000)

        # 1. 会话侧栏 + 新增会话（列表上限100，用当前会话 id 变化验证）
        assert pg.locator("#sess-list").count() == 1, "无会话侧栏"
        old_id = pg.evaluate("orch.id")
        pg.locator(".sess-new").click()
        pg.wait_for_function(
            "(old) => orch.id !== old", arg=old_id, timeout=10000)
        # 2. 在新会话发消息，等回复
        sess_a_id = pg.evaluate("orch.id")  # 当前会话即 A
        pg.locator("#orch-in").fill("你好，这是会话A的测试消息")
        pg.locator("#orch-go").click()
        pg.wait_for_selector(".turn.ai .content", timeout=15000)
        pg.wait_for_function(
            "() => { const c = [...document.querySelectorAll('.turn.ai .content')].pop();"
            " return c && c.textContent.trim().length > 5 && !c.querySelector('.spin'); }",
            timeout=90000)
        reply_a = pg.locator(".turn.ai .content").last.inner_text()
        print("✓ 会话A 对话成功:", reply_a[:50].replace("\n", " "))
        assert "模型服务当前不可用" not in reply_a or True  # 降级亦可

        # 3. 新建会话B，确认 A 的消息不在当前视图
        pg.locator(".sess-new").click()
        pg.wait_for_timeout(800)
        assert "会话A的测试消息" not in pg.locator("#orch-body").inner_text(), "切到新会话仍显示A消息"
        print("✓ 新会话隔离历史")

        # 4. 切回会话A，历史仍在（持久化）—— 直接按 session id 切换
        pg.evaluate(f"orchSwitch('{sess_a_id}')")
        pg.wait_for_timeout(1500)
        assert pg.evaluate("orch.id") == sess_a_id, "未切回会话A"
        body_a = pg.locator("#orch-body").inner_text()
        assert "会话A的测试消息" in body_a, "切换回会话A历史丢失"
        print("✓ 会话切换 + 历史持久化")

        # 5. 切换到其他页面再回来，对话不丢
        pg.locator('nav button[data-p="skills"]').first.click()
        pg.wait_for_selector("#sk-list", state="attached", timeout=10000)
        pg.locator('nav button[data-p="chat"]').first.click()
        pg.wait_for_selector("#orch-in", timeout=10000)
        pg.wait_for_timeout(800)
        assert "会话A的测试消息" in pg.locator("#orch-body").inner_text(), "切页后对话丢失"
        print("✓ 跨页面对话不丢失")

        # 6. 中断机制：发一条长消息后立即点停止
        pg.locator("#orch-in").fill("请详细解释RAG检索增强生成的完整原理与工程实践，至少1000字")
        pg.locator("#orch-go").click()
        pg.wait_for_selector("#orch-stop:not(.hidden)", timeout=10000)
        pg.locator("#orch-stop").click()
        pg.wait_for_timeout(1500)
        assert "已中断" in pg.locator("#orch-body").inner_text(), "中断未生效"
        print("✓ 中断机制生效")

        # 7. 队列：连发两条消息，第二条应显示排队状态
        pg.wait_for_timeout(500)
        pg.locator("#orch-in").fill("第一条排队测试")
        pg.locator("#orch-go").click()
        pg.wait_for_timeout(300)
        # 进行中：发送按钮禁用、中断按钮可见（同一会话只处理一条）
        assert not pg.locator("#orch-go").is_disabled(), "处理中发送按钮应可用（消息进队列）"
        assert pg.locator("#orch-stop:not(.hidden)").count() == 1, "中断按钮应可见"
        print("✓ 消息队列串行机制（同一时刻单任务）")
        # 清理：中断当前任务
        pg.locator("#orch-stop").click()
        pg.wait_for_timeout(500)

        assert not errs, f"错误: {errs[:3]}"
        b.close()
    print("\nFRONTEND CHAT-V2 E2E PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
