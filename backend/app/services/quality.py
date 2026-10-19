"""质量报告：判断「是解析丢内容还是切块切坏了」。

## 健康分算法

架构文档 §6.0 定义：

    penalty = 0.4 × 丢块占比
            + 0.3 × (有解析告警 ? 1 : 0)
            + 0.3 × (每页字符 < 200 ? 1 : 0)

    health.score = 1 - penalty

低于 0.7 标红。

## 为什么是这三个因子

| 因子 | 权重 | 理由 |
|---|---|---|
| 丢块占比 | 0.4 | **最严重**：块被丢掉意味着内容检索不到，用户永远问不出这段 |
| 解析告警 | 0.3 | 中等：告警说明有降级（扫描页、合并单元格），内容可能不完整 |
| 每页字符过少 | 0.3 | 中等：平均每页 < 200 字，通常是扫描件或解析失败 |

**没有把「切块平均长度」放进惩罚项**：短块不一定是坏事
（目录页、表格行本来就短），放进公式会误伤。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.workers.stages import StagingDir, stage_dir

#: 健康分阈值，低于此值在前端标红
HEALTH_WARN_THRESHOLD = 0.7

#: 每页字符低于此值视为「疑似扫描件 / 解析失败」
LOW_CHARS_PER_PAGE = 200


def compute_health(meta: dict[str, Any]) -> dict[str, Any]:
    """按 §6.0 的公式计算健康分。

    ## 字段路径（实测踩过）

    `meta.json` 的结构是：

        stages.parse.detail.char_count    ← 不是 stages.parse.char_count

    因为 `StageResult.detail` 把业务指标都收在 `detail` 下。
    第一版直接读 `parse.get("char_count")` 拿到 None，
    导致 `chars_per_page` 恒为 0，低密度文档永远不会被标记 —— 静默失效。
    """
    stages = meta.get("stages", {})
    parse = stages.get("parse", {})
    chunk = stages.get("chunk", {})

    # 兼容两种结构：detail 内（真实）与平铺（早期/手工构造）
    p_detail = parse.get("detail") or {}
    c_detail = chunk.get("detail") or {}

    def pick(src_detail: dict, src_flat: dict, key: str, default: Any = 0) -> Any:
        v = src_detail.get(key)
        if v is None:
            v = src_flat.get(key)
        return default if v is None else v

    input_blocks = pick(c_detail, chunk, "input_blocks") or pick(p_detail, parse, "block_count")
    dropped = pick(c_detail, chunk, "dropped_count")
    dropped_ratio = (dropped / input_blocks) if input_blocks else 0.0

    warnings = pick(p_detail, parse, "warnings", []) or []
    has_warning = bool(warnings)

    pages = pick(p_detail, parse, "pages")
    char_count = pick(p_detail, parse, "char_count")
    chars_per_page = (char_count / pages) if pages else char_count
    low_density = pages > 0 and chars_per_page < LOW_CHARS_PER_PAGE

    penalty = (
        0.4 * min(dropped_ratio, 1.0)
        + 0.3 * (1.0 if has_warning else 0.0)
        + 0.3 * (1.0 if low_density else 0.0)
    )
    score = round(max(0.0, 1.0 - penalty), 3)

    issues: list[str] = []
    if dropped:
        issues.append(f"{dropped} 个块被丢弃")
    if has_warning:
        for w in warnings[:3]:
            msg = w.get("message", "") if isinstance(w, dict) else str(w)
            if msg:
                issues.append(msg)
    if low_density:
        issues.append(f"平均每页仅 {chars_per_page:.0f} 字，疑似扫描件或解析失败")

    return {
        "score": score,
        "level": "warn" if score < HEALTH_WARN_THRESHOLD else "ok",
        "issues": issues,
        "factors": {
            "dropped_ratio": round(dropped_ratio, 3),
            "has_warning": has_warning,
            "chars_per_page": round(chars_per_page, 1),
            "low_density": low_density,
        },
    }


def build_report(doc_id: str, filename: str = "", ext: str = "") -> dict[str, Any]:
    """从 staging/meta.json 生成完整质量报告。"""
    sd = stage_dir(doc_id)
    meta = sd.read_meta()
    health = compute_health(meta)

    return {
        "document_id": doc_id,
        "filename": filename or meta.get("filename", ""),
        "ext": ext,
        "stages": meta.get("stages", {}),
        "health": health,
    }


def write_report(doc_id: str, filename: str = "", ext: str = "") -> Path:
    """生成并落盘 quality_report.json。"""
    sd = stage_dir(doc_id)
    report = build_report(doc_id, filename, ext)
    path = sd.path / "quality_report.json"
    sd.write_json(path, report)
    return path


def load_reports(doc_ids: list[str]) -> list[dict[str, Any]]:
    """批量读取，缺失的跳过。"""
    out: list[dict[str, Any]] = []
    for did in doc_ids:
        sd = stage_dir(did)
        path = sd.path / "quality_report.json"
        if path.exists():
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
                continue
            except (json.JSONDecodeError, OSError):
                pass
        # 没有报告就现算
        meta = sd.read_meta()
        if meta:
            out.append(build_report(did))
    return out


def aggregate(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总成看板顶部概览。"""
    if not reports:
        return {
            "doc_count": 0,
            "healthy_count": 0,
            "avg_health": 0.0,
            "total_chunks": 0,
            "issues": [],
        }

    scores = [r["health"]["score"] for r in reports]
    chunks = sum(
        (r.get("stages", {}).get("chunk", {}).get("detail") or {}).get("chunk_count", 0)
        or r.get("stages", {}).get("chunk", {}).get("output_count", 0)
        for r in reports
    )

    # 聚合问题
    buckets: dict[str, int] = {}
    for r in reports:
        for issue in r["health"].get("issues", []):
            key = _bucket(issue)
            buckets[key] = buckets.get(key, 0) + 1

    return {
        "doc_count": len(reports),
        "healthy_count": sum(1 for s in scores if s >= HEALTH_WARN_THRESHOLD),
        "avg_health": round(sum(scores) / len(scores), 3),
        "total_chunks": chunks,
        "issues": [{"issue": k, "doc_count": v} for k, v in sorted(buckets.items(), key=lambda x: -x[1])],
    }


def _bucket(issue: str) -> str:
    """把具体问题归成可聚合的类别。"""
    if "丢弃" in issue:
        return "有块被丢弃"
    if "扫描" in issue or "每页仅" in issue:
        return "疑似扫描件或解析失败"
    if "合并单元格" in issue:
        return "存在合并单元格（已展开）"
    if "编码" in issue:
        return "编码非 UTF-8（已按猜测解析）"
    if "图片" in issue:
        return "含图片（未提取图片内文字）"
    return "其他解析降级"


def export_json(reports: list[dict[str, Any]], path: Path) -> None:
    """把聚合结果写成文件，供前端或 CI 读取。"""
    payload = {"summary": aggregate(reports), "documents": reports}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "HEALTH_WARN_THRESHOLD",
    "StagingDir",
    "aggregate",
    "build_report",
    "compute_health",
    "export_json",
    "load_reports",
    "write_report",
]
