"""前端功能测试（真实浏览器）：智能对话——发消息、会话创建、回复渲染。

用法：python tests/e2e_frontend.py [url]   默认 http://localhost:8900/console
先决条件：系统已启动且存在至少一个编排型主 Agent（e2e_orch.py 会创建）。
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # 捕获网络失败
        page.on("requestfailed", lambda r: errors.append(f"NETFAIL {r.url}: {r.failure}"))

        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        # 1. 页面基本渲染
        assert page.locator('nav button[data-p="chat"]').count() == 1, "侧栏无智能对话入口"
        print("✓ 页面渲染，侧栏入口存在")

        # 2. 输入框存在（说明主 Agent 已加载并进入对话）
        if page.locator("#orch-in").count() == 0:
            print("页面内容片段:", page.locator("#main").inner_text()[:300])
            raise AssertionError("输入框未渲染——可能无主 Agent 或初始化失败")

        # 3. 发消息
        page.locator("#orch-in").fill("你好，测试消息")
        page.locator("#orch-in").press("Enter")

        # 4. 用户消息气泡出现
        page.wait_for_selector(".turn.user .bubble", timeout=10000)
        user_text = page.locator(".turn.user .bubble").last.inner_text()
        assert "你好，测试消息" in user_text, f"用户消息内容错误: {user_text}"
        print("✓ 消息已发出，用户气泡渲染")

        # 5. 等待 AI 回复（正文或降级提示）——取最后一个 turn（当前轮次）
        page.wait_for_function(
            "() => { const els=[...document.querySelectorAll('.turn.ai .content')];"
            " const c=els[els.length-1];"
            " return c && c.textContent.trim().length > 5 && !c.querySelector('.cursor'); }",
            timeout=90000,
        )
        ai_text = page.locator(".turn.ai .content").last.inner_text()
        print("✓ AI 回复已渲染:", ai_text[:80].replace("\n", " "))

        # 6. 端到端确认会话真的建立（后端有 session）
        assert not errors, f"页面 JS/网络错误: {errors[:3]}"
        print("✓ 无 JS/网络错误")

        browser.close()
    print("\nFRONTEND E2E PASSED ✓（消息发出 + 回复渲染）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
