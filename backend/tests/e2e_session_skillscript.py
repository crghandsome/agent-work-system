"""端到端：会话里请求执行 Skill 脚本，验证工具被调度。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
s = c.post("/orchestrator/sessions").json()
events = []
with c.stream("POST", f"/orchestrator/sessions/{s['session_id']}/chat/stream",
              json={"text": "请用 skill_script 工具执行 wage-pay-judge 的 judge.py 脚本，参数 --help"}) as r:
    assert r.status_code == 200
    for line in r.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:]))

delegs = [e for e in events if e.get("type") == "delegation"]
agents = [e.get("agent") for e in delegs]
print("tool delegations:", agents)
# 应至少有 skill_script 被调度
assert any("skill_script" in str(a) for a in agents), f"skill_script 未被调度: {agents}"
print("✓ 会话成功调度 skill_script 工具")

for a in c.get("/agents").json():
    pass
print("PASS-SESSION-SKILLSCRIPT ✓")
