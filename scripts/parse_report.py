#!/usr/bin/env python3
"""解析质量报告生成器。

对素材库里的每份文件跑一遍解析，输出一份可读的质量报告。

## 为什么需要它

架构文档里说「解析质量可观测」是核心卖点。这句话不能只是口号 ——
必须有一个东西能回答：「这份 PDF 解析得怎么样？」

报告回答四个问题：
1. 提取到多少内容（块数、字符数、表格数）
2. 有哪些降级（扫描件、合并单元格、编码猜测）
3. 哪里可能有问题（低密度页、超短块、表头缺失）
4. 与上次相比有没有变差（可选：对比基线）

用法：
    python scripts/parse_report.py
    python scripts/parse_report.py --json       # 机器可读
    python scripts/parse_report.py --baseline   # 与基线对比
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.rag.parsers import (  # noqa: E402
    SUPPORTED_EXTENSIONS,
    BlockType,
    ParseError,
    ParsedDoc,
    parse_file,
)

FIXTURES = ROOT / "eval" / "fixtures"
BASELINE = ROOT / "eval" / "expected" / "_parse_baseline.json"


def collect_files() -> list[tuple[Path, str]]:
    """收集待测文件，标注来源（synthetic / public / private）。"""
    out: list[tuple[Path, str]] = []
    for kind in ("synthetic", "public", "private"):
        base = FIXTURES / kind
        if not base.exists():
            continue
        for f in sorted(base.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower().lstrip(".") not in SUPPORTED_EXTENSIONS:
                continue
            # README 是目录说明，不是测试素材
            if f.name.upper().startswith("README"):
                continue
            out.append((f, kind))
    return out


def analyze(doc: ParsedDoc) -> dict:
    """从 ParsedDoc 提炼质量指标与问题。"""
    paras = [b for b in doc.blocks if b.type == BlockType.PARAGRAPH]
    tables = [b for b in doc.blocks if b.type == BlockType.TABLE]

    # 超短正文块：可能是导航残留或碎句
    # 只算 PARAGRAPH —— 标题、列表项、表格行本来就短，不该算问题
    short_blocks = [
        b
        for b in doc.blocks
        if b.type == BlockType.PARAGRAPH and 0 < b.char_count < 10
    ]

    # 低密度页
    low_pages = []
    if doc.page_char_counts:
        low_pages = [
            i + 1 for i, n in enumerate(doc.page_char_counts) if n < 50
        ]

    # 表头质量：是否出现「列1列2」这种占位
    placeholder_headers = sum(
        1 for t in tables if "列1" in t.text.split("\n")[0][:40]
    )

    issues: list[str] = []
    if low_pages:
        issues.append(f"{len(low_pages)} 页无可提取文字（扫描件？）")
    if short_blocks:
        issues.append(f"{len(short_blocks)} 个块短于 10 字")
    if placeholder_headers:
        issues.append(f"{placeholder_headers} 张表未识别出表头")
    if doc.total_chars < 100:
        issues.append("总字符数过少")
    for w in doc.warnings:
        if str(w.code) in ("table_detect_failed", "formula_no_cache", "partial_text"):
            issues.append(w.message)

    avg_para = (
        round(sum(b.char_count for b in paras) / len(paras), 1) if paras else 0
    )

    return {
        "block_count": doc.block_count,
        "total_chars": doc.total_chars,
        "pages": doc.pages,
        "table_count": doc.table_count,
        "heading_count": doc.heading_count,
        "paragraph_avg_chars": avg_para,
        "warning_count": len(doc.warnings),
        "warnings": [str(w.code) for w in doc.warnings],
        "low_text_pages": low_pages,
        "short_blocks": len(short_blocks),
        "placeholder_headers": placeholder_headers,
        "issues": issues,
    }


def grade(metrics: dict) -> str:
    """给一个粗略等级，便于一眼看出哪份素材有问题。"""
    if metrics["issues"]:
        return "⚠"
    if metrics["warning_count"]:
        return "△"
    return "✓"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--baseline", action="store_true", help="与基线对比并更新基线")
    args = ap.parse_args()

    files = collect_files()
    if not files:
        print("找不到素材文件", file=sys.stderr)
        return 2

    results: list[dict] = []
    total_ms = 0.0

    for path, kind in files:
        started = time.perf_counter()
        entry: dict = {"file": path.name, "kind": kind, "format": path.suffix.lstrip(".")}
        try:
            doc = parse_file(path)
            elapsed = (time.perf_counter() - started) * 1000
            total_ms += elapsed
            entry.update(
                {
                    "status": "ok",
                    "elapsed_ms": round(elapsed, 1),
                    "metrics": analyze(doc),
                }
            )
            entry["grade"] = grade(entry["metrics"])
        except ParseError as e:
            entry.update(
                {
                    "status": "error",
                    "error_code": str(e.code),
                    "error_message": e.message,
                    "grade": "✗" if str(e.code) not in ("empty_file",) else "○",
                }
            )
        except Exception as e:
            entry.update(
                {
                    "status": "crash",
                    "error_code": type(e).__name__,
                    "error_message": str(e)[:200],
                    "grade": "✗",
                }
            )
        results.append(entry)

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return 0

    # ---- 人读格式 ----
    print("=" * 96)
    print("解析质量报告")
    print("=" * 96)

    ok = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] != "ok"]

    print(
        f"\n共 {len(results)} 份文件：成功 {len(ok)}，失败 {len(err)}，"
        f"总耗时 {total_ms / 1000:.2f}s\n"
    )

    print(f"{'':2} {'文件':<42} {'块':>4} {'字符':>7} {'表':>3} {'告警':>4} {'耗时':>8}")
    print("-" * 96)
    for r in sorted(results, key=lambda x: (x["kind"], x["file"])):
        if r["status"] == "ok":
            m = r["metrics"]
            print(
                f"{r['grade']:2} {r['file'][:42]:<42} {m['block_count']:>4} "
                f"{m['total_chars']:>7} {m['table_count']:>3} "
                f"{m['warning_count']:>4} {r['elapsed_ms']:>7.0f}ms"
            )
        else:
            print(
                f"{r['grade']:2} {r['file'][:42]:<42} "
                f"  --  {r['error_code']}"
            )

    # ---- 问题明细 ----
    problem_items = [r for r in results if r.get("metrics", {}).get("issues") or r["status"] != "ok"]
    if problem_items:
        print("\n" + "-" * 96)
        print("需要注意的项：")
        for r in problem_items:
            if r["status"] != "ok":
                print(f"  {r['grade']} {r['file']}")
                print(f"      {r['error_code']}: {r.get('error_message', '')[:70]}")
            else:
                print(f"  {r['grade']} {r['file']}")
                for issue in r["metrics"]["issues"]:
                    print(f"      · {issue}")

    # ---- 汇总 ----
    print("\n" + "-" * 96)
    by_kind: dict[str, list[dict]] = {}
    for r in results:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind, items in by_kind.items():
        ok_n = sum(1 for r in items if r["status"] == "ok")
        chars = sum(r.get("metrics", {}).get("total_chars", 0) for r in items)
        tables = sum(r.get("metrics", {}).get("table_count", 0) for r in items)
        print(
            f"  {kind:<10} {len(items):>2} 份 | 成功 {ok_n:>2} | "
            f"总计 {chars:>7} 字符 | {tables:>2} 张表"
        )

    # ---- 基线对比 ----
    if args.baseline:
        current = {
            r["file"]: {
                "block_count": r.get("metrics", {}).get("block_count", 0),
                "total_chars": r.get("metrics", {}).get("total_chars", 0),
                "table_count": r.get("metrics", {}).get("table_count", 0),
                "status": r["status"],
            }
            for r in results
        }

        if BASELINE.exists():
            previous = json.loads(BASELINE.read_text(encoding="utf-8"))
            print("\n" + "-" * 96)
            print("与基线对比：")
            regressions = []
            for fname, cur in current.items():
                prev = previous.get(fname)
                if not prev:
                    print(f"  + {fname}（新增）")
                    continue
                if prev.get("status") == "ok" and cur["status"] != "ok":
                    regressions.append(f"{fname}: 从成功变为失败")
                elif prev.get("total_chars", 0) > 0:
                    delta = cur["total_chars"] - prev["total_chars"]
                    pct = delta / prev["total_chars"] * 100
                    if abs(pct) >= 5:
                        mark = "↓" if delta < 0 else "↑"
                        print(f"  {mark} {fname}: 字符 {prev['total_chars']} → {cur['total_chars']} ({pct:+.1f}%)")
                        if pct < -20:
                            # ## 不能一律判回归（P7 实测）
                            #
                            # 表格 PDF 的字符数可能**因为去重而下降**：
                            # 旧解析把同一张表同时输出成 paragraph（压平的整表）
                            # 和 table（结构化的），字符数被算了两遍。
                            # 剔除重复后总量掉 40% 是**修复**，不是回归。
                            #
                            # 区分办法：表格数 > 0 的文件，字符下降 ± 表格重复
                            # 是合理的；没有表格的文件下降才是真回归。
                            if cur.get("table_count", 0) > 0:
                                print(
                                    f"       （含 {cur['table_count']} 张表，"
                                    f"下降可能来自表格去重，请人工确认）"
                                )
                            else:
                                regressions.append(
                                    f"{fname}: 字符数下降 {abs(pct):.0f}%"
                                )
            if regressions:
                print("\n  ⚠ 疑似回归：")
                for r in regressions:
                    print(f"     {r}")
            else:
                print("  ✓ 无回归")
        else:
            print("\n（首次生成基线）")

        BASELINE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n基线已写入 {BASELINE.relative_to(ROOT)}")

    print("=" * 96)
    return 0 if not err or all(r.get("error_code") == "empty_file" for r in err) else 0


if __name__ == "__main__":
    raise SystemExit(main())
