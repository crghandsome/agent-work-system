"""验证设计 Agent 走普通 ReAct：问身份不再强制设计、不产生制品。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=300)
skills_before = {s["name"] for s in c.get("/skills").json()}
plugins_before = {p["key"] for p in c.get("/plugins/managed").json()}

for name in ("skill-designer", "plugin-designer"):
    a = next(x for x in c.get("/agents").json() if x["name"] == name)
    r = c.post(f"/agents/{a['id']}/playground", json={"input": "你是谁？你能做什么？"})
    assert r.status_code == 200, r.text
    d = r.json()
    actions = [s.get("action") for s in d["steps"]]
    assert "designer" not in actions, f"{name} 仍走强制设计: {actions}"
    print(f"✓ {name} 问身份走 ReAct，未强制设计:", actions)

assert {s["name"] for s in c.get("/skills").json()} == skills_before
assert {p["key"] for p in c.get("/plugins/managed").json()} == plugins_before
print("✓ 未产生任何新制品")
print("PASS-DESIGNER-REACT ✓")
