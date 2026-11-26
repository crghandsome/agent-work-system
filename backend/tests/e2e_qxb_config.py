"""验证日报数据源能找到项目 mcp/ 下的 qxb 连接配置。"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "/srv/data/artifacts/skills/wage-pay-judge/wage-pay-judge/scripts")
import datasource_qxb as dq

print("mcp_json:", dq.DEFAULT_QXB_CONFIG["mcp_json"])
assert os.path.isfile(dq.DEFAULT_QXB_CONFIG["mcp_json"]), "未找到项目 mcp/qxb/mcp.json"
cfg = dict(dq.DEFAULT_QXB_CONFIG)
url, headers = dq._load_connection(cfg)
print("url:", url[:60])
print("headers keys:", list(headers.keys()))
assert url.startswith("http"), url
print("✓ 数据源正确读取项目 mcp/qxb/mcp.json 的连接与鉴权头")
print("PASS-QXB-CONFIG ✓")
