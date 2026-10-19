"""质量报告与健康分测试。

健康分是「解析质量可观测」的量化出口，公式错了会让看板给错告警。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import quality
from app.workers import cleanup


class TestHealthScore:
    def _meta(
        self,
        *,
        input_blocks: int = 100,
        dropped: int = 0,
        warnings: list | None = None,
        pages: int = 10,
        char_count: int = 5000,
    ) -> dict:
        return {
            "stages": {
                "parse": {
                    "pages": pages,
                    "char_count": char_count,
                    "block_count": input_blocks,
                    "warnings": warnings or [],
                },
                "chunk": {
                    "input_blocks": input_blocks,
                    "dropped_count": dropped,
                    "chunk_count": input_blocks - dropped,
                },
            }
        }

    def test_perfect_document(self) -> None:
        h = quality.compute_health(self._meta())
        assert h["score"] == 1.0
        assert h["level"] == "ok"
        assert h["issues"] == []

    def test_dropped_blocks_penalty(self) -> None:
        """丢块是最严重的惩罚项（权重 0.4）。丢掉 = 内容检索不到。"""
        h = quality.compute_health(self._meta(dropped=50))
        assert h["score"] == pytest.approx(0.8, abs=0.01)
        assert any("丢弃" in i for i in h["issues"])

    def test_warning_penalty(self) -> None:
        h = quality.compute_health(self._meta(warnings=[{"code": "merged_cell", "message": "合并单元格"}]))
        assert h["score"] == pytest.approx(0.7, abs=0.01)

    def test_low_density_penalty(self) -> None:
        """每页字符 < 200 → 疑似扫描件，罚 0.3。"""
        h = quality.compute_health(self._meta(pages=10, char_count=1000))
        assert h["score"] == pytest.approx(0.7, abs=0.01)
        assert any("疑似扫描" in i for i in h["issues"])

    def test_all_penalties_combined(self) -> None:
        """三项全中：0.4 + 0.3 + 0.3 = 1.0 → 分数 0。"""
        h = quality.compute_health(
            self._meta(dropped=100, warnings=[{"message": "x"}], pages=10, char_count=500)
        )
        assert h["score"] == 0.0
        assert h["level"] == "warn"

    def test_score_never_negative(self) -> None:
        h = quality.compute_health(
            self._meta(dropped=200, warnings=[{"message": "x"}], pages=10, char_count=100)
        )
        assert h["score"] >= 0.0

    def test_threshold_boundary(self) -> None:
        """0.7 是阈值边界：等于 0.7 不算 warn。"""
        h = quality.compute_health(self._meta(warnings=[{"message": "x"}]))
        assert h["score"] == pytest.approx(0.7, abs=0.005)
        assert h["level"] == "ok"   # >= 0.7 算健康

    def test_no_chunk_stage(self) -> None:
        """只跑了 parse 的文档，健康分仍能算（chunk 视为 0 丢块）。"""
        meta = {"stages": {"parse": {"pages": 5, "char_count": 2000, "block_count": 20, "warnings": []}}}
        h = quality.compute_health(meta)
        assert h["score"] == 1.0

    def test_empty_meta(self) -> None:
        h = quality.compute_health({})
        assert 0.0 <= h["score"] <= 1.0


class TestHealthRealStructure:
    """用**真实**的 meta.json 结构测。

    实测踩过：第一版断言用平铺的 mock（stages.parse.char_count），
    但真实结构是 stages.parse.detail.char_count，导致 chars_per_page 恒为 0，
    低密度文档永远不被标记 —— 静默失效，测试却没抓到。
    """

    def _real_meta(self, **parse_detail_overrides) -> dict:
        parse_detail = {
            "format": "pdf",
            "pages": 12,
            "char_count": 20706,
            "table_count": 13,
            "heading_count": 0,
            "warning_count": 0,
            "warnings": [],
            "page_char_counts": [800] * 12,
        }
        parse_detail.update(parse_detail_overrides)
        return {
            "doc_id": "x",
            "filename": "a.pdf",
            "stages": {
                "parse": {
                    "stage": "parse",
                    "duration_ms": 778.9,
                    "output_count": 32,
                    "fingerprint": "abc",
                    "detail": parse_detail,
                },
                "chunk": {
                    "stage": "chunk",
                    "duration_ms": 0.4,
                    "output_count": 403,
                    "fingerprint": "def",
                    "detail": {
                        "input_blocks": 32,
                        "chunk_count": 403,
                        "table_row_chunks": 379,
                        "dropped_count": 0,
                        "avg_tokens": 54.2,
                    },
                },
            },
        }

    def test_reads_nested_detail(self) -> None:
        """真实结构下，chars_per_page 必须正确算出，不能是 0。"""
        h = quality.compute_health(self._real_meta())
        assert h["factors"]["chars_per_page"] == pytest.approx(1725.5, abs=1)
        assert h["factors"]["low_density"] is False

    def test_detects_scanned_document(self) -> None:
        """低密度文档必须被标出来 —— 这是健康分存在的主要意义。"""
        h = quality.compute_health(self._real_meta(char_count=1200))
        assert h["factors"]["chars_per_page"] == pytest.approx(100, abs=1)
        assert h["factors"]["low_density"] is True
        assert h["score"] == pytest.approx(0.7, abs=0.01)
        assert any("疑似扫描" in i for i in h["issues"])

    def test_dropped_from_nested_detail(self) -> None:
        meta = self._real_meta()
        meta["stages"]["chunk"]["detail"]["dropped_count"] = 8
        h = quality.compute_health(meta)
        assert h["factors"]["dropped_ratio"] == pytest.approx(0.25, abs=0.01)

    def test_warnings_from_nested_detail(self) -> None:
        meta = self._real_meta(
            warnings=[{"code": "merged_cell", "message": "合并单元格已展开"}],
            warning_count=1,
        )
        h = quality.compute_health(meta)
        assert h["factors"]["has_warning"] is True
        assert any("合并单元格" in i for i in h["issues"])


class TestAggregate:
    def test_empty(self) -> None:
        s = quality.aggregate([])
        assert s["doc_count"] == 0
        assert s["avg_health"] == 0.0

    def test_summary_fields(self) -> None:
        # 真实结构：chunk_count 在 detail 下
        reports = [
            {"health": {"score": 1.0, "level": "ok", "issues": []},
             "stages": {"chunk": {"output_count": 10, "detail": {"chunk_count": 10}}}},
            {"health": {"score": 0.5, "level": "warn", "issues": ["3 个块被丢弃"]},
             "stages": {"chunk": {"output_count": 5, "detail": {"chunk_count": 5}}}},
        ]
        s = quality.aggregate(reports)
        assert s["doc_count"] == 2
        assert s["healthy_count"] == 1
        assert s["avg_health"] == 0.75
        assert s["total_chunks"] == 15

    def test_issue_bucketing(self) -> None:
        """问题要归类聚合，否则 100 份文档会列出 100 条不同文本。"""
        reports = [
            {"health": {"score": 0.5, "level": "warn", "issues": ["5 个块被丢弃"]}, "stages": {}},
            {"health": {"score": 0.5, "level": "warn", "issues": ["12 个块被丢弃"]}, "stages": {}},
            {"health": {"score": 0.5, "level": "warn", "issues": ["疑似扫描件"]}, "stages": {}},
        ]
        s = quality.aggregate(reports)
        issues = {i["issue"]: i["doc_count"] for i in s["issues"]}
        assert issues["有块被丢弃"] == 2
        assert issues["疑似扫描件或解析失败"] == 1


class TestCleanupStaging:
    def test_old_dir_deleted(self, tmp_path: Path) -> None:
        d = tmp_path / "old-doc"
        d.mkdir()
        (d / "parsed.json").write_text("{}", encoding="utf-8")
        # 改成 10 天前
        old = time.time() - 10 * 86400
        import os

        os.utime(d, (old, old))

        r = cleanup.cleanup_staging(retention_days=7, dry_run=False, root=tmp_path)
        assert r.deleted == 1
        assert not d.exists()

    def test_recent_dir_kept(self, tmp_path: Path) -> None:
        d = tmp_path / "new-doc"
        d.mkdir()
        r = cleanup.cleanup_staging(retention_days=7, dry_run=False, root=tmp_path)
        assert r.deleted == 0
        assert d.exists()

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        d = tmp_path / "old-doc"
        d.mkdir()
        # 必须有内容，否则 freed_bytes 是 0，「报告要删多少空间」无从验证
        (d / "vectors.bin").write_bytes(b"x" * 512)
        old = time.time() - 10 * 86400
        import os

        os.utime(d, (old, old))

        r = cleanup.cleanup_staging(retention_days=7, dry_run=True, root=tmp_path)
        assert r.deleted == 1          # 报告要删
        assert r.freed_bytes == 512
        assert d.exists(), "dry-run 不该真删"

    def test_min_age_protection(self, tmp_path: Path) -> None:
        """即使 retention_days=0，也不能删最近 1 小时内的东西。"""
        d = tmp_path / "just-created"
        d.mkdir()
        r = cleanup.cleanup_staging(retention_days=0, dry_run=False, root=tmp_path)
        assert r.deleted == 0
        assert d.exists()

    def test_nonexistent_root(self, tmp_path: Path) -> None:
        r = cleanup.cleanup_staging(root=tmp_path / "nope")
        assert r.scanned == 0

    def test_reports_freed_bytes(self, tmp_path: Path) -> None:
        d = tmp_path / "big"
        d.mkdir()
        (d / "vectors.bin").write_bytes(b"x" * 1024)
        old = time.time() - 10 * 86400
        import os

        os.utime(d, (old, old))

        r = cleanup.cleanup_staging(retention_days=7, dry_run=True, root=tmp_path)
        assert r.freed_bytes == 1024


class TestCleanupUploads:
    def test_old_file_deleted(self, tmp_path: Path) -> None:
        f = tmp_path / "part-001"
        f.write_bytes(b"data")
        old = time.time() - 48 * 3600
        import os

        os.utime(f, (old, old))

        r = cleanup.cleanup_uploads(retention_hours=24, dry_run=False, root=tmp_path)
        assert r.deleted == 1
        assert not f.exists()

    def test_recent_kept(self, tmp_path: Path) -> None:
        f = tmp_path / "part-002"
        f.write_bytes(b"data")
        r = cleanup.cleanup_uploads(retention_hours=24, dry_run=False, root=tmp_path)
        assert r.deleted == 0
        assert f.exists()


class TestRunAll:
    def test_combines_both(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
        res = cleanup.run_all(dry_run=True)
        assert "staging" in res
        assert "uploads" in res
        assert "total_freed_mb" in res
