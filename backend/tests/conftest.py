"""pytest 共享夹具。

**核心原则：单测不需要模型服务，也不需要数据库。**

- Embedder：注入 FakeEmbedder，返回确定性向量
- 数据库：需要 DB 的测试标记为 `@pytest.mark.db`，默认跳过
- Redis：用 fakeredis 替代

这样 `make test` 在任何机器上都能跑通，新人 clone 下来不用先折腾环境。
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator

import pytest

from app.core.config import settings  # noqa: E402


# ---------------------------------------------------------------------------
# 确定性假向量
# ---------------------------------------------------------------------------
class FakeEmbedder:
    """不连任何服务的 embedder。

    关键设计：同一个文本永远得到同一个向量，且语义相近的文本向量夹角更小
    （用字符 3-gram 哈希模拟）。这样检索相关的单测才能写出有意义的断言。
    """

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.EMBED_DIM
        self.calls: list[list[str]] = []

    def _vec(self, text: str) -> list[float]:
        # 用字符 3-gram 的哈希散布到各维度，再做 L2 归一化
        acc = [0.0] * self.dim
        t = text.strip().lower()
        if not t:
            acc[0] = 1.0
            return acc
        for i in range(len(t) - 2):
            gram = t[i : i + 3]
            h = hash(gram)
            idx = abs(h) % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            acc[idx] += sign
        norm = math.sqrt(sum(x * x for x in acc)) or 1.0
        return [x / norm for x in acc]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    async def embed_query(self, question: str) -> list[float]:
        self.calls.append([question])
        return self._vec(question)

    async def health(self) -> dict[str, object]:
        return {"ok": True, "dim": self.dim, "model": "fake"}


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
@pytest.fixture
async def fake_redis() -> AsyncIterator[object]:
    fakeredis = pytest.importorskip("fakeredis", reason="需要 dev 依赖 fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# 数据库（默认跳过）
# ---------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: 需要真实 PostgreSQL 的测试（默认跳过）")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m") and "db" in (config.getoption("-m") or ""):
        return
    skip_db = pytest.mark.skip(reason="需要真实 PostgreSQL；用 pytest -m db 单独运行")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)
