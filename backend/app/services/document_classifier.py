"""文档体裁识别：规则打分 + LLM 兜底，带置信度与依据，结果按内容哈希缓存。

体裁清单（11 类 + 自定义）：
  contract / legal / academic / faq / meeting / manual / report / code / article / log / custom

用户手动指定优先；本模块只给"建议"，不强行覆盖用户选择。
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

GENRES = {
    "contract": "合同/协议",
    "legal": "法律/法规",
    "academic": "论文/学术",
    "faq": "FAQ/问答",
    "meeting": "会议纪要/工作文档",
    "manual": "技术手册/说明书",
    "report": "表格/报表",
    "code": "代码",
    "article": "新闻/普通文章",
    "log": "日志",
    "custom": "自定义",
}

# 规则：每个体裁一组 (正则, 权重)，命中累加分数
RULES: dict[str, list[tuple[str, float]]] = {
    "contract": [
        (r"第\s*[零一二三四五六七八九十百\d]+\s*条", 3.0),
        (r"\b甲方\b|\b乙方\b|发包人|承包人|出租方|承租方", 2.0),
        (r"本合同|本协议|签署|生效|违约责任|争议解决", 1.5),
    ],
    "legal": [
        (r"第\s*[零一二三四五六七八九十百\d]+\s*条", 2.0),
        (r"中华人民共和国|本法|条例|规定|最高人民法院|判决如下|本院认为|查明", 2.0),
        (r"^第[一二三四五六七八九十]+[章节]", 1.5),
    ],
    "academic": [
        (r"摘要|关键词|Abstract|Keywords", 2.0),
        (r"参考文献|References|实验|方法|结论|引言|Introduction", 1.5),
        (r"\[\d+\]|（\d{4}）|\(\d{4}\)", 1.0),
    ],
    "faq": [
        (r"问\s*[:：]|答\s*[:：]|Q\s*[:：]|A\s*[:：]", 3.0),
        (r"常见问题|FAQ|常见问题解答", 2.0),
        (r"^\s*\d+[\.、]\s*.+\?|？\s*$", 1.0),
    ],
    "meeting": [
        (r"会议纪要|会议记录|参会人|出席|主持人|会议时间", 3.0),
        (r"议题|讨论|决议|待办|行动项|Action Item", 1.5),
    ],
    "manual": [
        (r"安装|配置|使用说明|操作步骤|注意事项|第一步|步骤\s*\d", 2.0),
        (r"^#{1,6}\s|^\d+\.\d+", 1.0),
    ],
    "report": [
        (r"\|.*\|.*\|", 2.0),          # markdown 表格
        (r"合计|总计|小计|单位[:：]|报表|统计|同比|环比", 1.5),
    ],
    "code": [
        (r"^\s*(def |class |function |import |from \w+ import|#include|public class)", 3.0),
        (r"[{};]\s*$", 0.5),
    ],
    "log": [
        (r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", 2.5),
        (r"ERROR|WARN|INFO|DEBUG|TRACE|Exception|Stack trace", 1.5),
    ],
    "article": [
        (r"新华社|本报记者|报道|据悉|近日", 1.5),
    ],
}


def _rule_scores(text: str, filename: str = "") -> dict[str, float]:
    sample = (filename + "\n" + text[:3000])
    scores = {g: 0.0 for g in RULES}
    # 文件名关键词加权
    for g, kws in {
        "contract": ["合同", "协议", "contract"], "legal": ["法", "条例", "法规", "判决"],
        "faq": ["faq", "常见问题"], "meeting": ["纪要", "会议", "minutes"],
        "manual": ["手册", "说明", "指南", "guide", "manual"], "report": ["报表", "报告", "统计"],
        "academic": ["论文", "paper", "研究"],
    }.items():
        for kw in kws:
            if kw in filename.lower():
                scores[g] += 2.0
    for g, rules in RULES.items():
        for pattern, weight in rules:
            try:
                matches = re.findall(pattern, sample, flags=re.M | re.I)
                scores[g] += weight * min(len(matches), 5)
            except re.error:
                continue
    return scores


def _llm_classify(text: str, db=None) -> tuple[str, float, str]:
    """LLM 兜底分类，返回 (genre, confidence, reason)。"""
    try:
        from .model_gateway import ModelGateway

        sample = text[:1500]
        system = ("你是文档分类助手。只输出一个JSON对象："
                  '{"genre": "contract|legal|academic|faq|meeting|manual|report|code|article|log|custom", '
                  '"confidence": 0-1之间的小数, "reason": "不超过30字的依据"}。'
                  "不要输出其它任何文字。")
        raw, _, _ = ModelGateway(db).complete(system, [{"role": "user", "content": sample}])
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return "custom", 0.0, "LLM未返回有效JSON"
        data = json.loads(raw[start:end + 1])
        genre = str(data.get("genre", "custom")).lower()
        if genre not in GENRES:
            genre = "custom"
        conf = float(data.get("confidence", 0.5))
        return genre, min(max(conf, 0.0), 1.0), str(data.get("reason", "LLM分类"))
    except Exception as exc:
        return "custom", 0.0, f"LLM分类失败: {exc}"


@lru_cache(maxsize=256)
def _cached_classify(content_hash: str, filename: str, text_sample: str, use_llm: bool) -> tuple[str, float, str]:
    scores = _rule_scores(text_sample, filename)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top_genre, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    total = sum(scores.values())
    # 置信度：领先幅度 + 规则命中强度
    if top_score <= 0:
        confidence = 0.0
    else:
        lead = (top_score - second_score) / top_score
        confidence = min(1.0, 0.35 + 0.4 * lead + 0.25 * min(top_score / 8.0, 1.0))
    reason = f"规则命中: {top_genre} 得分{top_score:.1f}, 次高{ranked[1][0]} {second_score:.1f}"
    if confidence >= 0.6 or not use_llm:
        return top_genre, round(confidence, 2), reason
    # 置信度不足，交给 LLM
    genre, conf, llm_reason = _llm_classify(text_sample)
    return genre, conf, f"{reason}; LLM: {llm_reason}"


def classify_document(filename: str, text: str, *, use_llm: bool = True, db=None) -> dict:
    """识别体裁，返回 {genre, genre_label, confidence, reason, candidates}。"""
    content_hash = hashlib.md5((filename + text[:4000]).encode("utf-8", errors="replace")).hexdigest()
    genre, conf, reason = _cached_classify(content_hash, filename, text[:3000], use_llm)
    scores = _rule_scores(text, filename)
    candidates = [
        {"genre": g, "label": GENRES[g], "score": round(sc, 1)}
        for g, sc in sorted(scores.items(), key=lambda x: -x[1])[:4] if sc > 0
    ]
    return {
        "genre": genre, "genre_label": GENRES.get(genre, genre),
        "confidence": conf, "reason": reason,
        "candidates": candidates,
        "suggestion_only": True,  # 仅建议，用户手动指定优先
    }
