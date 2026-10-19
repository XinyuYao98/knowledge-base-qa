#!/usr/bin/env python3
"""单阶段重跑（P3 核心能力）。

## 为什么需要它

调切块参数时，如果每次都重跑整个管道，128 页 PDF 要等 3.4 秒解析。
只重切块只需 0.8 秒 —— 而且**改一次参数可能要试十几次**。

    make reprocess DOC=<id> STAGE=chunk

## 用法

    python scripts/pipeline_cli.py run <文件路径>            # 全流程
    python scripts/pipeline_cli.py run <文件路径> --doc-id x  # 指定 doc_id
    python scripts/pipeline_cli.py reprocess <doc_id> --stage chunk
    python scripts/pipeline_cli.py status <doc_id>
    python scripts/pipeline_cli.py report <doc_id>
    python scripts/pipeline_cli.py clean --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.core.platform import selector_loop_factory  # noqa: E402
from app.services import quality  # noqa: E402
from app.workers import cleanup  # noqa: E402
from app.workers.pipeline import Pipeline, PipelineError  # noqa: E402
from app.workers.stages import Stage, stage_dir  # noqa: E402


def _fmt_ms(ms: float) -> str:
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms/1000:.2f}s"


def cmd_run(args: argparse.Namespace) -> int:
    source = Path(args.file)
    if not source.exists():
        print(f"文件不存在：{source}", file=sys.stderr)
        return 2

    doc_id = args.doc_id or str(uuid.uuid4())
    print(f"doc_id: {doc_id}")
    print(f"源文件: {source.name} ({source.stat().st_size} 字节)")
    print()

    async def go() -> int:
        pipe = Pipeline()
        try:
            out = await pipe.run(doc_id, source)
        except PipelineError as e:
            print(f"✗ [{e.stage}] {e.message}")
            if e.detail:
                print(f"  {json.dumps(e.detail, ensure_ascii=False)[:300]}")
            return 1

        print_stages(out)
        report_path = quality.write_report(doc_id, source.name, source.suffix.lstrip("."))
        report = quality.build_report(doc_id, source.name, source.suffix.lstrip("."))
        print_health(report)
        print(f"\n中间产物: {stage_dir(doc_id).path}")
        print(f"质量报告: {report_path.name}")
        print(f"\n后续可用：")
        print(f"  make reprocess DOC={doc_id} STAGE=chunk")
        print(f"  python scripts/pipeline_cli.py status {doc_id}")
        return 0

    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def cmd_reprocess(args: argparse.Namespace) -> int:
    doc_id = args.doc_id
    sd = stage_dir(doc_id)
    if not sd.path.exists():
        print(f"找不到 doc_id={doc_id} 的中间产物，请先 run 一次", file=sys.stderr)
        return 2

    stage = Stage(args.stage)
    source = Path(args.source) if args.source else None

    # 从 parse 开始必须有源文件
    if stage.index <= Stage.PARSE.index and source is None:
        print(f"从 {stage} 开始需要 --source 指定源文件", file=sys.stderr)
        return 2

    print(f"doc_id: {doc_id}")
    print(f"从阶段: {stage}")
    print(f"将清除: {[s.value for s in Stage if s.index >= stage.index]}")
    print()

    async def go() -> int:
        pipe = Pipeline()
        try:
            out = await pipe.run_from(doc_id, source or Path(""), stage, reuse=False)
        except PipelineError as e:
            print(f"✗ [{e.stage}] {e.message}")
            return 1
        print_stages(out)
        quality.write_report(doc_id)
        print_health(quality.build_report(doc_id))
        return 0

    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def cmd_status(args: argparse.Namespace) -> int:
    sd = stage_dir(args.doc_id)
    if not sd.path.exists():
        print(f"找不到 doc_id={args.doc_id}")
        return 2

    meta = sd.read_meta()
    print(f"doc_id: {args.doc_id}")
    print(f"目录: {sd.path}")
    print()
    print(f"{'产物':<14} {'存在':<6} {'大小':>10}")
    print("-" * 36)
    for name, p in [
        ("parsed.json", sd.parsed),
        ("chunks.json", sd.chunks),
        ("vectors.bin", sd.vectors),
        ("meta.json", sd.meta),
        ("quality_report.json", sd.path / "quality_report.json"),
    ]:
        exists = "✓" if p.exists() else "—"
        size = f"{p.stat().st_size/1024:.1f} KB" if p.exists() else ""
        print(f"{name:<14} {exists:<6} {size:>10}")

    print()
    print("阶段记录：")
    for st in ("parse", "chunk", "embed"):
        d = meta.get("stages", {}).get(st)
        if not d:
            print(f"  {st:<6} 未运行")
            continue
        print(f"  {st:<6} {_fmt_ms(d['duration_ms']):>8}  产出 {d['output_count']}  "
              f"指纹 {d.get('fingerprint','')[:8]}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if args.doc_id:
        r = quality.build_report(args.doc_id)
        if not r.get("stages"):
            print(f"找不到 doc_id={args.doc_id} 的记录")
            return 2
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    # 全量汇总
    base = settings.staging_dir
    if not base.exists():
        print("staging 目录为空")
        return 0
    ids = [p.name for p in base.iterdir() if p.is_dir()]
    reports = quality.load_reports(ids)
    summary = quality.aggregate(reports)

    print("=" * 64)
    print("质量汇总")
    print("=" * 64)
    print(f"  文档数      {summary['doc_count']}")
    print(f"  健康文档    {summary['healthy_count']}")
    print(f"  平均健康分  {summary['avg_health']}")
    print(f"  总块数      {summary['total_chunks']}")
    if summary["issues"]:
        print("\n  问题聚合：")
        for it in summary["issues"]:
            print(f"    · {it['issue']}（{it['doc_count']} 份）")

    print(f"\n{'':2} {'文件':<32} {'健康':>6} {'块':>5} {'耗时':>9}")
    print("-" * 64)
    for r in reports:
        h = r["health"]
        mark = "✓" if h["level"] == "ok" else "⚠"
        ch = r.get("stages", {}).get("chunk", {})
        chunk_n = (ch.get("detail") or {}).get("chunk_count") or ch.get("output_count", 0)
        total_ms = sum(s.get("duration_ms", 0) for s in r.get("stages", {}).values())
        name = r.get("filename") or (r.get("stages", {}).get("parse", {}).get("detail") or {}).get("filename") or "（未命名）"
        print(f"{mark:2} {name[:32]:<32} {h['score']:>6.2f} "
              f"{chunk_n:>5} {_fmt_ms(total_ms):>9}")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    res = cleanup.run_all(dry_run=not args.apply)
    print("=" * 64)
    print(f"清理{'（dry-run）' if not args.apply else '（已应用）'}")
    print("=" * 64)
    for k in ("staging", "uploads"):
        r = res[k]
        print(f"\n[{k}] 扫描 {r['scanned']}，{'将删除' if r['dry_run'] else '已删除'} "
              f"{r['deleted']}，释放 {r['freed_mb']} MB")
        for e in r["errors"][:3]:
            print(f"  ! {e}")
    print(f"\n合计释放 {res['total_freed_mb']} MB")
    if not args.apply:
        print("\n提示：加 --apply 才会真正删除")
    return 0


def print_stages(out: object) -> None:
    run = getattr(out, "stages_run", [])
    reused = getattr(out, "stages_reused", [])
    durations = getattr(out, "durations_ms", {})
    print("阶段执行：")
    for st in ("parse", "chunk", "embed"):
        if st in run:
            print(f"  {st:<6} 执行  {_fmt_ms(durations.get(st, 0))}")
        elif st in reused:
            print(f"  {st:<6} 复用（指纹未变）")
        else:
            print(f"  {st:<6} 跳过")
    total = sum(v for k, v in durations.items() if k in run)
    print(f"  {'合计':<5} {_fmt_ms(total)}")


def print_health(report: dict) -> None:
    h = report.get("health", {})
    mark = "✓" if h.get("level") == "ok" else "⚠"
    print(f"\n{mark} 健康分 {h.get('score', 0):.2f}")
    for issue in h.get("issues", []):
        print(f"    · {issue}")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据管道 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑完整管道")
    p_run.add_argument("file")
    p_run.add_argument("--doc-id", default=None)
    p_run.set_defaults(func=cmd_run)

    p_re = sub.add_parser("reprocess", help="单阶段重跑")
    p_re.add_argument("doc_id")
    p_re.add_argument("--stage", choices=["parse", "chunk", "embed"], default="chunk")
    p_re.add_argument("--source", default=None, help="从 parse 开始时需指定源文件")
    p_re.set_defaults(func=cmd_reprocess)

    p_st = sub.add_parser("status", help="查看中间产物状态")
    p_st.add_argument("doc_id")
    p_st.set_defaults(func=cmd_status)

    p_rp = sub.add_parser("report", help="质量报告")
    p_rp.add_argument("doc_id", nargs="?", default=None)
    p_rp.set_defaults(func=cmd_report)

    p_cl = sub.add_parser("clean", help="清理过期数据")
    p_cl.add_argument("--apply", action="store_true")
    p_cl.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
