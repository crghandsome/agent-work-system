"""审计与计量（设计文档 8.1/8.2 首期实现）。

所有关键动作（发布/撤回/运行/A2A/Peer 管理）追加 AuditEvent；计量按主体累计。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditEvent, UsageCounter, new_id


def audit(db: Session, action: str, target: str = "", actor: str = "system", detail: dict | None = None) -> None:
    db.add(AuditEvent(id=new_id("aud"), action=action, actor=actor, target=target, detail=detail or {}))


def record_usage(db: Session, subject: str, kind: str, *, count: int = 1,
                 input_tokens: int = 0, output_tokens: int = 0) -> None:
    row = db.query(UsageCounter).filter(UsageCounter.subject == subject, UsageCounter.kind == kind).first()
    if row is None:
        row = UsageCounter(id=new_id("usage"), subject=subject, kind=kind)
        db.add(row)
    row.count = (row.count or 0) + count
    row.input_tokens = (row.input_tokens or 0) + input_tokens
    row.output_tokens = (row.output_tokens or 0) + output_tokens


def get_usage(db: Session, subject: str | None = None) -> list[dict]:
    q = db.query(UsageCounter)
    if subject:
        q = q.filter(UsageCounter.subject == subject)
    return [
        {"subject": r.subject, "kind": r.kind, "count": r.count,
         "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
        for r in q.order_by(UsageCounter.subject).all()
    ]
