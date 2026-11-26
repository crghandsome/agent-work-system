"""Agent 工具注册表：把平台能力标准化为 LLM 可自主调用的工具。

内置工具（4 类）：
- knowledge_search：检索知识库（RAG + 重排）
- skill_script：执行 Skill 包内脚本（受限：仅 Python，超时，cwd 锁在 skill 目录）
- a2a_delegate：委托其他 Agent / 团队（经 A2A）
- plugin_action：调用已安装插件的行为

扩展点（预留）：
- MCP 工具：register_tool() 可动态注册外部 MCP 工具
- 自定义工具：用户也可通过 register_tool() 注册

每个工具定义：{name, description, parameters(JSON Schema), handler}
handler 签名：(db, agent, **kwargs) -> str
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

TOOLS: dict[str, dict] = {}

# 最近一次 skill_script 使用的 run_key（线程局部，供 SSE 层发 file 事件）
import threading
_ctx = threading.local()


def _set_last_run_key(key: str) -> None:
    _ctx.last_run_key = key


def get_last_run_key() -> str:
    return getattr(_ctx, "last_run_key", "")


def register_tool(name: str, description: str, parameters: dict,
                  handler: Callable, source: str = "builtin") -> None:
    """注册工具（MCP / 自定义工具也走这个入口）。"""
    TOOLS[name] = {"name": name, "description": description,
                   "parameters": parameters, "handler": handler, "source": source}


def list_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"],
             "parameters": t["parameters"], "source": t["source"]}
            for t in TOOLS.values()]


def get_tool(name: str) -> dict | None:
    return TOOLS.get(name)


def invoke(db: Session, agent, name: str, args: dict) -> str:
    """调用工具，返回字符串结果（供 LLM 观察）。"""
    t = TOOLS.get(name)
    if not t:
        return f"工具不存在: {name}。可用工具: {list(TOOLS)}"
    try:
        return t["handler"](db, agent, **(args or {}))
    except Exception as exc:
        return f"工具 {name} 执行失败: {type(exc).__name__}: {exc}"


# ── 内置工具实现 ──

def _knowledge_search(db, agent, query: str, top_k: int = 5) -> str:
    from . import rag_service

    r = rag_service.search(query=query, top_k=top_k)
    if not r["hits"]:
        return "知识库无相关结果。"
    return "\n---\n".join(f"[{h['title']}] {h['text'][:600]}" for h in r["hits"])


def _skill_script(db, agent, skill_name: str, script: str, args: str = "", run_key: str = "") -> str:
    """执行 Skill 包内脚本，产物落 outputs/<run_key>/，返回文本含 [交付文件] 标记。

    兼容多层目录；参数用 shlex 正确拆分；若脚本支持 --out/--json 且用户未指定，
    自动指向运行目录，确保生成文件可被交付。
    """
    import shlex
    import time
    from .artifact_store import skill_dir
    from .artifact_files import run_output_dir, list_run_files

    # 未指定 run_key 时自动生成隔离目录（每次调用独立，避免覆盖）
    if not run_key:
        run_key = f"run-{int(time.time()*1000)}"
        # 让调用方（SSE）能拿到本次 key：写进线程局部变量
        _set_last_run_key(run_key)

    d = skill_dir(skill_name)
    if not d.exists():
        return f"Skill '{skill_name}' 不存在"
    # 脚本名容错：LLM 常传 "scripts/build_daily_report.py" 或 "./scripts/x.py"
    bare = script.replace("\\", "/").split("/")[-1]
    scripts_dirs = [d / "scripts"] + [p / "scripts" for p in d.iterdir() if p.is_dir()]
    scripts_root = next((s for s in scripts_dirs if s.is_dir() and (s / bare).is_file()), None)
    if not scripts_root:
        return f"脚本不存在: {script}（已查 {[str(s) for s in scripts_dirs]}）"
    target = (scripts_root / bare).resolve()
    if not str(target).startswith(str(scripts_root.resolve())):
        return f"脚本路径非法: {script}"
    if target.suffix != ".py":
        return "仅支持执行 .py 脚本"

    out_dir = run_output_dir(run_key)
    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()
    # 强制把 --out/--json 指向隔离目录：LLM 常自带绝对路径（如 /srv/data/artifacts/日报.docx），
    # 那样文件落在 outputs/ 之外，交付标记就检测不到。这里一律覆盖为用户可下载目录。
    def _force(flag, default_name):
        val = str(out_dir / default_name)
        if flag in tokens:
            i = tokens.index(flag)
            if i + 1 < len(tokens):
                tokens[i + 1] = val
            else:
                tokens.insert(i + 1, val)
        else:
            tokens.extend([flag, val])
    _force("--out", f"{skill_name}_result.docx")
    _force("--json", f"{skill_name}_data.json")

    env = {**os.environ}
    env.setdefault("QXB_CACHE_BACKEND", "mysql")
    for k in ("DSH_HOME", "AWS_MCP_CONFIG", "ANTHROPIC_BASE_URL"):
        if k in env:
            env[k] = env[k]
    try:
        proc = subprocess.run(
            ["python", str(target), *tokens], cwd=str(target.parent),
            capture_output=True, text=True, timeout=600, env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = (out or "(无输出)") + (f"\n[stderr] {err[:1500]}" if err else "")
    except subprocess.TimeoutExpired:
        return "脚本执行超时（600秒）"

    # 交付文件标记：SSE 层解析后发 file 事件，前端显示下载卡片
    files = list_run_files(run_key)
    delivered = [f for f in files if not f["name"].startswith(".")]
    if delivered:
        text += "\n" + "\n".join(
            f"[交付文件] {f['name']}|{f['download_url']}|{f['size']}" for f in delivered
        )
    return text


def _a2a_delegate(db, agent, agent_name: str, message: str) -> str:
    """委托其他 Agent（A2A 内部任务）。"""
    from . import a2a_service

    task, created = a2a_service.get_or_create_task(
        db, idempotency_key=f"tool:{agent.id if agent else 'x'}:{agent_name}:{hash(message)}",
        direction="internal", agent_name=agent_name, message=message,
    )
    if created and task.state == "submitted":
        task = a2a_service.run_internal_task(db, task)
    return f"[{task.state}] {task.result or task.error or ''}"


def _plugin_action(db, agent, plugin_key: str, action: str = "info") -> str:
    """查询插件能力/配置（行为驱动由 Runtime 自动执行，这里供 LLM 了解已装插件）。"""
    from ..models import Plugin

    row = db.query(Plugin).filter(Plugin.key == plugin_key).first()
    if not row:
        return f"插件不存在: {plugin_key}"
    if action == "info":
        return f"{row.name}: {row.description} · 扩展点 {row.extension_point} · 行为 {row.config or {}}"
    return f"未知操作: {action}"


_BUILTINS_REGISTERED = False


def init_builtin_tools() -> None:
    """注册内置工具（幂等）。

    注意：不能用 `if TOOLS: return` 判断——启动时 MCP 工具已注册进 TOOLS，
    那样会导致内置工具（skill_script 等）永远注册不上。用独立标志位。
    """
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    _BUILTINS_REGISTERED = True
    register_tool(
        "knowledge_search", "检索知识库，返回与查询相关的文档片段",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "检索查询"},
            "top_k": {"type": "integer", "description": "返回条数", "default": 5}},
         "required": ["query"]},
        _knowledge_search,
    )
    register_tool(
        "skill_script",
        "执行某个 Skill 包 scripts/ 目录下的 Python 脚本，可生成docx/xlsx/pdf等文件并交付给用户下载。"
        "用户要求生成报告/文档/表格时必须用此工具执行对应 Skill 的脚本；"
        "脚本会自动把产物保存到隔离目录，返回值含[交付文件]标记，前端会显示可点击下载卡片。",
        {"type": "object", "properties": {
            "skill_name": {"type": "string", "description": "Skill 名称"},
            "script": {"type": "string", "description": "脚本文件名（含 .py）"},
            "args": {"type": "string", "description": "命令行参数字符串，如 --date 20261104；输出路径会自动指定，无需手写"}},
         "required": ["skill_name", "script"]},
        _skill_script,
    )
    register_tool(
        "a2a_delegate", "把子任务委托给另一个 Agent 并拿回结果",
        {"type": "object", "properties": {
            "agent_name": {"type": "string", "description": "目标 Agent 名称"},
            "message": {"type": "string", "description": "任务内容"}},
         "required": ["agent_name", "message"]},
        _a2a_delegate,
    )
    register_tool(
        "plugin_action", "查询已安装插件的能力与配置",
        {"type": "object", "properties": {
            "plugin_key": {"type": "string", "description": "插件 key"},
            "action": {"type": "string", "description": "操作，默认 info", "default": "info"}},
         "required": ["plugin_key"]},
        _plugin_action,
    )
