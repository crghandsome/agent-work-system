"""SQLAlchemy engine / session。启动时自动建表（阶段2开发模式）。"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_engine = None
_session_factory: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().mysql_url, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def init_db() -> None:
    """开发期自动建表；生产迁移后续引入 Alembic。"""
    Base.metadata.create_all(get_engine())


def get_db() -> Generator[Session, None, None]:
    """会话依赖。

    注意：FastAPI yield 依赖的 teardown commit 发生在响应发送之后，会造成
    "创建后立刻查询读不到" 的竞态。因此所有变更端点必须在返回前显式 commit；
    这里的 teardown commit 仅作为兜底。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
