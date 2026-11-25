"""本地 BM25 关键词检索（无外部依赖）。

BM25 是经典的关键词相关性算法，考虑词频(TF)、逆文档频率(IDF)和文档长度。
中文用单字+二元组分词，英文按单词，与 embedding 的 tokenizer 保持一致口径。
"""
from __future__ import annotations

import math
import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    # 英文/数字词
    tokens.extend(re.findall(r"[a-z0-9]{2,}", text))
    # 中文单字 + 相邻二元组（捕捉词组）
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(cjk)
    tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


class BM25:
    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in corpus]
        self.n = len(self.docs)
        self.avgdl = sum(len(d) for d in self.docs) / self.n if self.n else 0
        self.df: Counter[str] = Counter()
        for d in self.docs:
            self.df.update(set(d))
        self.tfs = [Counter(d) for d in self.docs]

    def idf(self, term: str) -> float:
        # 经典 BM25 IDF，+1 防止负数
        return math.log(1 + (self.n - self.df.get(term, 0) + 0.5) / (self.df.get(term, 0) + 0.5))

    def scores(self, query: str) -> list[float]:
        qterms = tokenize(query)
        out = []
        for i, tf in enumerate(self.tfs):
            dl = len(self.docs[i])
            score = 0.0
            for t in qterms:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = self.idf(t)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                score += idf * (f * (self.k1 + 1)) / denom
            out.append(score)
        return out
