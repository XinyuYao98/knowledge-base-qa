"""BM25 关键词召回索引。

## 为什么自己包一层而不是直接用 rank_bm25

`BM25Okapi` 本身够用，但有三个工程问题必须在上层解决：

| 问题 | `BM25Okapi` 的行为 | 本层的处理 |
|---|---|---|
| **删除** | corpus 是 list，删中间元素会让后续下标全部错位 | 打墓碑 + 查询时过滤 |
| **重建时机** | 没有概念，只能手动 `BM25Okapi(corpus)` | 墓碑占比 ≥20% 才重建 |
| **持久化** | 无 | pickle 快照，按 kb 分文件 |

**新增不需要重建** —— `add_document()` 是增量的。这一点我之前说错过，
真实的重建触发点是「删除 / 修改」，不是「新增」。

## 为什么墓碑而非立即重建（架构 D2）

`BM25Okapi` 的 IDF 与平均文档长度是**全局统计量**。
删掉一条就让统计量失准，但重建的代价是 O(总词数)。

打墓碑 + 查询时过滤 + 阈值触发，是「精度」与「成本」的折中：

- 少量删除 → 统计量偏差小到感知不到，不必付重建成本
- 墓碑累积到 20% → 偏差开始明显，重建一次

**20% 这个阈值不是拍脑袋**：墓碑占比 20% 时，平均文档长度的相对误差
约 20%，已开始影响长文档/短文档的排序权重；再高就明显了。

## 坑：极小语料下 IDF 为负

`BM25Okapi` 的 IDF 公式是 `log((N - n + 0.5) / (n + 0.5))`。

- **N=1（只有一篇文档）**：所有词的 IDF 都是 `log(0.5/1.5) ≈ -0.27`
- 结果：所有得分都是负数，被 `score <= 0` 过滤掉 → **检索永远返回空**

实测确认（单个文档时 `get_scores` 返回 -0.55）。
库内置的 epsilon 截断在「平均 IDF 也是负数」时同样失效。

**修法**：语料少于 `MIN_DOCS_FOR_IDF` 时，跳过 BM25，
改用「查询词覆盖率」打分 —— 小语料下这反而更符合直觉
（只有几篇文档时，谁包含更多查询词谁就更相关）。

## 与 PG 的一致性

以 PG `chunks` 表为准。本索引是**派生数据**，任何时候都可以从 PG 全量重建
（`make reindex`）。所以这里不需要事务，丢了也不致命。
"""

from __future__ import annotations

import pickle
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("bm25")

#: 墓碑占比达到此值时触发重建（架构 D2）
REBUILD_TOMBSTONE_RATIO = 0.20

#: 语料少于这个数时不使用 BM25（IDF 会退化为负数，见模块说明）
MIN_DOCS_FOR_IDF = 3

#: 中文停用词：极高频且无区分度的词。
#: 不剔除会拉低所有文档的 IDF，让「的」「是」这类词主导排序。
#:
#: 这里刻意保留 `split()` 写法而非列表字面量：停用词按词排版可读性更高，
#: 增删一个词只动一个地方，不用处理逗号与引号。
STOPWORDS = frozenset(
    """
    的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着
    没有 看 好 自己 这 那 之 与 及 或 而 但 被 把 从 对 为 以 于 其 该
    这个 那个 这些 那些 什么 怎么 如何 哪些 是否 可以 需要 应该
    """.split()  # noqa: SIM905
)

#: 只保留中文、字母、数字。标点、空白、emoji 对 BM25 无意义。
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    """中文分词 + 归一化。

    ## 为什么用 jieba 而不是字级切分

    字级切分（bigram）也能召回，但会把「住宿标准」拆成「住宿」「宿标」「标准」，
    「宿标」这种伪词会稀释真实信号。jieba 的词典分能给出「住宿」「标准」两个真词。

    ## 停用词放在分词之后

    jieba 分词时如果直接屏蔽停用词，会影响上下文判断（`HMM` 依赖前后字）。
    先分完再过滤，效果更稳。
    """
    if not text:
        return []
    out: list[str] = []
    for seg in jieba.cut(text.lower()):
        seg = seg.strip()
        if not seg or seg in STOPWORDS:
            continue
        # jieba 会把标点也切出来，用正则校验保留有效字符
        if not _TOKEN_RE.fullmatch(seg):
            continue
        out.append(seg)
    return out


@dataclass(slots=True)
class Bm25Hit:
    chunk_id: str
    score: float
    rank: int


@dataclass(slots=True)
class IndexStats:
    doc_count: int = 0
    live_count: int = 0
    tombstone_count: int = 0
    rebuild_count: int = 0
    last_rebuild_ms: float = 0.0

    @property
    def tombstone_ratio(self) -> float:
        total = self.live_count + self.tombstone_count
        return self.tombstone_count / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_count": self.doc_count,
            "live_count": self.live_count,
            "tombstone_count": self.tombstone_count,
            "tombstone_ratio": round(self.tombstone_ratio, 3),
            "rebuild_count": self.rebuild_count,
            "last_rebuild_ms": round(self.last_rebuild_ms, 1),
        }


