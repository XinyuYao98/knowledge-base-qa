"""P3 管道测试：阶段契约、中间产物落盘、指纹复用、单阶段重跑。

**全部不需要模型服务**（注入 FakeEmbedder），也不需要数据库。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.workers.pipeline import Pipeline, PipelineError
from app.workers.stages import (
    Stage,
    StagingDir,
    file_fingerprint,
    fingerprint,
    stage_dir,
)
from tests._fake_embedder import FakeEmbedder

#: 用真实素材：真实文档比合成文本更能暴露问题
FIXTURE = Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "synthetic" / "txt_gbk_chinese.txt"


@pytest.fixture
def doc_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def pipe() -> Pipeline:
    return Pipeline(embedder=FakeEmbedder(dim=settings.EMBED_DIM))


@pytest.fixture(autouse=True)
def _cleanup(doc_id: str):  # type: ignore[no-untyped-def]
    """测试后清理 staging 目录。"""
    yield
    d = stage_dir(doc_id)
    if d.path.exists():
        shutil.rmtree(d.path, ignore_errors=True)


class TestFullRun:
    async def test_creates_all_artifacts(self, pipe: Pipeline, doc_id: str) -> None:
        out = await pipe.run(doc_id, FIXTURE)
        sd = stage_dir(doc_id)

        assert sd.parsed.exists(), "缺少 parsed.json"
        assert sd.chunks.exists(), "缺少 chunks.json"
        assert sd.vectors.exists(), "缺少 vectors.bin"
        assert sd.meta.exists(), "缺少 meta.json"

        assert out.stages_run == ["parse", "chunk", "embed"]
        assert out.parsed is not None
        assert len(out.chunks) > 0
        assert len(out.vectors) == len(out.chunks)

    async def test_meta_records_stage_detail(self, pipe: Pipeline, doc_id: str) -> None:
        await pipe.run(doc_id, FIXTURE)
        meta = stage_dir(doc_id).read_meta()

        assert "parse" in meta["stages"]
        assert "chunk" in meta["stages"]
        assert "embed" in meta["stages"]

        p = meta["stages"]["parse"]
        assert p["duration_ms"] >= 0
        assert p["detail"]["format"] == "txt"
        assert p["detail"]["char_count"] > 0
        assert len(p["fingerprint"]) == 16

    async def test_vectors_roundtrip(self, pipe: Pipeline, doc_id: str) -> None:
        """二进制读写必须无损。"""
        out = await pipe.run(doc_id, FIXTURE)
        reread = stage_dir(doc_id).read_vectors(settings.EMBED_DIM)

        assert len(reread) == len(out.vectors)
        # float32 有精度损失，允许 1e-6 误差
        for a, b in zip(out.vectors[0][:10], reread[0][:10], strict=False):
            assert abs(a - b) < 1e-6


class TestFingerprintReuse:
    async def test_second_run_reuses_all(self, pipe: Pipeline, doc_id: str) -> None:
        """第二次跑，参数没变 → 全部复用，不重新解析。"""
        first = await pipe.run(doc_id, FIXTURE)
        assert first.stages_run == ["parse", "chunk", "embed"]

        second = await pipe.run(doc_id, FIXTURE)
        assert second.stages_run == [], f"不该重跑，实际跑了 {second.stages_run}"
        assert set(second.stages_reused) == {"parse", "chunk", "embed"}

    async def test_changed_source_invalidates(self, pipe: Pipeline, doc_id: str, tmp_path: Path) -> None:
        """源文件内容变了 → 从 parse 起全部失效。"""
        await pipe.run(doc_id, FIXTURE)

        modified = tmp_path / "modified.txt"
        modified.write_text(FIXTURE.read_text(encoding="utf-8", errors="ignore") + "\n追加一行内容。", encoding="utf-8")

        out = await pipe.run(doc_id, modified)
        assert "parse" in out.stages_run

    async def test_chunk_param_change_only_reruns_downstream(
        self, pipe: Pipeline, doc_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """改切块参数 → parse 复用，chunk 与 embed 重跑。

        这是 P3 的核心价值：调切块参数不该重解析 PDF。
        """
        await pipe.run(doc_id, FIXTURE)

        monkeypatch.setattr(settings, "MIN_CHUNK_CHARS", 999)
        out = await pipe.run(doc_id, FIXTURE)

        assert "parse" in out.stages_reused, "parse 不该重跑"
        assert "chunk" in out.stages_run, "chunk 应该重跑"
        assert "embed" in out.stages_run, "下游 embed 应该跟着重跑"


class TestResumeFromStage:
    async def test_resume_from_chunk(self, pipe: Pipeline, doc_id: str) -> None:
        await pipe.run(doc_id, FIXTURE)

        # 用 -1000 的起点模拟单阶段重跑
        out = await pipe.run_from(doc_id, FIXTURE, Stage.CHUNK, reuse=False)
        assert "parse" in out.stages_reused
        assert "chunk" in out.stages_run

    async def test_resume_from_chunk_without_parsed_fails(self, pipe: Pipeline, doc_id: str) -> None:
        """没有上游产物时，从 chunk 开始必须明确报错，而不是静默产出空结果。"""
        with pytest.raises(PipelineError) as exc:
            await pipe.run_from(doc_id, FIXTURE, Stage.CHUNK)
        assert exc.value.stage == Stage.CHUNK
        assert "缺少上游产物" in exc.value.message

    async def test_clear_from_removes_downstream(self, pipe: Pipeline, doc_id: str) -> None:
        await pipe.run(doc_id, FIXTURE)
        sd = stage_dir(doc_id)

        removed = sd.clear_from(Stage.CHUNK)
        assert "chunks.json" in removed
        assert "vectors.bin" in removed
        assert sd.parsed.exists(), "parse 产物不该被删"
        assert not sd.chunks.exists()
        assert not sd.vectors.exists()


class TestParseFailure:
    async def test_scanned_pdf_raises_with_stage(self, pipe: Pipeline, doc_id: str) -> None:
        """扫描件在 parse 阶段失败，错误必须带阶段信息。"""
        scanned = (
            Path(__file__).resolve().parents[2]
            / "eval" / "fixtures" / "synthetic" / "pdf_scanned_no_text.pdf"
        )
        with pytest.raises(PipelineError) as exc:
            await pipe.run(doc_id, scanned)
        assert exc.value.stage == Stage.PARSE
        assert exc.value.detail.get("code") == "no_text_layer"

    async def test_no_partial_artifacts_on_failure(self, pipe: Pipeline, doc_id: str) -> None:
        """解析失败不能留下半成品 chunks。"""
        scanned = (
            Path(__file__).resolve().parents[2]
            / "eval" / "fixtures" / "synthetic" / "pdf_scanned_no_text.pdf"
        )
        with pytest.raises(PipelineError):
            await pipe.run(doc_id, scanned)

        sd = stage_dir(doc_id)
        assert not sd.chunks.exists(), "失败后不该有 chunks.json"
        assert not sd.vectors.exists(), "失败后不该有 vectors.bin"


class TestStageHelpers:
    def test_stage_order(self) -> None:
        assert Stage.PARSE.next == Stage.CHUNK
        assert Stage.CHUNK.next == Stage.EMBED
        assert Stage.EMBED.next is None

    def test_stage_index(self) -> None:
        assert Stage.PARSE.index < Stage.CHUNK.index < Stage.EMBED.index

    def test_fingerprint_stable(self) -> None:
        """同样输入必须得到同样指纹，否则复用永远失效。"""
        a = fingerprint({"x": 1, "y": [1, 2]})
        b = fingerprint({"y": [1, 2], "x": 1})   # 键顺序不同
        assert a == b

    def test_fingerprint_changes(self) -> None:
        assert fingerprint({"x": 1}) != fingerprint({"x": 2})

    def test_file_fingerprint(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("内容", encoding="utf-8")
        h1 = file_fingerprint(f)
        f.write_text("内容变了", encoding="utf-8")
        assert file_fingerprint(f) != h1


class TestStagingDir:
    def test_atomic_write(self, tmp_path: Path) -> None:
        sd = StagingDir(root=tmp_path, doc_id="d1")
        data = {"a": 1}
        sd.write_json(sd.meta, data)
        assert sd.read_json(sd.meta) == data
        # 不该留下 .tmp 文件
        assert not list(tmp_path.rglob("*.tmp"))

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        sd = StagingDir(root=tmp_path, doc_id="d1")
        assert sd.read_json(sd.parsed) is None

    def test_read_corrupt_returns_none(self, tmp_path: Path) -> None:
        """文件损坏时返回 None 而不是抛异常，让上层走重跑逻辑。"""
        sd = StagingDir(root=tmp_path, doc_id="d1")
        sd.ensure()
        sd.parsed.write_text("{ 坏掉的 json", encoding="utf-8")
        assert sd.read_json(sd.parsed) is None

    def test_vector_dim_mismatch_raises(self, tmp_path: Path) -> None:
        """维度不符必须立刻报错，否则写进 Chroma 才失败。"""
        sd = StagingDir(root=tmp_path, doc_id="d1")
        with pytest.raises(ValueError, match="维度"):
            sd.write_vectors([[1.0, 2.0, 3.0]], dim=2560)


class TestChunkSerialization:
    async def test_chunks_json_readable(self, pipe: Pipeline, doc_id: str) -> None:
        await pipe.run(doc_id, FIXTURE)
        data = json.loads(stage_dir(doc_id).chunks.read_text(encoding="utf-8"))

        assert "chunks" in data
        assert len(data["chunks"]) > 0
        first = data["chunks"][0]
        assert "text" in first
        assert "seq" in first
        assert "token_estimate" in first

    async def test_reload_chunks_roundtrip(self, pipe: Pipeline, doc_id: str) -> None:
        """从 JSON 载回的块必须与原对象等价。"""
        out = await pipe.run(doc_id, FIXTURE)
        reloaded = Pipeline._load_chunks(stage_dir(doc_id))

        assert len(reloaded) == len(out.chunks)
        for a, b in zip(out.chunks, reloaded, strict=False):
            assert a.text == b.text
            assert a.seq == b.seq
            assert a.chunk_type == b.chunk_type
