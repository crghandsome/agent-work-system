"""Agent 市场后端：落盘/删除/重新组装/推荐/团队协调。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
u = uuid.uuid4().hex[:6]

# 1. 创建 Agent → 落本地 agents/ 目录
r = c.post("/agents", json={"name": f"market-a-{u}", "description": "用于文档写作与代码审查的助手",
    "system_prompt": "你是写作助手", "skill_keys": [], "plugin_keys": {}})
r.raise_for_status()
a = r.json()
print("✓ 创建 Agent:", a["name"])

# 2. 智能推荐 Skill / 插件（按描述）
c.post("/skills/upload-md", files={"file": ("x.md", f"---\nname: rec-skill-{u}\ndescription: 文档写作技能，帮助撰写报告\n---\n\n写报告".encode(), "text/markdown")})
rs = c.post("/agents/recommend/skills", json={"description": "我想做一个文档写作的助手", "limit": 5})
rs.raise_for_status()
assert rs.json() and all("name" in x for x in rs.json()), rs.json()
print("✓ 按描述推荐 Skill:", [x["name"] for x in rs.json()][:3])
rp = c.post("/agents/recommend/plugins", json={"description": "需要意图识别和上下文压缩能力", "limit": 5})
rp.raise_for_status()
assert any(x["key"] == "intent_keyword" for x in rp.json()), rp.json()
print("✓ 按描述推荐插件:", [x["key"] for x in rp.json()][:3])

# 3. 重新组装
r = c.put(f"/agents/{a['id']}", json={"name": f"market-a-{u}", "description": "修改后：代码审查专家",
    "system_prompt": "你是代码审查专家", "skill_keys": [], "plugin_keys": {}})
r.raise_for_status()
assert "修改后" in r.json()["description"]
print("✓ 重新组装 Agent")

# 4b. 自动创建主理 Agent：leader 不存在时系统自动建
r = c.post("/agents/teams", json={"name": f"auto-team-{u}", "description": "自动建主理的团队",
    "leader": f"auto-leader-{u}", "members": []})
r.raise_for_status()
assert r.json()["auto_created_leader"] is True
assert any(a["name"] == f"auto-leader-{u}" for a in c.get("/agents").json())
print("✓ 创建团队自动创建主理 Agent")

# 4. 团队 CRUD
leader = f"leader-{u}"; member = f"member-{u}"
c.post("/agents", json={"name": leader, "description": "主理"})
c.post("/agents", json={"name": member, "description": "成员"})
r = c.post("/agents/teams", json={"name": f"team-{u}", "description": "写作团队",
    "leader": leader, "members": [member]})
r.raise_for_status()
assert r.json()["leader"] == leader
print("✓ 创建 Agent 团队:", r.json()["name"])
teams = c.get("/agents/teams").json()
assert any(t["name"] == f"team-{u}" for t in teams)
print("✓ 团队列表")

# 5. 智能对话中团队协调：主 Agent 只与 leader 通信
s = c.post("/orchestrator/sessions").json()
r = c.post(f"/orchestrator/sessions/{s['session_id']}/chat",
           json={"text": f"请让团队 team-{u} 帮我处理一个任务"})
r.raise_for_status()
body = r.json()
assert "主理" in body["reply"] and member in body["reply"], body["reply"][:300]
print("✓ 主 Agent 经主理 Agent 协调团队:", body["reply"][:120].replace("\n", " "))

# 6. 删除 Agent
r = c.delete(f"/agents/{a['id']}")
assert r.status_code == 200 and r.json()["ok"]
r2 = c.get(f"/agents/{a['id']}")
assert r2.status_code == 404
print("✓ 删除 Agent")

# 清理
for t in (f"team-{u}", f"auto-team-{u}"):
    c.delete(f"/agents/teams/{t}")
for n in (leader, member, f"auto-leader-{u}"):
    for ag in c.get("/agents").json():
        if ag["name"] == n:
            c.delete(f"/agents/{ag['id']}")
c.delete(f"/skills/rec-skill-{u}")
print("\nAGENT-MARKET BACKEND E2E PASSED ✓")
