"""前端测试：Agent 组装页动态加载所有已安装插件并可选用。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = uuid.uuid4().hex[:6]
KEY = f"ag-pick-{U}"
# 安装一个用户插件
c.post("/plugins/install", json={"key": KEY, "name": "组装测试插件",
    "description": "用于验证组装时可选", "extension_point": "IntentRecognizer", "source": "market"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: errs.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="agents"]').first.click()
    pg.wait_for_selector("#ag-plugin-picker", timeout=10000)
    pg.wait_for_timeout(800)

    # 1. 插件选择器渲染了用户插件（不是写死的3个）
    picker = pg.locator("#ag-plugin-picker").inner_text()
    assert "组装测试插件" in picker, f"新装插件未出现在选择器: {picker[:200]}"
    assert KEY in picker, "插件 key 未显示"
    print("✓ 动态显示所有已安装插件（含新装的）")

    # 2. 选中新插件并组装 Agent
    pg.locator(f'.ag-plugin[value="{KEY}"]').check()
    pg.locator("#ag-n").fill(f"agent-pick-{U}")
    pg.locator("#ag-d").fill("验证插件选用")
    pg.locator("button", has_text="校验并发布").first.click()
    pg.wait_for_function("() => document.querySelector('#ag-msg')?.textContent?.includes('发布成功')", timeout=15000)
    print("✓ 用新装插件组装 Agent 成功")

    # 3. 验证 Agent 的 plugin_keys 包含该插件
    agents = c.get("/agents").json()
    a = next(x for x in agents if x["name"] == f"agent-pick-{U}")
    assert a["plugin_keys"].get("intent") == KEY, a["plugin_keys"]
    print("✓ Agent 正确引用用户插件:", a["plugin_keys"])

    c.delete(f"/plugins/{KEY}")
    assert not errs, errs[:3]
    b.close()
print("PASS-AGENT-PLUGIN-PICKER ✓")
