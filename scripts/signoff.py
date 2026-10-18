#!/usr/bin/env python3
"""终端版决策确认单。

用法：
    python scripts/signoff.py            # 逐条确认
    python scripts/signoff.py --list     # 只看清单，不交互

按键：y 采纳 / n 否决(需写原因) / s 跳过 / b 回看上一条 / q 退出并保存

产物：docs/signoff-receipt.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECEIPT = ROOT / "docs" / "signoff-receipt.md"
STATE = ROOT / ".signoff-state.json"

ITEMS: list[dict[str, str]] = [
    # ---------- A ----------
    {
        "id": "A1",
        "group": "A",
        "title": "Embedding 模型",
        "verdict": "Qwen3-Embedding-4B-GGUF Q4_K_M（LM Studio / llama-cpp-python 加载）",
        "cost": "换模型 = 全部向量重算 + 全部文档重新索引，1GB 库约数十分钟到数小时",
    },
    {
        "id": "A2",
        "group": "A",
        "title": "向量维度",
        "verdict": "由所选模型决定（4B 原生 2560，可截断）",
        "cost": "维度不是独立参数，它是模型的属性；改维度等同 A1",
    },
    {
        "id": "A3",
        "group": "A",
        "title": "分块策略",
        "verdict": "父子块：子块 400 token（重叠 60）+ 父块 1500 token；表格行级切块",
        "cost": "改粒度 = 全部文档重切 + 重嵌入，历史消息里的引用定位会失效",
    },
    {
        "id": "A4",
        "group": "A",
        "title": "引用定位粒度",
        "verdict": "PDF=页码 / DOCX=章节路径 / PPTX=幻灯片序号 / XLSX=工作表+行号",
        "cost": "改定位方式，历史引用全部失效（用户点开会报找不到原文）",
    },
    {
        "id": "A5",
        "group": "A",
        "title": "数据隔离架构",
        "verdict": "三层：PG RLS（业务）+ Chroma tenant（向量）+ PG ready 文档过滤（兜底）",
        "cost": "改隔离模型 = 全表结构 + 全部检索代码 + 全部 API 鉴权逻辑",
    },
    # ---------- B ----------
    {
        "id": "B1",
        "group": "B",
        "title": "PDF 解析库",
        "verdict": "pypdf + pdfplumber（MIT），不用 PyMuPDF",
        "cost": "换回 PyMuPDF 需重写解析器 + 重跑评测 + 全部文档重新解析；收益是标题识别 ~85% → ~95%",
    },
    {
        "id": "B2",
        "group": "B",
        "title": "向量库",
        "verdict": "ChromaDB 1.5.9，tenant=user_id，每知识库一个 collection",
        "cost": "换回 pgvector 需重写检索层（但能换回 RLS 在向量层的兜底）",
    },
    {
        "id": "B3",
        "group": "B",
        "title": "关键词通道",
        "verdict": "jieba + rank_bm25（内存索引 + 磁盘快照 + 墓碑重建）",
        "cost": "换回 pg_trgm 只需替换一个模块；但丢掉「中文分词」这个技术叙事",
    },
    {
        "id": "B4",
        "group": "B",
        "title": "LangChain 边界",
        "verdict": "仅 L1 组件层（OpenAIEmbeddings / ChatOpenAI / Chroma），编排自己写",
        "cost": "改成 LCEL 编排 = 重写问答工作流，且会削弱可讲述性",
    },
    {
        "id": "B5",
        "group": "B",
        "title": "开源协议",
        "verdict": "MIT",
        "cost": "改协议本身成本低，但已发布的版本无法收回；且改 AGPL 才能用 PyMuPDF",
    },
    # ---------- C ----------
    {
        "id": "C1",
        "group": "C",
        "title": "Rerank",
        "verdict": "Qwen3-Reranker-0.6B-Q8；粗召回 20 → 重排 → top 8",
        "cost": "配置项开关，关掉退回 RRF 顺序",
    },
    {
        "id": "C2",
        "group": "C",
        "title": "RRF 与召回参数",
        "verdict": "RRF k=60；向量/BM25 各 top 20；融合后候选 20",
        "cost": "配置项",
    },
    {
        "id": "C3",
        "group": "C",
        "title": "引用修复次数",
        "verdict": "MAX_REPAIR_ATTEMPTS = 1",
        "cost": "配置项；不建议 >1，延迟翻倍且收益递减",
    },
    {
        "id": "C4",
        "group": "C",
        "title": "配额",
        "verdict": "5 库 / 1 GiB / 单文件 200 MB / 1000 文件 / 日 200 次 / 并发 3",
        "cost": "常量，改一行",
    },
    {
        "id": "C5",
        "group": "C",
        "title": "原文件存储",
        "verdict": "本地卷（不用 MinIO）",
        "cost": "换对象存储只需替换 storage 模块",
    },
    {
        "id": "C6",
        "group": "C",
        "title": "登录",
        "verdict": "邮箱+密码 argon2id；access 15min + refresh 7d；无邮箱验证、无找回密码",
        "cost": "加邮箱验证需引入 SMTP 配置",
    },
    {
        "id": "C7",
        "group": "C",
        "title": "部署形态",
        "verdict": "开发=本地 Docker+LM Studio；分发=Compose；云端=静态展示页",
        "cost": "云端要真正在线试用需租 GPU 实例",
    },
    {
        "id": "C8",
        "group": "C",
        "title": "示例与评测数据",
        "verdict": "演示用现网真实文档；评测用合成可控数据",
        "cost": "换数据集不影响代码",
    },
    {
        "id": "C9",
        "group": "C",
        "title": "演示体验",
        "verdict": "一键起 + 预置示例知识库（已索引）+ 一段预设对话",
        "cost": "纯数据文件",
    },
    # ---------- D ----------
    {
        "id": "D1",
        "group": "D",
        "title": "示例文档来源",
        "verdict": "由你自己从网上找，我只负责写 seed 脚本把文档灌进去",
        "cost": "若希望我生成示例文档，需要额外做 PDF/DOCX/XLSX 生成脚本",
    },
    {
        "id": "D2",
        "group": "D",
        "title": "示例文档类型",
        "verdict": "可以包含公开上市公司年报等公开文档（本地演示用途）",
        "cost": "若介意版权，改用开源项目文档或自造",
    },
    {
        "id": "D3",
        "group": "D",
        "title": "前端范围",
        "verdict": "4 个核心页面：登录、库列表、库详情+上传、问答页；不做营销首页",
        "cost": "若需要落地页，加一个页面",
    },
    {
        "id": "D4",
        "group": "D",
        "title": "多轮对话能力",
        "verdict": "不做指代消解优化（如「它的第二条呢」），历史只做简单拼接",
        "cost": "若需要，加一轮 query rewrite，延迟 +500ms",
    },
    {
        "id": "D5",
        "group": "D",
        "title": "知识库分享",
        "verdict": "不做用户间知识库分享",
        "cost": "需求里未要求",
    },
    {
        "id": "D6",
        "group": "D",
        "title": "1GB 的计量口径",
        "verdict": "指原始文件大小，不是切块后的文本大小",
        "cost": "若指后者，配额校验逻辑要改",
    },
]

GROUP_NAMES = {
    "A": "A 组 · 不可逆决策（改了就回不去）",
    "B": "B 组 · 模块级决策（改动范围 = 一个模块）",
    "C": "C 组 · 配置级决策（改一个常量或开关）",
    "D": "D 组 · 我替你假设的前提（不对的话现在说）",
}

W = 78


def _hr(ch: str = "─") -> str:
    return ch * W


def _wrap(text: str, width: int, indent: str = "   ") -> str:
    """按显示宽度折行，中文按 2 列算。"""
    lines: list[str] = []
    cur = ""
    cur_w = 0
    for ch in text:
        w = 2 if ord(ch) > 0x2E80 else 1
        if cur_w + w > width:
            lines.append(indent + cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur:
        lines.append(indent + cur)
    return "\n".join(lines)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def show(item: dict[str, str], idx: int, total: int) -> None:
    print()
    print(_hr())
    print(f"  [{idx}/{total}]  {item['id']}  {item['title']}")
    print(_hr())
    print("  结论：")
    print(_wrap(item["verdict"], W - 6))
    print()
    print("  反悔成本：")
    print(_wrap(item["cost"], W - 6))
    print(_hr())


def ask() -> str:
    while True:
        raw = input("  [y]采纳  [n]否决  [s]跳过  [b]上一条  [q]退出并保存 > ").strip().lower()
        if raw in ("y", "n", "s", "b", "q"):
            return raw
        print("  请输入 y / n / s / b / q")


def build_receipt(state: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"# 决策确认回执 · v3.0", "", f"> 生成时间：{now}", ""]

    counts = {"y": 0, "n": 0, "s": 0, "": 0}
    for it in ITEMS:
        s = state.get(it["id"], {})
        counts[s.get("ans", "")] = counts.get(s.get("ans", ""), 0) + 1

    lines.append(f"汇总：采纳 {counts['y']} 条 / 否决 {counts['n']} 条 / 跳过 {counts['s']} 条 / 未答 {counts['']} 条")
    lines.append("")

    current_group = ""
    for it in ITEMS:
        if it["group"] != current_group:
            current_group = it["group"]
            lines.append(f"## {GROUP_NAMES[current_group]}")
            lines.append("")
        s = state.get(it["id"], {})
        ans = s.get("ans", "")
        mark = {"y": "[x] 采纳", "n": "[!] 否决", "s": "[-] 跳过"}.get(ans, "[ ] 未答")
        lines.append(f"- {mark}  **{it['id']} {it['title']}**：{it['verdict']}")
        if s.get("note"):
            lines.append(f"  - 备注：{s['note']}")
    lines.append("")

    if counts["y"] == len(ITEMS):
        lines.append("**结论：全部采纳，可以开始 P0。**")
    elif counts["n"]:
        lines.append("**结论：存在否决项，需先对齐后再开工。**")
    else:
        lines.append("**结论：仍有未答项，请补齐后再开工。**")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只打印清单")
    ap.add_argument("--reset", action="store_true", help="清除已有进度")
    args = ap.parse_args()

    if args.reset and STATE.exists():
        STATE.unlink()
        print("已清除上次进度")

    if args.list:
        for g in ("A", "B", "C", "D"):
            print()
            print(GROUP_NAMES[g])
            print(_hr())
            for it in [i for i in ITEMS if i["group"] == g]:
                print(f"  {it['id']:4} {it['title']:16} {it['verdict']}")
        print()
        return 0

    state = load_state()
    total = len(ITEMS)

    print()
    print(_hr("═"))
    print("  决策确认单 · v3.0")
    print("  逐条确认，结束后自动生成 docs/signoff-receipt.md")
    if state:
        done = sum(1 for i in ITEMS if state.get(i["id"], {}).get("ans"))
        print(f"  检测到上次进度：已答 {done}/{total} 条")
    print(_hr("═"))

    i = 0
    while i < total:
        item = ITEMS[i]
        prev = state.get(item["id"], {})
        if prev.get("ans"):
            print()
            print(f"  [{i + 1}/{total}] {item['id']} 已标记为「{prev['ans']}」，跳过。")
            i += 1
            continue

        show(item, i + 1, total)
        ans = ask()

        if ans == "q":
            save_state(state)
            print("\n  进度已保存。下次运行会从断点继续。\n")
            return 0
        if ans == "b":
            if i > 0:
                i -= 1
                prev_id = ITEMS[i]["id"]
                if prev_id in state:
                    del state[prev_id]
            continue
        if ans == "n":
            note = input("  否决原因（必填）> ").strip()
            while not note:
                note = input("  否决必须写原因 > ").strip()
            state[item["id"]] = {"ans": "n", "note": note}
        elif ans == "s":
            note = input("  跳过原因（可选，回车略过）> ").strip()
            state[item["id"]] = {"ans": "s", "note": note}
        else:
            state[item["id"]] = {"ans": "y"}
        save_state(state)
        i += 1

    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(build_receipt(state), encoding="utf-8")

    y = sum(1 for it in ITEMS if state.get(it["id"], {}).get("ans") == "y")
    n = sum(1 for it in ITEMS if state.get(it["id"], {}).get("ans") == "n")
    s = sum(1 for it in ITEMS if state.get(it["id"], {}).get("ans") == "s")

    print()
    print(_hr("═"))
    print(f"  完成：采纳 {y} / 否决 {n} / 跳过 {s}")
    print(f"  回执已写入：{RECEIPT}")
    print(_hr("═"))
    print()

    if n:
        print("  存在否决项，请先与我讨论再开工。")
        return 1
    if s:
        print("  存在跳过项，建议补齐；未答项我按默认路径推进。")
        return 0
    print("  全部采纳。请回复我「回执已生成」，我将开始 P0。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
