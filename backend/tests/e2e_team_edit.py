"""后端：已建团队可编辑（改描述/主理/增删成员）。"""
import sys, uuid
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=180)
u = uuid.uuid4().hex[:6]
a1, a2, a3 = f"em-a-{u}", f"em-b-{u}", f"em-c-{u}"
for n in (a1, a2, a3):
    c.post("/agents", json={"name": n, "description": f"成员 {n}"})

# 建团（2 成员，自动建主理）
tn = f"em-team-{u}"
r = c.post("/agents/teams", json={"name": tn, "description": "初始描述", "leader": "", "members": [a1, a2]})
r.raise_for_status()
t = r.json()
assert t["members"] == [a1, a2] and t["auto_created_leader"]
print("✓ 建团（自动主理 + 2 成员）")

# 编辑：改描述 + 加一个成员 + 删一个成员 + 换主理为已存在 Agent
r = c.patch(f"/agents/teams/{tn}", json={
    "name": tn, "description": "更新后的团队描述",
    "leader": a3, "members": [a1, a3],
})
r.raise_for_status()
t2 = r.json()
assert t2["description"] == "更新后的团队描述"
assert t2["leader"] == a3
assert set(t2["members"]) == {a1, a3}
print("✓ 编辑团队（描述/主理/成员增删）:", t2)

# 从列表确认持久化
t3 = next(x for x in c.get("/agents/teams").json() if x["name"] == tn)
assert t3["members"] == [a1, a3]
print("✓ 编辑结果已持久化")

# 清理
c.delete(f"/agents/teams/{tn}")
for n in (a1, a2, a3, f"{tn}-leader"):
    for ag in c.get("/agents").json():
        if ag["name"] == n:
            c.delete(f"/agents/{ag['id']}")
print("\nTEAM-EDIT E2E PASSED ✓")
