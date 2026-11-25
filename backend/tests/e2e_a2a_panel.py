"""前端：A2A 页面改造（删内部任务、调用链查询、Peer 说明）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=120)
U = uuid.uuid4().hex[:6]
# 造一次团队调用，产生 trace
leader, member = f"a2alead-{U}", f"a2amember-{U}"
for n in (leader, member): c.post("/agents", json={"name": n, "description": n})
tn = f"a2ateam-{U}"
c.post("/agents/teams", json={"name": tn, "description": "调用链UI测试", "leader": leader, "members": [member]})
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat", json={"text": f"让团队 {tn} 处理任务"})
trace = r.json()["trace_id"]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="a2a"]').first.click()
    pg.wait_for_selector("#trace-select", timeout=15000)
    pg.wait_for_timeout(800)

    # 1. 发起内部任务已删除
    assert pg.locator("#tk-ag").count() == 0 and pg.locator("#tk-msg").count() == 0
    print("✓ 「发起内部任务」已删除")

    # 2. Peer 说明
    txt = pg.locator("#main").inner_text()
    assert "另一个系统" in txt and "管理员提供" in txt, txt[:300]
    print("✓ 注册 Peer 有 URL 来源说明")

    # 3. 调用链查询
    pg.locator("#trace-id").fill(trace)
    pg.locator("button", has_text="查询").click()
    pg.wait_for_selector("#trace-result .tag", timeout=10000)
    result = pg.locator("#trace-result").inner_text()
    assert leader in result and member in result, result[:300]
    print("✓ 调用链显示主理与成员:", result[:100].replace("\n"," "))

    assert not errs, errs[:3]
    b.close()

# 清理
c.delete(f"/agents/teams/{tn}")
for n in (leader, member):
    for a in c.get("/agents").json():
        if a["name"] == n: c.delete(f"/agents/{a['id']}")
print("PASS-A2A-PANEL ✓")
