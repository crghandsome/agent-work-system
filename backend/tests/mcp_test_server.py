"""测试用 MCP Server：HTTP JSON-RPC，提供两个工具（计算器、天气查询）。"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOLS = [
    {"name": "calculate", "description": "计算两个数的加减乘除", "inputSchema": {
        "type": "object", "properties": {
            "a": {"type": "number"}, "b": {"type": "number"},
            "op": {"type": "string", "enum": ["add", "sub", "mul", "div"]}},
        "required": ["a", "b", "op"]}},
    {"name": "get_weather", "description": "查询城市天气（模拟数据）", "inputSchema": {
        "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n))
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test-mcp", "version": "1.0.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            args = req.get("params", {}).get("arguments", {})
            name = req.get("params", {}).get("name")
            if name == "calculate":
                a, b, op = args.get("a", 0), args.get("b", 0), args.get("op", "add")
                v = {"add": a+b, "sub": a-b, "mul": a*b, "div": a/b if b else 0}.get(op, 0)
                result = {"content": [{"type": "text", "text": f"结果: {v}"}]}
            elif name == "get_weather":
                result = {"content": [{"type": "text", "text": f"{args.get('city','未知')}：晴，25℃"}]}
            else:
                result = {"content": [{"type": "text", "text": "未知工具"}]}
        elif method.startswith("notifications/"):
            self.send_response(200); self.end_headers(); return
        else:
            result = {}
        resp = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 18899), Handler).serve_forever()
