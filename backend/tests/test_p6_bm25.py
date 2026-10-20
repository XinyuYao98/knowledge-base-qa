"""BM25 索引测试。

**不需要数据库、不需要模型服务** —— 纯内存 + 本地文件。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.retrieval.bm25 import (
    REBUILD_TOMBSTONE_RATIO,
    Bm25Index,
    Bm25Manager,
    tokenize,
)


class TestTokenize:
    def test_chinese_segmentation(self) -> None:
        """jieba 应把「住宿标准」切成两个真词，不是字级 bigram。"""
        tokens = tokenize("一线城市住宿标准")
        assert "住宿" in tokens
        assert "标准" in tokens
        # 字级切分会产生「宿标」这种伪词
        assert "宿标" not in tokens

    def test_stopwords_removed(self) -> None:
        tokens = tokenize("这是一个的测试")
        assert "的" not in tokens
        assert "是" not in tokens

    def test_punctuation_removed(self) -> None:
        tokens = tokenize("住宿标准，每晚不超过 480 元。")
        assert "，" not in tokens
        assert "。" not in tokens

    def test_numbers_kept(self) -> None:
        """数字必须保留 —— 「480」「2025」是关键检索信号。"""
        tokens = tokenize("每晚不超过480元")
        assert "480" in tokens

    def test_english_lowercased(self) -> None:
        tokens = tokenize("OCR Pipeline")
        assert "ocr" in tokens
        assert "pipeline" in tokens

    def test_empty(self) -> None:
        assert tokenize("") == []
        assert tokenize("，。！") == []


class TestAddAndSearch:
    def test_add_and_hit(self) -> None:
        idx = Bm25Index("kb1")
        idx.add("c1", "一线城市住宿标准每晚不超过480元")
        idx.add("c2", "二线城市住宿标准每晚不超过350元")
        idx.add("c3", "供应商每年复审一次")
        idx.rebuild(force=True)

        hits = idx.search("一线城市住宿", top_k=5)
        assert hits, "应该有命中"
        assert hits[0].chunk_id == "c1"

    def test_rank_order(self) -> None:
        idx = Bm25Index("kb1")
        idx.add("c1", "住宿标准一线城市")
        idx.add("c2", "住宿标准一线城市每晚不超过480元超标自付")
        idx.rebuild(force=True)

        hits = idx.search("住宿标准", top_k=5)
        assert len(hits) >= 2
        # 排名必须严格递增
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
        # 分数降序
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_no_match_returns_empty(self) -> None:
        idx = Bm25Index("kb1")
        idx.add("c1", "住宿标准")
        idx.rebuild(force=True)
        assert idx.search("量子计算机") == []

    def test_empty_index(self) -> None:
        idx = Bm25Index("kb1")
        assert idx.search("任何问题") == []

    def test_empty_query(self) -> None:
        idx = Bm25Index("kb1")
        idx.add("c1", "住宿标准")
        idx.rebuild(force=True)
        assert idx.search("") == []

    def test_top_k_limit(self) -> None:
        idx = Bm25Index("kb1")
        for i in range(50):
            idx.add(f"c{i}", f"住宿标准 第{i}条 城市")
        idx.rebuild(force=True)
        assert len(idx.search("住宿标准", top_k=10)) == 10


class TestTombstone:
    def test_remove_filters_results(self) -> None:
        """删除的块不应再出现在结果里。"""
        idx = Bm25Index("kb1")
        idx.add("c1", "住宿标准一线城市")
        idx.add("c2", "住宿标准二线城市")
        idx.rebuild(force=True)

        assert idx.remove("c1") is True
        hits = idx.search("住宿标准", top_k=10)
        assert all(h.chunk_id != "c1" for h in hits)

    def test_remove_unknown_returns_false(self) -> None:
        idx = Bm25Index("kb1")
        idx.add("c1", "住宿标准")
        idx.rebuild(force=True)
        assert idx.remove("not_exist") is False

    def test_no_rebuild_below_threshold(self) -> None:
        """墓碑占比低于 20% 时不该重建 —— 重建代价是 O(总词数)。"""
        idx = Bm25Index("kb1")
        for i in range(100):
            idx.add(f"c{i}", f"住宿标准 内容{i}")
        idx.rebuild(force=True)

        for i in range(10):   # 10% 墓碑
            idx.remove(f"c{i}")

        rebuilt = idx.rebuild_if_needed()
        assert rebuilt is False, "10% 墓碑不该触发重建"
        assert idx.stats.tombstone_count == 10

    def test_rebuild_at_threshold(self) -> None:
        """墓碑达到 20% 必须重建。"""
        idx = Bm25Index("kb1")
        for i in range(100):
            idx.add(f"c{i}", f"住宿标准 内容{i}")
        idx.rebuild(force=True)

        for i in range(int(100 * REBUILD_TOMBSTONE_RATIO) + 1):
            idx.remove(f"c{i}")

        rebuilt = idx.rebuild_if_needed()
        assert rebuilt is True
        assert idx.stats.tombstone_count == 0
        assert idx.stats.live_count == 100 - (int(100 * REBUILD_TOMBSTONE_RATIO) + 1)

    def test_rebuild_clears_tombstones(self) -> None:
        idx = Bm25Index("kb1")
        for i in range(10):
            idx.add(f"c{i}", f"内容{i}")
        idx.rebuild(force=True)
        for i in range(5):
            idx.remove(f"c{i}")

        idx.rebuild(force=True)
        assert idx.stats.tombstone_count == 0
        assert idx.stats.doc_count == 5


class TestSnapshot:
    def test_save_and_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        idx = Bm25Index("kb-snap")
        idx.add("c1", "一线城市住宿标准")
        idx.add("c2", "供应商复审")
        idx.rebuild(force=True)
        idx.save()

        loaded = Bm25Index.load("kb-snap")
        assert loaded is not None
        assert loaded.stats.doc_count == 2

        hits = loaded.search("住宿标准", top_k=5)
        assert hits and hits[0].chunk_id == "c1"

    def test_load_missing_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))
        assert Bm25Index.load("no-such-kb") is None

    def test_load_corrupt_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """快照损坏时返回 None，让上层走重新索引，而不是崩溃。"""
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))
        (tmp_path / "bad-kb.pkl").write_bytes(b"not a pickle")
        assert Bm25Index.load("bad-kb") is None

    def test_tombstones_persisted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """墓碑也要存 —— 否则重启后被删的块会「复活」。"""
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        idx = Bm25Index("kb-tomb")
        for i in range(10):
            idx.add(f"c{i}", f"住宿标准 内容{i}")
        idx.rebuild(force=True)
        idx.remove("c0")
        idx.remove("c1")
        idx.save()

        loaded = Bm25Index.load("kb-tomb")
        assert loaded is not None
        # 重新索引后（load 时 rebuild），被删的两条不应存在
        hits = loaded.search("住宿标准", top_k=20)
        ids = {h.chunk_id for h in hits}
        assert "c0" not in ids and "c1" not in ids


class TestPerformance:
    """实测重建耗时，为「要不要担心重建代价」提供数据。

    注意：这里只是**记录**，不做断言 —— 机器不同耗时差异大，
    断言耗时会让 CI 变得脆弱。
    """

    def test_rebuild_403_chunks(self) -> None:
        """403 块（P4 实测的 PDF 规模）的重建耗时。"""
        idx = Bm25Index("kb-perf")
        for i in range(403):
            idx.add(f"c{i}", f"第{i}段内容 一线城市住宿标准每晚不超过480元 超标自付")

        t0 = time.perf_counter()
        idx.rebuild(force=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        print(f"\n[实测] 403 块重建: {elapsed_ms:.1f}ms")

    def test_add_is_incremental(self) -> None:
        """新增不该触发重建 —— 这是「墓碑方案」的价值前提。"""
        idx = Bm25Index("kb-perf2")
        for i in range(403):
            idx.add(f"c{i}", f"内容{i}")
        idx.rebuild(force=True)
        rebuild_count_before = idx.stats.rebuild_count

        idx.add("new1", "新增内容")
        assert idx.stats.rebuild_count == rebuild_count_before, "新增不该重建"


class TestManager:
    def test_lazy_load(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        m = Bm25Manager()
        m.add_documents("kb-x", [("c1", "住宿标准")])
        hits = m.search("kb-x", "住宿标准")
        assert hits and hits[0].chunk_id == "c1"

    def test_stats(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        m = Bm25Manager()
        m.add_documents("kb-s", [("c1", "内容一"), ("c2", "内容二")])
        st = m.stats("kb-s")
        assert st["live_count"] == 2

    def test_flush_writes_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        m = Bm25Manager()
        m.add_documents("kb-f", [("c1", "住宿标准")])
        n = m.flush()
        assert n == 1
        assert (tmp_path / "kb-f.pkl").exists()

    def test_drop_removes_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(type(settings), "bm25_dir", property(lambda self: tmp_path))

        m = Bm25Manager()
        m.add_documents("kb-d", [("c1", "内容")])
        m.flush()
        assert (tmp_path / "kb-d.pkl").exists()

        m.drop("kb-d")
        assert not (tmp_path / "kb-d.pkl").exists()