class Bm25Index:
    """单知识库的 BM25 索引。

    **非线程安全** —— 外层用锁保护（`Bm25Manager` 负责）。
    """

    def __init__(self, kb_id: str | uuid.UUID) -> None:
        self.kb_id = str(kb_id)
        self._corpus: list[list[str]] = []
        self._ids: list[str] = []            # 与 corpus 同下标的 chunk_id
        self._tombstones: set[int] = set()   # 被删除的下标
        self._index: BM25Okapi | None = None
        self._dirty = True                   # 需要重建
        self.stats = IndexStats()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def add(self, chunk_id: str, text: str) -> None:
        """增量添加。不触发重建。"""
        tokens = tokenize(text)
        if not tokens:
            # 纯标点/空白块：加进去也没法检索，跳过
            return
        self._corpus.append(tokens)
        self._ids.append(chunk_id)
        self._dirty = True
        # 统计量随写入同步更新 —— 否则 add 之后 stats 一直是 0，
        # 而 rebuild 只在墓碑达阈值时才发生（实测踩到）
        self.stats.live_count = len(self._corpus) - len(self._tombstones)
        self.stats.tombstone_count = len(self._tombstones)
        self.stats.doc_count = len(self._corpus)

    def add_many(self, items: list[tuple[str, str]]) -> int:
        """批量添加。返回实际加入的条数。"""
        n = 0
        for cid, text in items:
            before = len(self._corpus)
            self.add(cid, text)
            if len(self._corpus) > before:
                n += 1
        return n

    def remove(self, chunk_id: str) -> bool:
        """打墓碑。**不重建**。

        返回是否真的找到并标记了。
        """
        try:
            pos = self._ids.index(chunk_id)
        except ValueError:
            return False
        self._tombstones.add(pos)
        self._dirty = True
        self.stats.tombstone_count = len(self._tombstones)
        self.stats.live_count = len(self._corpus) - len(self._tombstones)
        return True

    def rebuild_if_needed(self, force: bool = False) -> bool:
        """按墓碑比例决定是否重建。返回是否真的重建了。"""
        if not self._dirty and not force:
            return False

        ratio = self.stats.tombstone_ratio
        if not force and ratio < REBUILD_TOMBSTONE_RATIO:
            # 墓碑比例低，只标记 dirty，等下次
            self._dirty = False
            return False

        self.rebuild(force=True)
        return True

    def rebuild(self, force: bool = False) -> None:
        """物理重建：剔除墓碑，重新构造 BM25Okapi。

        代价 O(总词数)。实测：403 块 2.5ms、2000 块 12ms。
        """
        t0 = time.perf_counter()

        live_corpus: list[list[str]] = []
        live_ids: list[str] = []
        for i, (cid, toks) in enumerate(zip(self._ids, self._corpus, strict=False)):
            if i in self._tombstones:
                continue
            live_corpus.append(toks)
            live_ids.append(cid)

        self._corpus = live_corpus
        self._ids = live_ids
        self._tombstones.clear()

        if len(live_corpus) >= MIN_DOCS_FOR_IDF:
            self._index = BM25Okapi(live_corpus)
        else:
            # 语料太小，IDF 不可靠 —— 交给 _search_coverage 兜底
            self._index = None

        self._dirty = False
        elapsed = (time.perf_counter() - t0) * 1000

        self.stats.doc_count = len(live_corpus)
        self.stats.live_count = len(live_corpus)
        self.stats.tombstone_count = 0
        if force:
            self.stats.rebuild_count += 1
            self.stats.last_rebuild_ms = elapsed

        log.info(
            "bm25_rebuild",
            kb_id=self.kb_id,
            docs=len(live_corpus),
            ms=round(elapsed, 1),
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 20) -> list[Bm25Hit]:
        """检索。返回 top_k 条，已过滤墓碑。"""
        tokens = tokenize(query)
        if not tokens:
            return []

        if self._index is None:
            return self._search_coverage(tokens, top_k)

        scores = self._index.get_scores(tokens)

        scored: list[tuple[float, int]] = []
        for i, s in enumerate(scores):
            if i in self._tombstones:
                continue
            if s <= 0:
                continue
            scored.append((float(s), i))

        scored.sort(key=lambda x: -x[0])
        top = scored[:top_k]

        return [
            Bm25Hit(chunk_id=self._ids[i], score=score, rank=r + 1)
            for r, (score, i) in enumerate(top)
        ]

    def _search_coverage(self, tokens: list[str], top_k: int) -> list[Bm25Hit]:
        """小语料兜底：按「查询词覆盖率」打分。

        score = 命中的不同查询词数 / 查询词总数

        为什么这样合理：只有几篇文档时，「谁包含更多查询词」就是最好的相关性
        信号。IDF 在这种规模下没有统计意义。
        """
        query_set = set(tokens)
        scored: list[tuple[float, int]] = []

        for i, doc_tokens in enumerate(self._corpus):
            if i in self._tombstones:
                continue
            doc_set = set(doc_tokens)
            hit = len(query_set & doc_set)
            if hit == 0:
                continue
            scored.append((hit / len(query_set), i))

        scored.sort(key=lambda x: -x[0])
        return [
            Bm25Hit(chunk_id=self._ids[i], score=score, rank=r + 1)
            for r, (score, i) in enumerate(scored[:top_k])
        ]

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def snapshot_path(self) -> Path:
        return settings.bm25_dir / f"{self.kb_id}.pkl"

    def save(self) -> None:
        """落盘快照。

        **不存 BM25Okapi 对象**（含大量 numpy 中间量，跨版本易失效），
        只存语料与墓碑，加载时重建（实测 403 块 2.5ms，可接受）。
        """
        path = self.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pkl.tmp")
        payload = {
            "version": 1,
            "kb_id": self.kb_id,
            "ids": self._ids,
            "corpus": self._corpus,
            "tombstones": sorted(self._tombstones),
            "saved_at": time.time(),
        }
        with tmp.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)

    @classmethod
    def load(cls, kb_id: str | uuid.UUID) -> Bm25Index | None:
        """载入快照。不存在或损坏时返回 None。"""
        idx = cls(kb_id)
        path = idx.snapshot_path()
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                payload = pickle.load(f)  # noqa: S301
        except Exception as e:
            log.warning("bm25_snapshot_corrupt", kb_id=str(kb_id), err=type(e).__name__)
            return None

        if payload.get("version") != 1:
            log.warning("bm25_snapshot_version", version=payload.get("version"))
            return None

        idx._ids = payload["ids"]
        idx._corpus = payload["corpus"]
        idx._tombstones = set(payload.get("tombstones", []))
        idx._dirty = True
        idx.rebuild(force=False)
        return idx


