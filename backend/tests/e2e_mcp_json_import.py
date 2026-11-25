"""验证 MCP JSON 配置导入（Claude Desktop 风格 mcpServers）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx, json

c = httpx.Client(base_url="http://localhost:8000", timeout=60)
for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])

# 1. 标准 mcpServers JSON（url 方式）
cfg = {"mcpServers": {
    "json-mcp": {"url": "http://host.docker.internal:18899/", "description": "JSON导入的计算工具"}
}}
r = c.post("/mcp/import", json={"config": json.dumps(cfg)})
assert r.status_code == 200, r.text
d = r.json()
assert d["imported"] == 1 and d["results"][0]["ok"], d
assert d["results"][0]["tool_count"] == 2
print("✓ JSON 导入成功:", d["results"])

servers = c.get("/mcp/servers").json()
assert len(servers) == 1 and servers[0]["transport"] == "http"
assert servers[0]["tool_count"] == 2
print("✓ 落库并发现工具:", servers[0]["name"])

# 2. 重复导入应报错但不崩
r2 = c.post("/mcp/import", json={"config": json.dumps(cfg)})
assert r2.status_code == 200 and not r2.json()["results"][0]["ok"]
print("✓ 重复导入有错误提示不崩溃")

# 3. 错误 JSON
r3 = c.post("/mcp/import", json={"config": "not json"})
assert r3.status_code == 422
print("✓ 非法 JSON 返回 422")

for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-JSON-IMPORT ✓")
