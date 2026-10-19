"""假 Embedder，用于管道测试。

**为什么可以假**：管道要验证的是「阶段契约、落盘、指纹复用、重跑」，
与向量质量无关。用假 embedder 能让这些测试在**不开模型服务**的情况下跑通。

`FakeEmbedder` 与 `tests/conftest.py` 里的同类保持一致，
但这里独立一份，避免 tests 包的导入耦合。
"""

from __future__ import annotations

import math


class FakeEmbedder:
    """确定性向量生成器。

    同一文本永远得到同一向量，且不同文本的向量不同 —— 足够验证读写与复用逻辑。
    """

    def __init__(self, dim: int = 2560) -> None:
        self.dim = dim
        self.calls: list[int] = []   # 记录每次调用的批大小

    def _vec(self, text: str) -> list[float]:
        acc = [0.0] * self.dim
        t = text.strip().lower()
        if not t:
            acc[0] = 1.0
            return acc
        for i in range(len(t) - 2):
            h = hash(t[i : i + 3])
            acc[abs(h) % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in acc)) or 1.0
        return [x / norm for x in acc]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [self._vec(t) for t in texts]

    async def embed_query(self, question: str) -> list[float]:
        return self._vec(question)