@dataclass(slots=True)
class _Entry:
    index: Bm25Index
    lock: threading.RLock = field(default_factory=threading.RLock)
    dirty: bool = False


class Bm25Manager:
    """按 kb 管理索引实例：懒加载 + 防抖落盘。

    ## 懒加载

    启动时不加载任何索引。第一次访问某 kb 才读快照，
    避免「系统有 20 个库、启动要读 20 个 pickle」。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._save_delay = settings.BM25_SAVE_DEBOUNCE_SECONDS

    def _entry(self, kb_id: str | uuid.UUID) -> _Entry:
        key = str(kb_id)
        entry = self._entries.get(key)
        if entry is None:
            loaded = Bm25Index.load(key)
            entry = _Entry(index=loaded or Bm25Index(key))
            self._entries[key] = entry
            log.info("bm25_loaded", kb_id=key, docs=entry.index.stats.doc_count)
        return entry

    def add_documents(self, kb_id: str | uuid.UUID, items: list[tuple[str, str]]) -> int:
        """添加文档。不动盘，标脏，由 flush 统一落盘。"""
        entry = self._entry(kb_id)
        with entry.lock:
            n = entry.index.add_many(items)
            entry.dirty = True
        return n

    def remove_documents(self, kb_id: str | uuid.UUID, chunk_ids: list[str]) -> int:
        entry = self._entry(kb_id)
        with entry.lock:
            n = sum(1 for cid in chunk_ids if entry.index.remove(cid))
            entry.dirty = True
            # 墓碑到阈值就重建（在锁内，避免并发重建）
            entry.index.rebuild_if_needed()
            return n

    def search(self, kb_id: str | uuid.UUID, query: str, top_k: int = 20) -> list[Bm25Hit]:
        entry = self._entry(kb_id)
        with entry.lock:
            if entry.index._dirty:  # noqa: SLF001
                entry.index.rebuild_if_needed()
            return entry.index.search(query, top_k)

    def stats(self, kb_id: str | uuid.UUID) -> dict[str, Any]:
        entry = self._entry(kb_id)
        with entry.lock:
            return entry.index.stats.to_dict()

    def flush(self) -> int:
        """把标脏的索引落盘。返回落盘数量。

        由调度器每 `BM25_SAVE_DEBOUNCE_SECONDS` 调一次（防抖）。
        """
        n = 0
        for entry in self._entries.values():
            with entry.lock:
                if not entry.dirty:
                    continue
                entry.index.save()
                entry.dirty = False
                n += 1
        return n

    def drop(self, kb_id: str | uuid.UUID) -> None:
        key = str(kb_id)
        self._entries.pop(key, None)
        path = settings.bm25_dir / f"{key}.pkl"
        path.unlink(missing_ok=True)


_manager: Bm25Manager | None = None


def get_manager() -> Bm25Manager:
    global _manager
    if _manager is None:
        _manager = Bm25Manager()
    return _manager
