#!/usr/bin/env python
"""扩充评测集并自动校验期望值。

## 为什么要校验

第一版我凭记忆写了 72 条，核对时发现两条错的：
- 广东投入强度我写 3.6，实际 3.78
- 「超过4000亿的省份」我漏了广东（5510.4）

**凭记忆出题一定会错。** 这个脚本把每条用例的期望事实，
拿去原文里精确匹配 —— 匹配不到就报错，不写进评测集。

## 校验规则

对每条 case 的每个 expect_fact：
1. 在两份公开文档的**全部解析文本**里搜索（含表格）
2. 找不到 → 该条标记为「可疑」，需要人工复核
3. 全部找到 → 通过

金额类还要额外校验：去掉千分位后应能匹配原文的数字。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.parsers import parse_file  # noqa: E402

# 用 project_root()，别再手算 parents[N]
from app.core.paths import data_path

ROOT = data_path()
PUBLIC = ROOT / "eval" / "fixtures" / "public"
CASES = ROOT / "eval" / "qa_cases" / "qa_cases.jsonl"
EXPAND = ROOT / "eval" / "qa_cases" / "_expand_v2.json"


def collect_corpus() -> str:
    """把两份文档的全部文本拼起来，作为校验底本。"""
    parts: list[str] = []
    for f in sorted(PUBLIC.iterdir()):
        if f.is_file() and f.suffix.lower() in (".pdf", ".html"):
            doc = parse_file(f)
            parts.append(f"\n### {f.name}\n")
            for b in doc.blocks:
                if b.text:
                    parts.append(b.text)
    return "\n".join(parts)


def main() -> int:
    if not EXPAND.exists():
        print(f"找不到 {EXPAND}")
        return 1

    data = json.loads(EXPAND.read_text(encoding="utf-8"))
    new_cases = data["cases"]

    print("加载原文…")
    corpus = collect_corpus()
    print(f"  底本 {len(corpus):,} 字符\n")

    existing: list[dict] = []
    if CASES.exists():
        existing = [
            json.loads(line)
            for line in CASES.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    existing_ids = {c["id"] for c in existing}

    passed: list[dict] = []
    suspicious: list[tuple[str, str, str]] = []

    for c in new_cases:
        if c["id"] in existing_ids:
            print(f"  SKIP {c['id']}（已存在）")
            continue

        problems: list[str] = []
        for fact in c.get("expect_facts", []):
            if fact not in corpus:
                # 数字类可能被表格切成两半，去掉千分位再试
                bare = fact.replace(",", "")
                if bare not in corpus.replace(",", ""):
                    problems.append(f"事实「{fact}」在原文找不到")

        if problems:
            suspicious.append((c["id"], c["question"], "; ".join(problems)))
        else:
            passed.append(c)

    print(f"\n校验结果：通过 {len(passed)}，可疑 {len(suspicious)}，跳过 {len(new_cases) - len(passed) - len(suspicious)}")

    if suspicious:
        print("\n⚠️  以下用例的期望值在原文中找不到，需人工复核：")
        for cid, q, why in suspicious:
            print(f"  [{cid}] {q[:40]}")
            print(f"        {why}")

    # 只写通过的
    merged = existing + passed
    CASES.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in merged) + "\n",
        encoding="utf-8",
    )
    from collections import Counter

    cats = Counter(c["category"] for c in merged)
    print(f"\n已写入 {CASES.relative_to(ROOT)}")
    print(f"  总计 {len(merged)} 条：{dict(cats)}")

    return 0 if not suspicious else 2


if __name__ == "__main__":
    raise SystemExit(main())
