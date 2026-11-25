"""验证虚构假数据已清除。"""
import httpx, pymysql
c = httpx.Client(base_url="http://localhost:8000", timeout=60)
conn = pymysql.connect(host="mysql", port=3306, user="root", password="aws_root_9f3b2c",
                       database="agent_work_system", charset="utf8mb4")
with conn.cursor() as cur:
    for table, col in [("documents","content"),("skills","instructions"),("agents","system_prompt")]:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE '%云信通%'")
        n = cur.fetchone()[0]
        assert n == 0, f"{table} 仍有 {n} 条假数据"
        print(f"✓ {table} 无云信通假数据")
conn.close()
# 界面建议卡无假数据（源码级检查由 e2e_symbols 覆盖，这里检查 API 检索不返回假数据）
r = c.post("/rag/search", json={"content": "云信通"}).json()
assert not any("云信通" in h["text"] for h in r["hits"]), "检索仍返回假数据"
print("✓ RAG 检索无假数据")
print("PASS-CLEAN ✓")
