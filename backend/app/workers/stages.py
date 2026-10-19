"""管道阶段契约。

## 为什么要有显式契约

RAG 索引流水线本质是 ETL。大多数实现把它塞进 worker 的黑盒里，
结果是：

- 调切块参数要重跑整个文件（含 PDF 重解析）
- 出了质量问题，不知道是解析丢内容还是切块切坏
- 每个阶段无法单独测试

本项目把三个阶段的数据结构固定下来，**输入输出强类型，且都能序列化落盘**：

    parse  : file      → ParsedDoc
    chunk  : ParsedDoc → ChunkSet
    embed  : ChunkSet  → EmbeddingBatch

## 落盘格式

`data/staging/{doc_id}/`

    parsed.json        ParsedDoc 的完整序列化
    chunks.json        ChunkSet
    vectors.bin        float32 二进制（维度写在 meta.json 里）
    meta.json          每阶段耗时、参数快照、产物指纹

**vectors 用二进制不用 JSON**：2560 维 × 500 块用 JSON 约 60MB，
二进制只要 5MB，且读写快一个量级。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 阶段定义
# ---------------------------------------------------------------------------


class Stage(StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"

    @property
    def next(self) -> Stage | None:
        order = [Stage.PARSE, Stage.CHUNK, Stage.EMBED]
        i = order.index(self)
        return order[i + 1] if i + 1 < len(order) else None

    @property
    def index(self) -> int:
        return [Stage.PARSE, Stage.CHUNK, Stage.EMBED].index(self)


@dataclass(slots=True)
class StageResult:
    """单个阶段的执行结果。"""

    stage: Stage
    duration_ms: float
    output_count: int          # 产出数量（块数 / 向量数）
    fingerprint: str = ""      # 产物指纹，用于判断能否复用
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "duration_ms": round(self.duration_ms, 1),
            "output_count": self.output_count,
            "fingerprint": self.fingerprint,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# 指纹：判断中间产物能否复用
# ---------------------------------------------------------------------------


def fingerprint(obj: Any) -> str:
    """对任意可 JSON 序列化的对象算稳定指纹。

    **为什么需要**：调了切块参数后，若 parsed.json 的来源文件与解析参数
    都没变，就不该重解析 PDF（可能几十秒）。

    做法：把影响该阶段输出的所有因素（上游产物指纹 + 本阶段参数）一起哈希。
    """
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def file_fingerprint(path: Path) -> str:
    """文件内容指纹（sha256 前 16 位）。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 中间产物目录
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StagingDir:
    """`data/staging/{doc_id}/` 的访问封装。"""

    root: Path
    doc_id: str

    @property
    def path(self) -> Path:
        return self.root / self.doc_id

    def ensure(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return self.path

    # ---- 各阶段产物路径 ----
    @property
    def parsed(self) -> Path:
        return self.path / "parsed.json"

    @property
    def chunks(self) -> Path:
        return self.path / "chunks.json"

    @property
    def vectors(self) -> Path:
        return self.path / "vectors.bin"

    @property
    def meta(self) -> Path:
        return self.path / "meta.json"

    # ---- 读写 ----
    def write_json(self, path: Path, data: Any) -> None:
        self.ensure()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 原子替换：避免写到一半被读到

    def read_json(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def write_vectors(self, vectors: list[list[float]], dim: int) -> None:
        """写 float32 二进制。"""
        import array

        self.ensure()
        flat = array.array("f")
        for v in vectors:
            if len(v) != dim:
                raise ValueError(f"向量维度 {len(v)} 与声明 {dim} 不一致")
            flat.extend(v)
        tmp = self.vectors.with_suffix(".bin.tmp")
        with tmp.open("wb") as f:
            flat.tofile(f)
        tmp.replace(self.vectors)

    def read_vectors(self, dim: int) -> list[list[float]]:
        import array

        if not self.vectors.exists():
            return []
        arr = array.array("f")
        with self.vectors.open("rb") as f:
            arr.fromfile(f, self.vectors.stat().st_size // 4)
        count = len(arr) // dim
        return [list(arr[i * dim : (i + 1) * dim]) for i in range(count)]

    def exists(self, stage: Stage) -> bool:
        return {
            Stage.PARSE: self.parsed,
            Stage.CHUNK: self.chunks,
            Stage.EMBED: self.vectors,
        }[stage].exists()

    def clear_from(self, stage: Stage) -> list[str]:
        """删除该阶段及其下游的产物。返回被删的文件名。"""
        removed: list[str] = []
        for s in [Stage.PARSE, Stage.CHUNK, Stage.EMBED]:
            if s.index < stage.index:
                continue
            p = {
                Stage.PARSE: self.parsed,
                Stage.CHUNK: self.chunks,
                Stage.EMBED: self.vectors,
            }[s]
            if p.exists():
                p.unlink()
                removed.append(p.name)
        # meta 里的阶段记录也要清
        meta = self.read_json(self.meta) or {}
        stages = meta.get("stages", {})
        for s in [Stage.PARSE, Stage.CHUNK, Stage.EMBED]:
            if s.index >= stage.index:
                stages.pop(str(s), None)
        if meta:
            meta["stages"] = stages
            self.write_json(self.meta, meta)
        return removed

    # ---- meta ----
    def read_meta(self) -> dict[str, Any]:
        return self.read_json(self.meta) or {}

    def record_stage(self, result: StageResult, *, filename: str = "", ext: str = "") -> None:
        meta = self.read_meta()
        meta.setdefault("doc_id", self.doc_id)
        # filename / ext 只在第一次记录时写入，后续阶段继承
        if filename:
            meta.setdefault("filename", filename)
        if ext:
            meta.setdefault("ext", ext)
        meta.setdefault("stages", {})[str(result.stage)] = result.to_dict()
        self.write_json(self.meta, meta)


# ---------------------------------------------------------------------------
# 分阶段重跑的入口辅助
# ---------------------------------------------------------------------------


def stage_dir(doc_id: str) -> StagingDir:
    from app.core.config import settings

    return StagingDir(root=settings.staging_dir, doc_id=doc_id)
