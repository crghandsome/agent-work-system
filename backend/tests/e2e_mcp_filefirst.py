"""验证 MCP 文件优先：落盘、从目录重建、headers 透传。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=60)
# 清理
for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])

# 1. 接入（带 headers）
r = c.post("/mcp/import", json={"config": json.dumps({"mcpServers": {
    "file-mcp": {"url": "http://host.docker.internal:18899/",
                 "headers": {"Authorization": "Bearer test-token"}}
}})})
assert r.json()["results"][0]["ok"], r.json()
print("✓ 接入成功（带 headers）")

# 2. 项目 mcp/ 目录有配置
from pathlib import Path
p = Path("/srv/data/artifacts/mcp/file-mcp/mcp.json")
assert p.exists(), "未落盘"
cfg = json.loads(p.read_text(encoding="utf-8"))
assert cfg["name"] == "file-mcp" and cfg["headers"]["Authorization"] == "Bearer test-token"
print("✓ 落盘到 mcp/file-mcp/mcp.json，含 headers")

# 3. 删除 DB 记录但保留文件，然后重建
sid = c.get("/mcp/servers").json()[0]["id"]
from app.db import get_session_factory
from app.models import MCPServer
db = get_session_factory()()
db.query(MCPServer).filter(MCPServer.id == sid).delete()
db.commit(); db.close()
assert len(c.get("/mcp/servers").json()) == 0
from app.services import mcp_service
db = get_session_factory()()
created = mcp_service.rebuild_index_from_disk(db)
db.close()
servers = c.get("/mcp/servers").json()
assert len(servers) == 1 and servers[0]["name"] == "file-mcp"
assert servers[0]["headers"]["Authorization"] == "Bearer test-token"
print("✓ 从目录重建 DB 索引（含 headers）")

for s in c.get("/mcp/servers").json():
    if s["name"] in ['test-mcp', 'ui-mcp', 'json-mcp', 'ui-json-mcp', 'switch-mcp', 'verify-mcp', 'file-mcp', 'qxb-test']:
        c.delete("/mcp/servers/"+s["id"])
print("PASS-MCP-FILE-FIRST ✓")
