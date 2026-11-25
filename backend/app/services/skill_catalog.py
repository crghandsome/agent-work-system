"""Skill 分类与摘要（Skill 市场分组用）。

分类规则：基于 description（+ name + 指令摘要）的关键词匹配，命中优先级从上到下；
未命中归入「其他」。分类在发布时计算并落库，之后可通过 PATCH 人工覆盖。
"""
from __future__ import annotations

import re

# (分类名, 关键词...) —— 顺序即优先级
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("写作", ("写作", "文案", "润色", "撰写", "文章", "邮件", "报告", "翻译", "写作助手", "编辑")),
    ("编程", ("编程", "代码", "开发", "调试", "重构", "单元测试", "代码审查", "重构", "前端", "后端", "api", "脚本")),
    ("数据分析", ("数据分析", "统计", "报表", "指标", "可视化", "表格", "csv", "excel", "数据清洗", "归因")),
    ("检索问答", ("检索", "知识库", "问答", "faq", "客服", "文档", "搜索", "查资料", "参考资料", "知识")),
    ("运营营销", ("运营", "营销", "推广", "活动", "文案投放", "用户增长", "转化", "私域", "品牌")),
    ("项目管理", ("项目管理", "排期", "计划", "进度", "任务拆解", "敏捷", "需求", "里程碑", "会议纪要")),
    ("设计", ("设计", "ui", "交互", "原型", "视觉", "配色", "界面", "figma")),
    ("财务法务", ("财务", "合同", "法务", "合规", "发票", "报销", "审计", "条款", "风险")),
    ("人力资源", ("招聘", "面试", "绩效", "入职", "培训", "员工", "人事")),
]

# 分类卡片配色（前端用）
CATEGORY_META: dict[str, str] = {
    "写作": "蓝",
    "编程": "绿",
    "数据分析": "橙",
    "检索问答": "紫",
    "运营营销": "粉",
    "项目管理": "青",
    "设计": "黄",
    "财务法务": "红",
    "人力资源": "灰",
    "其他": "灰",
}


def classify(text: str) -> str:
    """按关键词归类，返回中文分类名。"""
    t = (text or "").lower()
    for name, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw.lower() in t:
                return name
    return "其他"


def make_summary(description: str, instructions: str, max_len: int = 120) -> str:
    """生成简洁介绍：优先用 description，不足则从指令首句补充。"""
    d = (description or "").strip().replace("\n", " ")
    if not d:
        first = (instructions or "").strip().split("\n")
        d = next((l.strip() for l in first if l.strip()), "")
    if len(d) > max_len:
        d = d[:max_len].rstrip() + "…"
    return d


def skill_category(name: str, description: str, instructions: str) -> str:
    return classify(f"{name} {description} {instructions[:200]}")


def file_tree(files: list[dict]) -> list[dict]:
    """把平铺文件清单转成目录树，供前端展示 Skill 文件结构。"""
    root: dict = {"name": "", "type": "dir", "children": []}
    index: dict = {root["name"]: root}
    for f in sorted(files or [], key=lambda x: x.get("path", "")):
        parts = [p for p in str(f.get("path", "")).split("/") if p]
        parent = root
        for i, part in enumerate(parts):
            path = "/".join(parts[: i + 1])
            is_file = i == len(parts) - 1
            node = index.get(path)
            if node is None:
                node = {"name": part, "type": "file" if is_file else "dir",
                        "children": [] if not is_file else None,
                        "size": f.get("bytes", 0) if is_file else None}
                index[path] = node
                parent["children"].append(node)
            parent = node
    return root["children"]


def split_frontmatter(text: str) -> tuple[str, str]:
    """拆分 YAML frontmatter 与正文，返回 (frontmatter 原文, 正文)。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text or "", re.S)
    if m:
        return m.group(1), m.group(2).strip()
    return "", (text or "").strip()


# 兼容旧名
strip_frontmatter = split_frontmatter
