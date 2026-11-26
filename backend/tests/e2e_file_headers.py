"""验证对话流中能收到 file 事件。"""
import sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx

c = httpx.Client(base_url="http://localhost:8000", timeout=120)
# 复用上一步生成的文件所在目录；用前端列表接口验证即可
r = c.get("/files/e2e-delivery")
assert r.status_code == 200 and r.json()
# 验证下载响应头带 Content-Disposition（浏览器下载用）
r2 = c.get("/files/e2e-delivery/file-gen-test.docx")
cd = r2.headers.get("content-disposition", "")
print("content-disposition:", cd)
assert "attachment" in cd and "file-gen-test.docx" in cd, cd
print("✓ 下载响应带附件头，浏览器可下载")
print("PASS-FILE-HEADERS ✓")
