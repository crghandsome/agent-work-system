"""功能点测试3：输出简洁、无奇怪符号（UI + 模型回复）。"""
import sys, re, pathlib, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from playwright.sync_api import sync_playwright

# 1. 源码中无装饰性 emoji
h = pathlib.Path("backend/web/index.html").read_text(encoding="utf-8")
emoji = set(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", h))
print("UI 装饰符号残留:", emoji)
assert not emoji, f"仍有装饰 emoji: {emoji}"
print("✓ UI 无装饰性 emoji")

# 2. JS 语法
js = re.search(r"<script>(.*?)</script>", h, re.S).group(1)
pathlib.Path("backend/tests/_check.js").write_text(js, encoding="utf-8")
import subprocess
r = subprocess.run(["node", "--check", "backend/tests/_check.js"], capture_output=True, text=True)
pathlib.Path("backend/tests/_check.js").unlink(missing_ok=True)
assert r.returncode == 0, r.stderr[-300:]
print("✓ JS 语法通过")

# 3. 浏览器渲染 + 模型回复无 Markdown 符号
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1500, "height": 950})
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.wait_for_selector("#orch-in", timeout=15000)
    pg.locator("#orch-in").fill("简单说明什么是RAG，用两句话")
    pg.locator("#orch-go").click()
    pg.wait_for_function("() => { const c=[...document.querySelectorAll('.turn.ai .content')].pop(); return c && c.textContent.length>10 && !c.querySelector('.spin'); }", timeout=90000)
    text = pg.locator(".turn.ai .content").last.inner_text()
    print("回复:", text[:120].replace("\n", " "))
    bad = re.findall(r"\*\*|^#{1,6}\s|`", text, re.M)
    assert not bad, f"回复含 Markdown 符号: {bad}"
    print("✓ 模型回复无 Markdown 符号")
    b.close()
print("PASS-3 ✓")
