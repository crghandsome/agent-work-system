"""测试用 A2A Agent Card Server：在标准路径暴露 Card。"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

CARD = {
    "name": "外部客服机器人",
    "version": "2.1.0",
    "description": "专门处理客户咨询与售后问题的外部智能体，支持订单查询与退换货指引",
    "protocol": "a2a/1.0",
    "url": "http://localhost:18898/a2a/tasks",
    "capabilities": {"streaming": True, "pushNotifications": False},
    "skills": [
        {"id": "order-query", "name": "订单查询", "description": "查询订单状态与物流", "tags": ["客服", "订单"]},
        {"id": "refund-guide", "name": "退款指引", "description": "指导用户完成退款流程", "tags": ["客服", "售后"]},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/.well-known/agent-card.json":
            body = json.dumps(CARD, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 18898), Handler).serve_forever()
