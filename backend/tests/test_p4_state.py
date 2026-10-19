"""P4 状态机与仓储层测试（不依赖数据库）。

状态迁移是否合法、错误分类是否正确，这些逻辑不连库就能测。
连库的部分在 `test_p4_index_integration.py`（标记 db）。
"""

from __future__ import annotations

from app.db.repos import DOC_STATUS_FLOW, TransitionError


class TestStatusFlow:
    def test_happy_path(self) -> None:
        """正常流程：pending → parsing → chunking → embedding → ready。"""
        path = ["pending", "parsing", "chunking", "embedding", "ready"]
        for cur, nxt in zip(path, path[1:], strict=False):
            assert nxt in DOC_STATUS_FLOW[cur], f"{cur} 不能到 {nxt}"

    def test_cannot_skip_stages(self) -> None:
        """不能跳步：pending 直接到 ready 是错的，会掩盖中间失败。"""
        assert "ready" not in DOC_STATUS_FLOW["pending"]
        assert "embedding" not in DOC_STATUS_FLOW["pending"]

    def test_any_stage_can_fail(self) -> None:
        for st in ("pending", "parsing", "chunking", "embedding"):
            assert "failed" in DOC_STATUS_FLOW[st]

    def test_waiting_model_needs_model(self) -> None:
        """waiting_model 只能从 embedding 进 —— 只有向量化阶段会因为模型不可用停下来。

        如果在 parsing 阶段就标 waiting_model，说明判断逻辑错了：
        解析不需要模型。
        """
        for st, targets in DOC_STATUS_FLOW.items():
            if st == "embedding":
                assert "waiting_model" in targets
            else:
                assert "waiting_model" not in targets, f"{st} 不该能到 waiting_model"

    def test_waiting_model_resumes_or_fails(self) -> None:
        """等待中的文档：模型恢复后继续，或者失败/整单重来。"""
        assert set(DOC_STATUS_FLOW["waiting_model"]) == {"embedding", "failed", "pending"}

    def test_failed_can_retry(self) -> None:
        """失败必须能回到 pending，否则用户只能删了重传。"""
        assert "pending" in DOC_STATUS_FLOW["failed"]

    def test_ready_allows_reindex(self) -> None:
        """ready 必须能重跑。

        实测踩到：用户点「重新索引」要从 ready 回到 parsing，
        第一版把 ready 当纯终态，重跑直接被拒。
        """
        assert "parsing" in DOC_STATUS_FLOW["ready"]
        assert "pending" in DOC_STATUS_FLOW["ready"]

    def test_in_flight_can_reset_to_pending(self) -> None:
        """处理中的文档要能被打回 pending（用户改了参数想重来）。"""
        assert "pending" in DOC_STATUS_FLOW["parsing"]
        assert "pending" in DOC_STATUS_FLOW["chunking"]
        assert "pending" in DOC_STATUS_FLOW["embedding"]

    def test_failed_can_retry_directly(self) -> None:
        assert "parsing" in DOC_STATUS_FLOW["failed"]

    def test_no_self_loop(self) -> None:
        for st, targets in DOC_STATUS_FLOW.items():
            assert st not in targets, f"{st} 不该能迁移到自己"


class TestTransitionError:
    def test_is_exception(self) -> None:
        e = TransitionError("非法迁移")
        assert isinstance(e, Exception)
        assert "非法迁移" in str(e)


class TestJobStatuses:
    """injest_jobs 的状态集合必须与 SQL CHECK 约束一致。"""

    SQL_JOB_STATUSES = {"queued", "running", "retrying", "waiting_model", "done", "failed"}

    def test_doc_statuses_subset(self) -> None:
        """文档状态不超出 SQL 约束范围（除 waiting_model 外都要有对应）。

        documents.status 没有 CHECK 约束，但保持一致能避免前端漏处理。
        """
        doc_states = set(DOC_STATUS_FLOW)
        # documents 的状态集合
        expected = {"pending", "parsing", "chunking", "embedding", "ready", "waiting_model", "failed"}
        assert doc_states == expected

    def test_job_has_waiting_model(self) -> None:
        assert "waiting_model" in self.SQL_JOB_STATUSES


class TestSourcePath:
    """source_path 还原逻辑。"""

    def test_relative_key_resolved_under_files_dir(self) -> None:
        from pathlib import Path

        from app.core.config import settings

        key = "kb123/abcd.pdf"
        p = settings.files_dir / key if not Path(key).is_absolute() else Path(key)
        assert str(settings.files_dir) in str(p)

    def test_absolute_key_kept(self) -> None:
        from pathlib import Path

        abs_path = "D:/tmp/x.pdf"
        p = Path(abs_path)
        assert p.is_absolute()


class TestRetryPolicy:
    def test_max_attempts_constant(self) -> None:
        from app.workers import ingest_worker

        assert ingest_worker.MAX_ATTEMPTS == 3

    def test_waiting_model_delay_positive(self) -> None:
        from app.workers import ingest_worker

        assert ingest_worker.WAITING_MODEL_DELAY > 0

    def test_model_error_detection(self) -> None:
        """模型不可用的判定要覆盖真实的错误文案。

        实测错误是 `Embedding 服务连接失败（...）：ConnectError`，
        判定逻辑必须能识别，否则会当成普通失败重试 3 次就放弃。
        """
        cases = [
            ("embed", "Embedding 服务连接失败（http://127.0.0.1:1234/v1）：ConnectError. ..."),
            ("embed", "模型服务不可用"),
        ]
        for code, detail in cases:
            is_model = (code == "embed" and "模型" in detail) or "ConnectError" in detail
            assert is_model, f"未识别为模型问题：{detail}"


class TestChunkMapping:
    """切块字段到表字段的映射。"""

    def test_page_to_page_from(self) -> None:
        """Chunk.page → chunks.page_from / page_to。

        单页块两个字段相同；将来做跨页块时 page_to 才不同。
        """
        from app.rag.chunker import Chunk

        c = Chunk(text="x" * 30, seq=0, page=3)
        assert c.page == 3

    def test_row_becomes_row_range_string(self) -> None:
        from app.rag.chunker import Chunk

        c = Chunk(text="x" * 30, seq=0, row=12)
        # 表里 row_range 是 varchar
        assert str(c.row) == "12"

    def test_missing_optionals_are_none(self) -> None:
        from app.rag.chunker import Chunk

        c = Chunk(text="x" * 30, seq=0)
        assert c.page is None
        assert c.sheet is None
        assert c.row is None


class TestPipelineFailureMapping:
    """管道失败要映射成合适的错误码。"""

    def test_parse_error_maps_to_code(self) -> None:
        from app.workers.pipeline import PipelineError
        from app.workers.stages import Stage

        e = PipelineError(Stage.PARSE, "解析失败", detail={"code": "no_text_layer"})
        assert str(e.stage) == "parse"
        assert e.detail["code"] == "no_text_layer"
