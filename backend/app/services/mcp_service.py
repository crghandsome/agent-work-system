"""MCP 服务：MCP Server 的注册、连接发现、工具注册与生命周期。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import MCPServer, new_id
from . import tool_registry
from .mcp_client import connect_and_discover, MCPClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(description: str) -> str:
    """按功能简介分类（与 Skill/Peer 对齐的关键词规则）。"""
    t = (description or "").lower()
    rules = {
        "文件与系统": ["文件", "file", "目录", "folder", "系统", "system", "shell", "命令"],
        "数据与数据库": ["数据库", "database", "sql", "查询", "query", "数据", "data"],
        "网络与搜索": ["搜索", "search", "网页", "web", "http", "浏览器", "browser", "爬取"],
        "开发工具": ["代码", "code", "git", "github", "ide", "编译", "测试", "debug"],
        "知识与文档": ["文档", "doc", "知识", "markdown", "笔记", "notion"],
        "通讯与协作": ["消息", "message", "slack", "邮件", "email", "聊天", "chat"],
    }
    for cat, kws in rules.items():
        if any(k in t for k in kws):
            return cat
    return "其他"


def _config_of(srv: MCPServer) -> dict:
    """提取 MCP Server 的可持久化配置（真相源内容）。"""
    return {
        "id": srv.id, "name": srv.name, "description": srv.description,
        "category": srv.category, "transport": srv.transport,
        "endpoint": srv.endpoint, "command": srv.command,
        "env": srv.env_json or {}, "headers": srv.headers_json or {},
        "enabled": bool(srv.enabled),
        # 发现结果也落盘（缓存，重启后可立即用）
        "status": srv.status, "server_name": srv.server_name,
        "server_version": srv.server_version, "protocol_version": srv.protocol_version,
        "tools": srv.tools or [], "error": srv.error,
    }


def save_config(srv: MCPServer) -> str:
    """把配置写入项目 mcp/<name>/mcp.json（文件优先）。"""
    from .artifact_store import save_mcp_config

    return save_mcp_config(srv.name, _config_of(srv))


def rebuild_index_from_disk(db: Session) -> int:
    """从项目 mcp/ 目录重建 DB 索引（文件是真相源；启动时调用）。"""
    from .artifact_store import scan_mcp_configs

    created = 0
    for cfg in scan_mcp_configs():
        name = cfg.get("name")
        if not name:
            continue
        srv = db.query(MCPServer).filter(MCPServer.name == name).first()
        if srv is None:
            srv = MCPServer(id=cfg.get("id") or new_id("mcp"), name=name)
            db.add(srv)
            created += 1
        for k in ("description", "category", "transport", "endpoint", "command",
                  "enabled", "status", "server_name", "server_version",
                  "protocol_version", "tools", "error"):
            if k in cfg:
                setattr(srv, k, cfg[k])
        if "env" in cfg:
            srv.env_json = cfg["env"]
        if "headers" in cfg:
            srv.headers_json = cfg["headers"]
        db.flush()
        if srv.enabled and srv.status == "connected":
            register_mcp_tools(db, srv)
    db.commit()
    return created


def connect_server(db: Session, srv: MCPServer, refresh: bool = False) -> dict:
    """连接 MCP Server、发现工具，更新 DB + 文件，并注册工具。"""
    try:
        info = connect_and_discover(
            transport=srv.transport, endpoint=srv.endpoint, command=srv.command,
            env=srv.env_json or {}, headers=srv.headers_json or {},
        )
        srv.status = "connected"
        srv.error = ""
        srv.server_name = info["server_name"]
        srv.server_version = info["server_version"]
        srv.protocol_version = info["protocol_version"]
        srv.tools = info["tools"]
        srv.last_connected_at = _now()
        db.flush()
        register_mcp_tools(db, srv)
        save_config(srv)   # 文件优先：落盘
        db.commit()
        return {"ok": True, "tools": srv.tools, "server_name": srv.server_name}
    except Exception as exc:
        srv.status = "error"
        srv.error = f"{type(exc).__name__}: {exc}"
        db.flush()
        save_config(srv)   # 失败状态也落盘（含错误原因，用户可查看）
        db.commit()
        return {"ok": False, "error": srv.error}


def _mcp_tool_handler(server_id: str, tool_name: str):
    """生成一个闭包作为工具 handler，调用时连接对应 MCP Server 的指定工具。"""
    def handler(db, agent, **kwargs):
        srv = db.query(MCPServer).filter(MCPServer.id == server_id).first()
        if not srv or not srv.enabled:
            return f"MCP Server 不可用: {server_id}"
        client = MCPClient(srv.transport, srv.endpoint, srv.command,
                           srv.env_json or {}, headers=srv.headers_json or {})
        try:
            client.initialize()
            return client.call_tool(tool_name, kwargs)
        except Exception as exc:
            return f"MCP 工具 {tool_name} 调用失败: {type(exc).__name__}: {exc}"
        finally:
            client.close()
    return handler


def register_mcp_tools(db: Session, srv: MCPServer) -> None:
    """把 MCP Server 的工具注册进 tool_registry（供 Agent 自主调用）。"""
    if not srv.enabled or srv.status != "connected":
        return
    for t in srv.tools or []:
        name = t.get("name", "")
        if not name:
            continue
        # 命名空间：mcp_<server名>_<工具名>，避免与内置工具冲突
        tool_name = f"mcp_{srv.name}_{name}".replace("-", "_").replace(" ", "_")
        tool_registry.register_tool(
            tool_name,
            f"[MCP:{srv.name}] {t.get('description', '')}",
            t.get("input_schema") or {"type": "object", "properties": {}},
            _mcp_tool_handler(srv.id, name),
            source="mcp",
        )


def register_all_enabled(db: Session) -> int:
    """启动时把所有已启用且已连接的 MCP 工具注册进 registry。"""
    count = 0
    for srv in db.query(MCPServer).filter(MCPServer.enabled.is_(True)).all():
        before = len(tool_registry.TOOLS)
        register_mcp_tools(db, srv)
        count += len(tool_registry.TOOLS) - before
    return count


def to_dict(s: MCPServer) -> dict:
    return {
        "id": s.id, "name": s.name, "description": s.description, "category": s.category,
        "transport": s.transport, "endpoint": s.endpoint, "command": s.command,
        "env": s.env_json or {}, "headers": s.headers_json or {},
        "enabled": s.enabled, "status": s.status,
        "server_name": s.server_name, "server_version": s.server_version,
        "protocol_version": s.protocol_version, "tools": s.tools or [],
        "tool_count": len(s.tools or []), "error": s.error,
        "last_connected_at": s.last_connected_at, "created_at": str(s.created_at),
    }
