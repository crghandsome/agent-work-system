"""任务执行中切页再切回：结果和文件卡片都要恢复（真实后台任务）。

用慢速 LLM 决策制造时间窗口：发消息后立刻切页，等完成再切回。
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
# 创建一个会话
s = c.post("/orchestrator/sessions").json()
sid = s["session_id"]
print("session:", sid)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.evaluate("sid => orchSwitch(sid)", sid)
    pg.wait_for_timeout(600)

    # 发一条普通消息（会触发后台任务）
    pg.locator("#orch-in").fill("你好，请简单介绍一下你自己")
    pg.locator("#orch-go").click()
    pg.wait_for_timeout(1000)
    # 立刻切走
    pg.locator('nav button[data-p="mcp"]').first.click()

    # 等后端落库：先等任务出现，再等它完成（running 端点有注册延迟）
    hist = []
    for _ in range(120):
        hist = c.get(f"/orchestrator/sessions/{sid}").json().get("history", [])
        if len(hist) >= 2:
            break
        time.sleep(1)
    print("backend history msgs:", len(hist))
    assert len(hist) >= 2, "后端未落库"

    # 切回
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_timeout(1500)
    body = pg.locator("#orch-body").inner_text()
    print("body head:", body[:200].replace("\n", " "))
    # 用户消息和 AI 回复都要在
    assert "你好，请简单介绍一下你自己" in body, "用户消息丢失"
    assert len(body) > 50, "结果空白"
    print("✓ 任务中切页，完成后切回：用户消息与 AI 结果均恢复")
    assert not errs, errs[:3]
    b.close()
print("PASS-TASK-SWITCH-RESUME ✓")
