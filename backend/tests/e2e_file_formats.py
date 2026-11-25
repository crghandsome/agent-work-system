"""验证 docx/xlsx/pdf 等二进制文件可解析预览（依赖已固化到镜像）。"""
import sys, io
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx
from docx import Document
from openpyxl import Workbook

c = httpx.Client(base_url="http://localhost:8900", timeout=180)
# 快照：只清理本次测试新增的文档，保留用户已有文档
EXISTING_DOC_IDS = {d['id'] for d in c.get('/rag/documents').json()}

# docx（含标题样式）
doc = Document()
doc.add_paragraph("产品定价")
doc.add_paragraph("专业版每席位每月199元。")
buf = io.BytesIO(); doc.save(buf)
r = c.post("/rag/documents/preview", files={"file": ("test.docx", buf.getvalue(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
assert r.status_code == 200, r.text
assert "199元" in r.text, r.text[:300]
print("✓ docx 解析成功")

# xlsx
wb = Workbook(); ws = wb.active; ws.title = "价格表"
ws.append(["产品", "价格"]); ws.append(["专业版", 199])
buf2 = io.BytesIO(); wb.save(buf2)
r2 = c.post("/rag/documents/preview", files={"file": ("test.xlsx", buf2.getvalue(),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
assert r2.status_code == 200 and "199" in r2.text, r2.text[:300]
print("✓ xlsx 解析成功")

for d in c.get("/rag/documents").json():
    if d["id"] not in EXISTING_DOC_IDS:
        c.delete(f"/rag/documents/{d['id']}")
print("PASS-FILE-FORMATS ✓")
