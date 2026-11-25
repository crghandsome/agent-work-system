"""RAG 服务：文档入库（分块插件 → embedding → Qdrant）+ 检索（向量 → 重排）。"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Document, new_id
from ..plugins.builtin import ChunkingPlugin
from ..providers.embedding import get_embedding_provider
from ..providers.rerank import get_rerank_provider

_client: QdrantClient | None = None


def get_qdrant() -> QdrantClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = QdrantClient(host=s.qdrant_host, port=s.qdrant_port, timeout=10)
    return _client


def ensure_collection() -> None:
    s = get_settings()
    client = get_qdrant()
    if not client.collection_exists(s.qdrant_collection):
        client.create_collection(
            collection_name=s.qdrant_collection,
            vectors_config=VectorParams(size=s.embedding_dim, distance=Distance.COSINE),
        )
        return
    # 维度变更检测：配置维度与现有 collection 不一致时重建（如 hashing 256 → bge-m3 1024）
    info = client.get_collection(s.qdrant_collection)
    cfg = info.config.params.vectors
    existing_dim = cfg.size if hasattr(cfg, "size") else None
    if existing_dim and int(existing_dim) != int(s.embedding_dim):
        client.delete_collection(s.qdrant_collection)
        client.create_collection(
            collection_name=s.qdrant_collection,
            vectors_config=VectorParams(size=s.embedding_dim, distance=Distance.COSINE),
        )


def reindex_all_documents(db: Session) -> int:
    """维度变更/启动后从 DB 重建全部向量索引（DB 文档是真相源）。"""
    from . import chunking_service

    s = get_settings()
    ensure_collection()
    client = get_qdrant()
    docs = db.query(Document).all()
    total = 0
    for doc in docs:
        genre = doc.genre or "custom"
        blocks = chunking_service.chunk_document(doc.content, genre,
                                                 {"chunk_size": 500, "overlap": 50},
                                                 doc_title=doc.title)
        if not blocks:
            continue
        texts = [b["embedding_text"] for b in blocks]
        vectors = get_embedding_provider().embed(texts)
        points = [_to_payload_point(doc.id, i, b["text"], doc.title, v,
                                    {"genre": genre, "locator": b["locator"], "kind": b["kind"]})
                  for i, (b, v) in enumerate(zip(blocks, vectors))]
        client.upsert(collection_name=s.qdrant_collection, points=points)
        total += len(points)
    return total


def _to_payload_point(doc_id: str, idx: int, text: str, title: str, vector: list[float],
                      meta: dict | None = None) -> PointStruct:
    payload = {"document_id": doc_id, "chunk_index": idx, "text": text, "title": title}
    if meta:
        payload.update(meta)
    return PointStruct(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{idx}")),
        vector=vector,
        payload=payload,
    )


def ingest_document(
    db: Session, *, title: str, content: str, chunking_config: dict | None = None,
    genre: str = "custom", genre_confidence: float = 0.0, detect_reason: str = "",
    source_filename: str = "", file_type: str = ".txt",
) -> Document:
    """入库：按体裁分块（用户指定优先），每块带 locator 元数据。"""
    s = get_settings()
    ensure_collection()
    from . import chunking_service

    config = chunking_config or {"chunk_size": 500, "overlap": 50}
    genre = genre or "custom"
    blocks = chunking_service.chunk_document(content, genre, config, doc_title=title)
    strategy = genre  # 策略名即体裁名（含 custom）
    doc = Document(
        id=new_id("doc"), title=title, content=content, chunk_count=len(blocks),
        source_filename=source_filename, file_type=file_type, chunk_strategy=strategy,
        genre=genre, genre_confidence=genre_confidence, detect_reason=detect_reason,
    )
    db.add(doc)
    db.flush()

    # 用 embedding_text（带上下文前缀）做向量化，payload 保留原文+locator
    texts = [b["embedding_text"] for b in blocks]
    vectors = get_embedding_provider().embed(texts) if texts else []
    points = [
        _to_payload_point(doc.id, i, b["text"], title, v,
                          {"genre": genre, "locator": b["locator"], "kind": b["kind"]})
        for i, (b, v) in enumerate(zip(blocks, vectors))
    ]
    if points:
        get_qdrant().upsert(collection_name=s.qdrant_collection, points=points)
    return doc


def _scroll_all_points(client, collection: str, limit: int = 2000) -> list[dict]:
    """拉取集合内全部点（带上限保护），供 BM25 本地打分。"""
    points, offset = [], None
    while len(points) < limit:
        batch, offset = client.scroll(
            collection_name=collection,
            limit=min(256, limit - len(points)),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not batch:
            break
        for p in batch:
            pl = p.payload or {}
            points.append({
                "id": p.id, "text": pl.get("text", ""),
                "document_id": pl.get("document_id", ""), "title": pl.get("title", ""),
            })
        if offset is None:
            break
    return points


def search(*, query: str, top_k: int | None = None, rerank: bool = True) -> dict:
    """混合检索：向量(ANN) + BM25(关键词)，RRF 融合，按阈值过滤。

    - 两路各召回 hybrid_k 个候选
    - RRF 融合：score = Σ 1/(rrf_k + rank)，rank 从 1 起
    - 相关性分 = max(向量余弦, BM25分/6)，低于 score_threshold 的丢弃；两路排名用 RRF 融合
    - top_k 控制最终返回数
    - 仅当配置了真实 rerank provider（非 hashing）时才重排，避免假分数污染
    """
    s = get_settings()
    ensure_collection()
    k = top_k or s.rag_top_k
    candidate_n = s.rag_hybrid_k
    threshold = s.rag_score_threshold
    client = get_qdrant()

    # ── 路 1：向量 ANN ──
    qvec = get_embedding_provider().embed([query])[0]
    vec_result = client.query_points(
        collection_name=s.qdrant_collection, query=qvec, limit=candidate_n,
    )
    vec_hits = vec_result.points

    # ── 路 2：BM25 关键词 ──
    from .bm25 import BM25

    all_points = _scroll_all_points(client, s.qdrant_collection)
    bm25_scores = BM25([p["text"] for p in all_points]).scores(query) if all_points else []
    # BM25 top-N 的 id 与分数（BM25 分仅用于 RRF 排名，不参与阈值——它的绝对分无统一量纲）
    bm25_order = sorted(range(len(all_points)), key=lambda i: -bm25_scores[i])[:candidate_n]
    bm25_rank = {all_points[i]["id"]: (rank, bm25_scores[i]) for rank, i in enumerate(bm25_order, 1)}

    candidates_by_id: dict[str, dict] = {}

    def ensure(id_, text, doc_id, title):
        return candidates_by_id.setdefault(id_, {
            "text": text, "document_id": doc_id, "title": title,
            "vector_score": 0.0, "bm25_score": 0.0,
        })

    # 向量路候选
    for rank, h in enumerate(vec_hits, start=1):
        pl = h.payload or {}
        c = ensure(str(h.id), pl.get("text", ""), pl.get("document_id", ""), pl.get("title", ""))
        c["vector_score"] = max(c["vector_score"], float(h.score))
        c["_vrank"] = rank
    # BM25 路候选
    for pid, (rank, score) in bm25_rank.items():
        p = next(pp for pp in all_points if pp["id"] == pid)
        c = ensure(pid, p["text"], p["document_id"], p["title"])
        c["bm25_score"] = score
        c["_brank"] = rank

    # ── RRF 融合 + 阈值 ──
    rrf_k = s.rag_rrf_k
    max_bm25 = max(bm25_scores) if bm25_scores else 0.0
    # 可信度系数：max_bm25 越高，BM25 归一化分越可信；整体弱相关时自动压低
    bm25_weight = min(max_bm25 / 3.0, 1.0)
    for c in candidates_by_id.values():
        rrf = 0.0
        if c.get("_vrank"):
            rrf += 1.0 / (rrf_k + c["_vrank"])
        if c.get("_brank"):
            rrf += 1.0 / (rrf_k + c["_brank"])
        c["rrf"] = rrf
        # 相关性分：向量余弦(0-1) 与 加权 BM25 归一化取最大
        bm25_rel = (c["bm25_score"] / max_bm25 * bm25_weight) if max_bm25 > 0 else 0.0
        c["relevance"] = max(c["vector_score"], bm25_rel)

    # 先按 RRF 排序取候选（此时不做阈值过滤）
    ranked_candidates = sorted(candidates_by_id.values(), key=lambda c: -c["rrf"])

    # 真实 rerank provider 才参与（hashing 是假重排，跳过）
    was_reranked = False
    if rerank and ranked_candidates and s.rerank_provider != "hashing":
        try:
            reranked = get_rerank_provider().rerank(query, [c["text"] for c in ranked_candidates])
            # 用重排分作为最终相关性分，并按重排分排序
            for idx, score in reranked:
                ranked_candidates[idx]["rerank_score"] = float(score)
            ranked_candidates.sort(key=lambda c: -c.get("rerank_score", 0.0))
            was_reranked = True
        except Exception:
            pass  # 重排服务不可用，保留 RRF 结果与 relevance

    # 阈值过滤：
    # - 有真实重排分：按本次候选最高分归一化（相对相关性），且最高分需 >=0.2 才认可
    #   （bge-reranker 的 sigmoid 绝对分普遍偏低，直接拿 0.5 卡会误杀相关结果）
    # - 无重排：用向量/BM25 综合分直接卡阈值
    if was_reranked and ranked_candidates:
        scores = [c.get("rerank_score", 0.0) for c in ranked_candidates]
        max_score = max(scores)
        credible = max_score >= 0.2
        for c in ranked_candidates:
            c["final_rel"] = (c.get("rerank_score", 0.0) / max_score) if (credible and max_score > 0) else 0.0
    else:
        for c in ranked_candidates:
            c["final_rel"] = c["relevance"]

    kept = [c for c in ranked_candidates if c["final_rel"] >= threshold][:k]

    hits = [{
        "text": c["text"], "document_id": c["document_id"], "title": c["title"],
        "score": round(c["rrf"], 6), "relevance": round(c["final_rel"], 4),
        "vector_score": round(c["vector_score"], 4), "bm25_score": round(c["bm25_score"], 4),
    } for c in kept]
    return {
        "query": query, "hits": hits, "reranked": was_reranked,
        "hybrid": True, "threshold": threshold, "top_k": k,
        "candidates": len(candidates_by_id), "kept": len(kept),
    }
