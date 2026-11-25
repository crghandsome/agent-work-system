"""前端功能测试 4：Agent 市场（组装、编排型、评测、Fork、Playground）。

用法：python tests/e2e_frontend4.py [url]
"""
from __future__ import annotations

import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8900/console"
U = uuid.uuid4().hex[:6]


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1440, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.goto(URL, wait_until="networkidle", timeout=30000)
        pg.locator('nav button[data-p="agents"]').first.click()
        pg.wait_for_selector("#ag-list", state="attached", timeout=10000)
        pg.wait_for_timeout(1500)

        # 1. 组装一个普通 Agent（引用刚建的 Skill 或空）
        pg.locator("#ag-n").fill(f"ui-pro-{U}")
        pg.locator("#ag-d").fill("UI测试专业Agent")
        pg.locator("#ag-p").fill("你是专业助手，回答简洁。")
        pg.locator("#ag-mem").select_option("memory_simple")
        pg.locator("#ag-int").select_option("intent_keyword")
        pg.get_by_role("button", name="校验并发布").click()
        pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('发布成功')", timeout=20000)
        print("✓ 组装专业 Agent 成功")

        # 2. 组装编排型主 Agent（勾选编排、选子 Agent）
        pg.wait_for_timeout(600)
        pg.locator("#ag-n").fill(f"ui-master-{U}")
        pg.locator("#ag-d").fill("UI测试主Agent")
        pg.locator("#ag-orch").check()
        pg.wait_for_selector(".ag-sub", timeout=5000)
        subs = pg.locator(".ag-sub")
        assert subs.count() >= 1, "无可选子 Agent"
        subs.first.check()
        pg.get_by_role("button", name="校验并发布").click()
        pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('编排型主 Agent')", timeout=20000)
        print("✓ 组装编排型主 Agent 成功（选中子 Agent）")

        # 3. 列表刷新后有主 Agent 标签
        pg.wait_for_timeout(800)
        assert "编排型主 Agent" in pg.locator("#ag-list").inner_text(), "列表未显示编排型标签"
        print("✓ 列表显示编排型标签")

        # 4. 评测（点第一张卡的“运行评测”）
        pg.locator("#ag-list button", has_text="运行评测").first.click()
        pg.wait_for_function("() => document.querySelector('#ag-list .res')?.textContent?.includes('评测')", timeout=120000)
        print("✓ 评测运行成功")

        # 5. Fork（自定义弹框）
        name = f"ui-fork-{U}"
        pg.locator("#ag-list button", has_text="Fork").first.click()
        pg.wait_for_selector(".modal-mask", timeout=5000)
        pg.locator("#modal-input").fill(name)
        pg.locator("#modal-ok").click()
        pg.wait_for_timeout(1500)
        print("✓ Fork 操作完成（自定义弹框）")

        # 6. Playground：导航过去发送一条消息
        pg.locator('nav button[data-p="playground"]').first.click()
        pg.wait_for_selector("#pg-in", timeout=10000)
        pg.locator("#pg-in").fill("你好")
        pg.locator("#pg-go").click()
        pg.wait_for_selector("#pg-trace .card", timeout=90000)
        print("✓ Playground 运行成功，轨迹显示")

        assert not errs, f"错误: {errs[:3]}"
        b.close()
    print("\nFRONTEND E2E-4 PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
