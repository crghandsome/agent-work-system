"""功能点测试1：清空历史后控制台自动新建会话。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8900/console"
api = "http://localhost:8900"
c = httpx.Client(base_url=api, timeout=60)
for s in c.get("/orchestrator/sessions").json():
    c.delete("/orchestrator/sessions/" + s["session_id"])
assert not c.get("/orchestrator/sessions").json()
print("✓ 后端历史清空")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.wait_for_timeout(1000)
    assert pg.evaluate("!!orch.id"), "未自动新建会话"
    assert len(c.get("/orchestrator/sessions").json()) == 1, "应恰好1个会话"
    print("✓ 控制台自动新建会话")
    assert not errs, errs[:2]
    b.close()
print("PASS-1 ✓")
