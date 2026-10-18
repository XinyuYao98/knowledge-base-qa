#!/usr/bin/env python3
"""产品架构图的几何自检。

用法：
    python scripts/check-diagram.py

只校验栅格与对齐，不渲染。改图后跑一遍，比肉眼对快得多。
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "docs" / "diagrams" / "product-flow.svg"

# ---------- 栅格参数（改图时必须与 SVG 同步） ----------
CANVAS_W, CANVAS_H = 680, 420
MARGIN, GUTTER = 28, 24
COL_W = (CANVAS_W - MARGIN * 2 - GUTTER * 2) // 3
BOX_H, GAP, TOP = 46, 12, 56
COL_BOTTOM = TOP + BOX_H * 4 + GAP * 3

X1 = MARGIN
X2 = MARGIN + COL_W + GUTTER
X3 = MARGIN + (COL_W + GUTTER) * 2

# 索引卡内部
CARD_HEADER_LINE = 96
CARD_FOOTER_LINE = 248
INNER_TOP = CARD_HEADER_LINE + 4
INNER_BOTTOM = CARD_FOOTER_LINE - 4
STEP = (INNER_BOTTOM - INNER_TOP) / 4
CARD_ITEM_Y = [round(INNER_TOP + STEP * (i + 0.5)) for i in range(4)]


def row_y(i: int) -> tuple[int, int]:
    y0 = TOP + i * (BOX_H + GAP)
    return y0, y0 + BOX_H


failures: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def main() -> int:
    if not SVG.exists():
        print(f"找不到 {SVG}", file=sys.stderr)
        return 2

    try:
        root = ET.fromstring(SVG.read_text(encoding="utf-8"))
    except ET.ParseError as e:
        print(f"XML 语法错误: {e}", file=sys.stderr)
        return 2

    ns = "{http://www.w3.org/2000/svg}"
    rects = [
        {k: float(v) for k, v in r.attrib.items() if k in ("x", "y", "width", "height")}
        for r in root.findall(f".//{ns}rect")
    ]
    circles = [
        {k: float(v) for k, v in c.attrib.items() if k in ("cx", "cy")}
        for c in root.findall(f".//{ns}circle")
    ]

    print(f"画布      {CANVAS_W} × {CANVAS_H}")
    print(f"三栏      {X1}~{X1+COL_W} / {X2}~{X2+COL_W} / {X3}~{X3+COL_W}")
    print(f"索引卡条目 y = {CARD_ITEM_Y}")
    print("-" * 62)

    # 1. 三栏等宽等距
    expect(X1 == MARGIN, f"左边距应为 {MARGIN}")
    expect(CANVAS_W - (X3 + COL_W) == MARGIN, "右边距与左边距不等（不对称）")
    expect(X2 - (X1 + COL_W) == GUTTER, f"左中间距应为 {GUTTER}")
    expect(X3 - (X2 + COL_W) == GUTTER, f"中右间距应为 {GUTTER}")

    # 2. 索引卡位置与高度
    card = [r for r in rects if r.get("width") == COL_W and r.get("height") == COL_BOTTOM - TOP]
    expect(len(card) == 1, "找不到索引卡（应为宽=单栏宽、高=四行框总高的 rect）")
    if card:
        c = card[0]
        expect(c["x"] == X2, f"索引卡 x 应为 {X2}，实际 {c['x']}")
        expect(c["y"] == TOP, f"索引卡顶应为 {TOP}（与三栏同顶），实际 {c['y']}")
        expect(c["y"] + c["height"] == COL_BOTTOM,
               f"索引卡底应为 {COL_BOTTOM}（与三栏同底），实际 {c['y']+c['height']}")

    # 3. 索引卡四条圆点纵向均分
    expect(len(circles) == 4, f"索引卡应有 4 个圆点，实际 {len(circles)}")
    for i, c in enumerate(sorted(circles, key=lambda d: d.get("cy", 0))):
        expect(abs(c["cy"] - CARD_ITEM_Y[i]) < 0.6,
               f"第{i+1}条圆点 y 应为 {CARD_ITEM_Y[i]}，实际 {c['cy']}")

    # 4. 每条圆点 x 对齐
    if circles:
        xs = {c["cx"] for c in circles}
        expect(len(xs) == 1, f"四个圆点的 x 未对齐：{xs}")

    # 5. 左列与右列各 4 个节点框
    for name, x in (("左列", X1), ("右列", X3)):
        boxes = [r for r in rects if r.get("x") == x and r.get("width") == COL_W]
        expect(len(boxes) == 4, f"{name}应有 4 个节点框，实际 {len(boxes)}")
        expect([b["y"] for b in boxes] == [row_y(i)[0] for i in range(4)],
               f"{name}纵向位置与栅格不符：{[b['y'] for b in boxes]}")

    # 6. 框高与间距
    for i in range(3):
        expect(row_y(i + 1)[0] - row_y(i)[1] == GAP, f"第{i+1}段间距应为 {GAP}")

    # 7. 跨栏箭头在索引卡纵向中心
    mid = (TOP + COL_BOTTOM) // 2
    expect(mid == 166, f"索引卡纵向中心应为 166，实际 {mid}")

    if failures:
        print("发现问题：")
        for f in failures:
            print("  ✗", f)
        print("-" * 62)
        print(f"共 {len(failures)} 项不通过")
        return 1

    print("✅ 全部几何约束通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
