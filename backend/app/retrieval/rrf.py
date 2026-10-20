"""RRF（Reciprocal Rank Fusion）融合。

## 为什么用 RRF 而不是加权分数和

两路召回的分数**不可比**：

| 通道 | 分数含义 | 典型范围 |
|---|---|---|
| 向量 | cosine 相似度 | 0-1，分布集中在 0.3-0.9 |
| BM25 | 词频-逆文档频率加权 | 0-50+，无上界 |

直接相加会让 BM25 主导（数值大得多）。归一化也不可靠——
两路分布的形态完全不同，Min-Max 归一化会把「一个优秀但孤立的结果」
拉成 1.0，掩盖真实差距。

**RRF 只用排名，不用分数**：`score = Σ 1/(k + rank)`，k 通常取 60。

k=60 的意义：让第 1 名和第 2 名的差距（1/61 vs 1/62）足够小，
避免「某一路的第 1 名」压倒性地主导。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: RRF 平滑常数。60 是原论文经验值。
RRF_K = 60


@dataclass(slots=True)
class FusedHit:
    chunk_id: str
    rrf_score: float
    #: 该结果在各路的原始排名与分数，便于排查「为什么这条排第一」
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 出现在几路里。两路都命中的通常更可信
    hit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "rrf_score": round(self.rrf_score, 6),
            "hit_count": self.hit_count,
            "sources": self.sources,
        }


def fuse(
    rankings: dict[str, list[tuple[str, float]]],
    *,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """融合多路排名。

    `rankings`：`{通道名: [(chunk_id, 原始分数), ...]}`，**列表顺序即排名**。

    返回按 RRF 分数降序的列表。
    """
    scores: dict[str, float] = {}
    sources: dict[str, dict[str, Any]] = {}
    hit_counts: dict[str, int] = {}

    for channel, items in rankings.items():
        for rank, (cid, raw_score) in enumerate(items, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            sources.setdefault(cid, {})[channel] = {
                "rank": rank,
                "score": round(float(raw_score), 4),
            }
            hit_counts[cid] = hit_counts.get(cid, 0) + 1

    ordered = sorted(scores.items(), key=lambda x: -x[1])
    if limit is not None:
        ordered = ordered[:limit]

    return [
        FusedHit(
            chunk_id=cid,
            rrf_score=score,
            sources=sources[cid],
            hit_count=hit_counts[cid],
        )
        for cid, score in ordered
    ]


def explain(rankings: dict[str, list[tuple[str, float]]], k: int = RRF_K) -> str:
    """生成可读的融合说明，用于调试与日志。"""
    lines: list[str] = []
    for channel, items in rankings.items():
        lines.append(f"[{channel}] {len(items)} 条")
        for rank, (cid, score) in enumerate(items[:3], start=1):
            contribute = 1.0 / (k + rank)
            lines.append(f"  #{rank} {cid[:12]} 原始 {score:.3f} → 贡献 {contribute:.5f}")

    fused = fuse(rankings, k=k)
    if fused:
        lines.append("[融合后 Top3]")
        for h in fused[:3]:
            chans = "+".join(h.sources.keys())
            lines.append(f"  {h.chunk_id[:12]} {h.rrf_score:.5f} 来自 {chans}")
    return "\n".join(lines)
