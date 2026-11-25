"""基础路由：Skill 列表/详情、插件列表、文档管理（其余全量路由在 api_full.py）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Document, Plugin, Skill
from ..schemas import DocumentCreate, PluginOut, SkillOut
from ..services import rag_service

router = APIRouter()

# 与 api_full.py 的静态路由区分：这些路径由那边处理
RESERVED = {"categories", "dialog", "upload", "upload-md", "template"}


@router.get("/skills", response_model=list[SkillOut])
def list_skills(db: Session = Depends(get_db)):
    return db.query(Skill).order_by(Skill.created_at.desc()).all()


@router.get("/skills/{name}", response_model=SkillOut)
def get_skill(name: str, db: Session = Depends(get_db)):
    if name in RESERVED:
        raise HTTPException(404, "Skill 不存在")
    skill = db.query(Skill).filter(Skill.name == name).first()
    if not skill:
        raise HTTPException(404, f"Skill '{name}' 不存在")
    return skill


@router.get("/plugins", response_model=list[PluginOut])
def list_plugins(db: Session = Depends(get_db)):
    # 返回全部插件（含停用），前端据此显示启用/停用状态
    return db.query(Plugin).order_by(Plugin.builtin.desc(), Plugin.key).all()


@router.post("/rag/documents")
def ingest(body: DocumentCreate, db: Session = Depends(get_db)):
    doc = rag_service.ingest_document(db, title=body.title, content=body.content,
                                      source_filename=f"{body.title}.txt", file_type=".txt")
    db.commit()
    return {"id": doc.id, "title": doc.title, "chunk_count": doc.chunk_count,
            "chunk_strategy": doc.chunk_strategy}


@router.post("/rag/documents/preview")
async def preview_document(
    file: UploadFile = File(...),
    genre: str = Form(""),
    chunk_size: int = Form(500),
    overlap: int = Form(50),
    db: Session = Depends(get_db),
):
    """解析预览（不入库）：上传原始文件 → 后端统一解码/识别/分块，避免编码差异乱码。"""
    from ..services import chunking_service, document_classifier

    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    filename = file.filename or "document.txt"
    try:
        text, _ftype = chunking_service.parse_file(filename, data)
    except Exception as exc:
        raise HTTPException(422, f"文件解析失败: {exc}")
    if not text.strip():
        raise HTTPException(422, "文件中没有可入库的文本内容")

    suggestion = document_classifier.classify_document(filename, text, db=db)
    final_genre = genre or suggestion["genre"]
    blocks = chunking_service.chunk_document(
        text, final_genre, {"chunk_size": chunk_size, "overlap": overlap},
        doc_title=filename.rsplit(".", 1)[0],
    )
    return {
        "suggestion": suggestion,
        "genre": final_genre,
        "genre_label": document_classifier.GENRES.get(final_genre, final_genre),
        "chunk_count": len(blocks),
        "chunks": [
            {"chunk_index": b["chunk_index"], "text": b["text"],
             "embedding_text": b["embedding_text"], "locator": b["locator"], "kind": b["kind"]}
            for b in blocks[:30]
        ],
        "truncated": len(blocks) > 30,
    }


@router.post("/rag/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    genre: str = Form(""),                 # 用户手动指定的体裁，优先于自动识别
    chunk_size: int = Form(500),
    overlap: int = Form(50),
    db: Session = Depends(get_db),
):
    """上传文件入库：体裁用户指定优先，否则自动识别；支持自定义分块参数。"""
    from ..services import chunking_service, document_classifier

    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    filename = file.filename or "document.txt"
    try:
        text, ftype = chunking_service.parse_file(filename, data)
    except Exception as exc:
        raise HTTPException(422, f"文件解析失败: {exc}")
    if not text.strip():
        raise HTTPException(422, "文件中没有可入库的文本内容")

    # 体裁：用户手动指定优先，否则自动识别（识别仅建议，不强制）
    suggestion = document_classifier.classify_document(filename, text, db=db)
    final_genre = genre or suggestion["genre"]
    title = filename.rsplit(".", 1)[0] if "." in filename else filename
    doc = rag_service.ingest_document(
        db, title=title, content=text, genre=final_genre,
        genre_confidence=suggestion["confidence"], detect_reason=suggestion["reason"],
        chunking_config={"chunk_size": chunk_size, "overlap": overlap},
        source_filename=filename, file_type=ftype,
    )
    db.commit()
    return {"id": doc.id, "title": doc.title, "file_type": ftype,
            "genre": doc.genre, "genre_label": document_classifier.GENRES.get(doc.genre, doc.genre),
            "genre_source": "user" if genre else "auto",
            "suggestion": suggestion,
            "chunk_count": doc.chunk_count, "content_length": len(text)}


@router.delete("/rag/documents/{doc_id}")
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    """删除文档：DB 记录 + Qdrant 中该文档的所有向量点。"""
    d = db.query(Document).filter(Document.id == doc_id).first()
    if not d:
        raise HTTPException(404, "文档不存在")
    try:
        from qdrant_client import models as qm

        client = rag_service.get_qdrant()
        s = rag_service.get_settings()
        # 按 document_id 过滤删除点
        client.delete(
            collection_name=s.qdrant_collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(
                    key="document_id", match=qm.MatchValue(value=doc_id))])
            ),
        )
    except Exception:
        pass  # Qdrant 删除失败不阻断 DB 删除
    db.delete(d)
    db.commit()
    return {"ok": True, "id": doc_id}


@router.get("/rag/documents/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    """文档详情：返回完整原始内容（不是分块）。"""
    d = db.query(Document).filter(Document.id == doc_id).first()
    if not d:
        raise HTTPException(404, "文档不存在")
    return {"id": d.id, "title": d.title, "content": d.content,
            "source_filename": d.source_filename, "file_type": d.file_type,
            "chunk_strategy": d.chunk_strategy, "chunk_count": d.chunk_count,
            "genre": d.genre, "genre_confidence": d.genre_confidence,
            "detect_reason": d.detect_reason,
            "created_at": str(d.created_at)}


@router.post("/rag/search")
def search(body: DocumentCreate, db: Session = Depends(get_db)):
    result = rag_service.search(query=body.content)
    return result


@router.get("/rag/documents")
def list_documents(db: Session = Depends(get_db)):
    docs = db.query(Document).order_by(Document.created_at.desc()).limit(100).all()
    return [{"id": d.id, "title": d.title, "chunk_count": d.chunk_count,
             "source_filename": d.source_filename, "file_type": d.file_type,
             "chunk_strategy": d.chunk_strategy, "genre": d.genre,
             "genre_confidence": d.genre_confidence, "created_at": str(d.created_at)}
            for d in docs]
