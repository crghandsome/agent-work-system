"""验证：会话历史带 files 时，切页回来能显示下载卡片，结果不空白。"""
import sys, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

# 自包含：先在容器内准备一个带交付文件历史的会话
subprocess.run(["docker", "exec", "-e", "PYTHONPATH=/srv/app", "agent-wokplat-api",
                "python", "tests/_prepare_switch.py"], check=True,
               stdout=subprocess.DEVNULL)
sid = Path("backend/tests/_sid.txt").read_text(encoding="utf-8").strip()
c = httpx.Client(base_url="http://localhost:8900", timeout=30)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.evaluate("sid => orchSwitch(sid)", sid)
    pg.wait_for_timeout(1200)
    assert "日报已生成" in pg.locator("#orch-body").inner_text(), "结果丢失"
    assert pg.locator("#orch-body .deliver-file").count() == 1, "文件卡片未渲染"
    print("✓ 会话历史带 files：结果与文件卡片正常显示")

    pg.locator('nav button[data-p="mcp"]').first.click()
    pg.wait_for_timeout(400)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_timeout(1200)
    body = pg.locator("#orch-body").inner_text()
    assert "日报已生成" in body, f"切页后结果丢失: {body[:200]}"
    assert pg.locator("#orch-body .deliver-file").count() >= 1, "切页后文件卡片丢失"
    print("✓ 切走再切回：结果与文件下载卡片都还在")
    assert not errs, errs[:3]
    b.close()
print("PASS-SWITCH-FILE-RESUME ✓")
