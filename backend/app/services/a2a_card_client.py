"""A2A Agent Card 发现客户端。

新版 A2A 标准：GET {base_url}/.well-known/agent-card.json
兼容旧版：/.well-known/agent.json、直接 GET base_url

解析器宽松处理多种 Card 格式：
- 标准 A2A v1: {name, description, url, capabilities, skills:[{id,name,description,tags}]}
- 本平台格式: {name, description, skills:[str], extension_points:[str]}
"""
from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import httpx

CARD_PATHS = [
    "/.well-known/agent-card.json",   # 新版 A2A 标准
    "/.well-known/agent.json",        # 旧版 A2A 标准
    "",                                # 直接根路径（兜底）
]


def _base_url(endpoint: str) -> str:
    """从 endpoint 取 scheme://host（去掉路径与查询）。"""
    p = urlparse(endpoint)
    return urlunparse((p.scheme, p.netloc, "", "", "", ""))


def discover_card(endpoint: str, headers: dict | None = None,
                  timeout: int = 15) -> dict:
    """从外部 Agent 发现 Agent Card。返回标准化结果与原始 card。"""
    base = _base_url(endpoint)
    errors = []
    for path in CARD_PATHS:
        url = base + path
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                resp = c.get(url, headers={"Accept": "application/json", **(headers or {})})
                if resp.status_code != 200:
                    errors.append(f"{path or '/'}: HTTP {resp.status_code}")
                    continue
                card = resp.json()
                if not isinstance(card, dict):
                    errors.append(f"{path or '/'}: 非 JSON 对象")
                    continue
                return _normalize(card, url)
        except Exception as exc:
            errors.append(f"{path or '/'}: {type(exc).__name__}: {exc}")
    return {"ok": False, "error": "; ".join(errors), "card_url": base + CARD_PATHS[0]}


def _normalize(card: dict, card_url: str) -> dict:
    """把各种 Card 格式标准化。"""
    skills = []
    raw_skills = card.get("skills") or []
    for s in raw_skills:
        if isinstance(s, str):
            skills.append({"id": s, "name": s, "description": "", "tags": []})
        elif isinstance(s, dict):
            skills.append({
                "id": str(s.get("id") or s.get("name") or ""),
                "name": str(s.get("name") or s.get("id") or ""),
                "description": str(s.get("description") or ""),
                "tags": s.get("tags") or [],
            })
    # 本平台的扩展点字段也算能力
    eps = card.get("extension_points") or []
    for ep in eps:
        if ep not in [s["id"] for s in skills]:
            skills.append({"id": str(ep), "name": str(ep), "description": "扩展点", "tags": ["extension_point"]})
    caps = card.get("capabilities") or {}
    return {
        "ok": True,
        "card_url": card_url,
        "name": str(card.get("name") or ""),
        "description": str(card.get("description") or ""),
        "version": str(card.get("version") or ""),
        "protocol": str(card.get("protocol") or ""),
        "url": str(card.get("url") or ""),
        "skills": skills,
        "capabilities": caps if isinstance(caps, dict) else {},
        "raw": card,
    }
