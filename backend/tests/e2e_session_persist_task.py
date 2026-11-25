"""验证：切页面后后台任务继续执行、会话列表状态图标（转圈/绿点）。"""
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

    # 确保有新会话
    sess_count = pg.locator("#sess-list .sess-item").count()
    if sess_count == 0:
        pg.locator("button.sess-new").click()
        pg.wait_for_timeout(500)
    # 记录当前会话标题
    first = pg.locator("#sess-list .sess-item").first
    title = first.locator(".st").inner_text()
    print("会话:", title)

    # 发一个任务
    pg.locator("#orch-in").fill("写一首关于春天的短诗")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(800)  # 让任务开始

    # 切换到别的页面（任务应继续）
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector("#sk-list", state="attached", timeout=10000)
    print("✓ 已切到 Skill 市场，任务在后台继续")

    # 等待任务完成（轮询后端历史）
    import httpx, time
    api = httpx.Client(base_url="http://localhost:8900", timeout=30)
    sessions = api.get("/orchestrator/sessions").json()
    target = sessions[0]["session_id"]
    done = False
    for _ in range(40):
        hist = api.get(f"/orchestrator/sessions/{target}").json().get("history", [])
        if len(hist) >= 2 and hist[-1].get("role") == "assistant":
            done = True
            break
        time.sleep(1)
    assert done, "任务未能在后台完成"
    print("✓ 切页面后任务在后台完成并落库")

    # 切回对话页，历史应显示回复
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-body", timeout=10000)
    pg.wait_for_timeout(500)
    body = pg.locator("#orch-body").inner_text()
    assert "春" in body or "spring" in body.lower(), body[:300]
    print("✓ 切回后看到后台完成的回复")

    # 状态图标：绿点或转圈至少有一种
    icons = pg.locator("#sess-list .sess-done, #sess-list .spin").count()
    assert icons >= 1, "会话列表缺少状态图标"
    print("✓ 会话列表显示状态图标（绿点/转圈）")

    assert not errs, errs[:3]
    b.close()
print("PASS-SESSION-PERSIST-TASK ✓")
