"""确定性验证：前端能分区渲染子 Agent 实时过程事件。

不依赖 LLM 委托决策与网关状态：直接调 appendSubEvent 注入事件，验证 DOM 渲染。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width":1500,"height":1000})
    errs=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8900/console", wait_until="networkidle", timeout=30000)
    pg.locator('nav button[data-p="chat"]').first.click()
    pg.wait_for_selector("#orch-in", timeout=15000)

    # 造一个 AI turn，注入子 Agent 事件（模拟 SSE phase=sub）
    result = pg.evaluate("""() => {
      const body = document.querySelector('#orch-body');
      body.innerHTML = '<div class="turn ai"><div class="content"></div></div>';
      const turn = body.querySelector('.turn.ai');
      appendSubEvent(turn, {type:'thought', sub_agent:'报告子Agent', text:'先分析数据结构'});
      appendSubEvent(turn, {type:'tool', sub_agent:'报告子Agent', action:'skill_script'});
      appendSubEvent(turn, {type:'reasoning', sub_agent:'报告子Agent', delta:'调用脚本生成报告'});
      const box = turn.querySelector('.sub-process');
      return {rows: box.querySelectorAll('.sub-row').length, text: box.innerText};
    }""")
    print("rendered sub rows:", result["rows"])
    assert result["rows"] == 3, result
    assert "报告子Agent" in result["text"], result["text"]
    assert "skill_script" in result["text"], result["text"]
    print("✓ 前端分区渲染子 Agent 思考/工具事件")
    assert not errs, errs[:3]
    b.close()
print("PASS-SUB-UI ✓")
