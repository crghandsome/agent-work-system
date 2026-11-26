"""验证 Peer 注册自动从 Agent Card 发现（新版标准路径）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=60)
for p in c.get("/a2a/peers").json(): c.delete("/a2a/peers/"+p["id"])

# 1. 预览发现（不落库）
r = c.post("/a2a/peers/discover", json={"base_url": "http://host.docker.internal:18898"})
assert r.status_code == 200, r.text
d = r.json()
assert d["ok"] and d["name"] == "外部客服机器人", d
assert len(d["skills"]) == 2, d["skills"]
assert d["card_url"].endswith("/.well-known/agent-card.json"), d["card_url"]
print("✓ 预览发现：名称/简介/2个技能自动识别")

# 2. 注册（只填 base_url，不填名称/简介）
r2 = c.post("/a2a/peers", json={"base_url": "http://host.docker.internal:18898/"})
assert r2.status_code == 200, r2.text
p = r2.json()
assert p["discovered"] is True and p["skill_count"] == 2, p
peer = [x for x in c.get("/a2a/peers").json() if x["id"] == p["id"]][0]
assert peer["name"] == "外部客服机器人" and "售后" in peer["description"]
assert len(peer["skills"]) == 2 and peer["card_url"].endswith("agent-card.json")
print("✓ 注册自动填充：", peer["name"], "|", peer["category"], "|", [s["name"] for s in peer["skills"]])

# 3. 手动指定覆盖自动发现
for x in c.get("/a2a/peers").json(): c.delete("/a2a/peers/"+x["id"])
r3 = c.post("/a2a/peers", json={"base_url": "http://host.docker.internal:18898/",
    "name": "我的自定义名称", "description": "手动填写的描述"})
p3 = r3.json()
peer3 = [x for x in c.get("/a2a/peers").json() if x["id"] == p3["id"]][0]
assert peer3["name"] == "我的自定义名称" and peer3["description"] == "手动填写的描述"
print("✓ 手动填写优先于自动发现")

# 4. 刷新 Card
r4 = c.post(f"/a2a/peers/{p3['id']}/refresh-card")
assert r4.status_code == 200 and len(r4.json()["skills"]) == 2
print("✓ 刷新 Card 更新能力信息")

# 5. 本平台标准 Card 端点
r5 = c.get("/.well-known/agent-card.json")
assert r5.status_code == 200 and "name" in r5.json() and "skills" in r5.json()
print("✓ 本平台标准 Card 端点可用")

for x in c.get("/a2a/peers").json(): c.delete("/a2a/peers/"+x["id"])
print("PASS-A2A-CARD-DISCOVER ✓")
