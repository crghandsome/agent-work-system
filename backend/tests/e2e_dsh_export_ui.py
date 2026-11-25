"""前端测试：Skill/插件卡片上的「导出到 DSH」按钮。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

c = httpx.Client(base_url="http://localhost:8900", timeout=60)
U = "uiexp01"
MD = f"---\nname: exp-skill-{U}\ndescription: 测试导出按钮的写作技能\n---\n\n写文档".encode()
c.post("/skills/upload-md", files={"file": (f"x.md", MD, "text/markdown")})
c.post("/plugins/install", json={"key": f"exp-plugin-{U}", "name": "导出测试插件",
    "description": "测试导出按钮", "extension_point": "WorkflowNode"})

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)

    # Skill 导出按钮
    pg.locator('nav button[data-p="skills"]').first.click()
    pg.wait_for_selector(".sk-card", timeout=15000)
    card = pg.locator(".sk-card", has_text=f"exp-skill-{U}").first
    card.locator("button", has_text="导出到 DSH").click()
    pg.wait_for_function("() => document.body.textContent.includes('导出成功')", timeout=10000)
    print("✓ Skill 导出到 DSH 按钮工作")
    pg.locator(".modal-mask").last.locator("#modal-ok").click()   # 关闭 toast
    pg.wait_for_timeout(300)

    # 插件导出按钮
    pg.locator('nav button[data-p="plugins"]').first.click()
    pg.wait_for_selector(".pl-card", timeout=15000)
    pl = pg.locator(f'.pl-card[data-key="exp-plugin-{U}"]')
    pl.locator("button", has_text="导出 DSH").click()
    pg.wait_for_function("() => document.body.textContent.includes('导出成功')", timeout=10000)
    print("✓ 插件导出 DSH bundle 按钮工作")

    assert not errs, errs[:3]
    b.close()

# 清理
c.delete(f"/skills/exp-skill-{U}")
c.delete(f"/plugins/exp-plugin-{U}")
print("PASS-DSH-EXPORT-UI ✓")
