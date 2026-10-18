#!/usr/bin/env python3
"""一键确认：全采纳。

用法：
    python scripts/confirm_all.py           # 全采纳，生成回执
    python scripts/confirm_all.py --dry-run # 只看会生成什么

产物：docs/signoff-receipt.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from signoff import GROUP_NAMES, ITEMS, RECEIPT  # noqa: E402


def build(all_yes: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 决策确认回执 · v3.0",
        "",
        f"> 生成时间：{now}",
        f"> 依据：`docs/02-架构方案-v3.md` + `docs/05-决策确认单-可勾选.md`",
        "",
        f"汇总：采纳 {len(ITEMS)} 条 / 否决 0 条 / 跳过 0 条",
        "",
    ]

    current = ""
    for it in ITEMS:
        if it["group"] != current:
            current = it["group"]
            lines += [f"## {GROUP_NAMES[current]}", ""]
        mark = "[x] 采纳" if all_yes else "[ ] 未确认"
        lines.append(f"- {mark}  **{it['id']} {it['title']}**：{it['verdict']}")
    lines += ["", "**结论：全部采纳，可以开始 P0。**", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    content = build(all_yes=True)

    if args.dry_run:
        print(content)
        return 0

    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(content, encoding="utf-8")
    print()
    print(f"  已生成回执：{RECEIPT}")
    print(f"  共 {len(ITEMS)} 条，全部标记为采纳。")
    print()
    print("  请回到对话中回复：「回执已生成，开始 P0」")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
