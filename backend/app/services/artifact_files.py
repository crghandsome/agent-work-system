"""产物文件服务：Skill 脚本生成的文件统一落项目 outputs/ 目录（文件优先）。

- 每个会话/运行一个子目录，避免互相覆盖
- 后端提供列出与下载 API，前端对话显示可点击文件卡片
"""
from __future__ import annotations

import os
from pathlib import Path


def outputs_root() -> Path:
    """项目根目录 outputs/（容器内为 /srv/data/artifacts/outputs，映射到宿主机项目目录）。"""
    env = os.environ.get("AWS_OUTPUTS_DIR")
    if env:
        return Path(env)
    # 容器内挂载点；本地运行回退到项目根
    p = Path("/srv/data/artifacts/outputs")
    if p.parent.exists():
        return p
    return Path(__file__).resolve().parents[3] / "outputs"


def ensure_outputs() -> Path:
    d = outputs_root()
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_output_dir(run_key: str) -> Path:
    """为一次运行创建隔离目录，返回路径。"""
    safe = "".join(c for c in (run_key or "default") if c.isalnum() or c in "-_")[:64]
    d = ensure_outputs() / (safe or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_run_files(run_key: str) -> list[dict]:
    """列出某次运行目录下的可交付文件。"""
    d = run_output_dir(run_key)
    out = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            out.append({"name": f.name, "size": f.stat().st_size,
                        "download_url": f"/files/{run_key}/{f.name}"})
    return out
