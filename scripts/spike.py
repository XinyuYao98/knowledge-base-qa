#!/usr/bin/env python3
"""竖切片：最小可用的端到端链路。

    TXT/MD 文件 → 解析 → 切块 → 向量化 → 写 Chroma → 检索 → 生成 → 引用校验

## 为什么要有这一步

不是做功能，是验证四个基础假设：

1. 模型服务能连上（含绕开系统代理）
2. Chroma 读写正常（维度、collection 隔离）
3. 提示词能让模型说人话（不编造、能拒答）
4. 引用编号能被正确解析

**任何一项不通，后面写的切块、检索、API 全要返工。**

## 用法

    python scripts/spike.py                      # 跑内置用例
    python scripts/spike.py --file my.txt        # 用自己的文件
    python scripts/spike.py --reset              # 清空测试 collection 重来

前置条件：LM Studio 已加载 embedding 与 chat 模型。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.core.platform import selector_loop_factory  # noqa: E402
from app.index import chroma_store  # noqa: E402
from app.rag.chunker import chunk_document  # noqa: E402
from app.rag.citations import check_citations, format_context  # noqa: E402
from app.rag.models import ChatClient, EmbeddingClient, ModelServiceError  # noqa: E402
from app.rag.parsers import parse_file  # noqa: E402
from app.rag.prompts.qa import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt  # noqa: E402

#: 固定的测试 id，便于 --reset 时清掉
SPIKE_KB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

SAMPLE = """青岚智造科技有限公司 差旅与费用管理制度

一、差旅管理

1.1 住宿标准
一线城市每晚不超过 480 元，二线城市每晚不超过 350 元，其他城市每晚不超过 280 元。
住宿超标部分由个人承担，特殊情况需在出差前提交书面说明并经分管副总批准。

1.2 交通标准
市内交通凭票实报实销，单日不超过 120 元。
高铁出行原则乘坐二等座；如因公务需要乘坐一等座，需提前审批。
飞机出行原则乘坐经济舱；连续飞行超过 5 小时可乘坐商务舱。

1.3 出差补贴
每人每天 120 元，按实际出差天数计算，出发与返回当日各按半天计。

二、采购管理

2.1 审批权限
单笔采购金额 5 万元以下，由部门负责人审批。
单笔 5 万元至 20 万元，由分管副总审批。
单笔超过 20 万元，由总经理审批，并须附三家以上供应商比价记录。

2.2 供应商管理
新增供应商需提交营业执照、开户许可证与近一年纳税证明。
供应商每年复审一次，连续两年评级低于 B 级的移出合格名录。

三、用印管理

3.1 用印流程
合同用印需在 OA 系统提交申请，经法务审核、分管领导审批后，由行政部统一加盖。
3.2 禁止事项
空白合同、空白介绍信一律不得用印。
"""

#: 测试用例：(问题, 期望关键词, 是否应当拒答)
CASES = [
    ("一线城市出差住宿每晚能报多少？", ["480"], False),
    ("单笔采购 15 万元需要谁审批？", ["副总", "分管"], False),
    ("出差补贴每天多少钱？", ["120"], False),
    ("连续飞行多久可以坐商务舱？", ["5 小时", "5小时"], False),
    ("公司年会在哪家酒店举办？", [], True),
]


def ensure_sample() -> Path:
    """把内置样例写到临时文件。"""
    p = Path(tempfile.gettempdir()) / "spike_kbqa_差旅制度.txt"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


async def run(file_path: Path, reset: bool) -> int:
    print("=" * 72)
    print("竖切片：TXT → 切块 → 向量化 → Chroma → 检索 → 生成 → 引用校验")
    print("=" * 72)

    # ---- 0. 环境检查 ----
    print("\n[0] 检查模型服务")
    emb = EmbeddingClient()
    chat = ChatClient()

    h1 = await emb.health()
    print(f"    Embedding: {settings.EMBED_MODEL}")
    print(f"      {'✓' if h1.get('ok') else '✗'} 服务可达, 模型存在={h1.get('model_present')}")
    if not h1.get("ok"):
        print(f"      错误: {h1.get('error')}")
        return 2
    if not h1.get("model_present"):
        print(f"      可用模型: {h1.get('available_models')}")
        print(f"      请把 EMBED_MODEL 改成上面之一")
        return 2

    h2 = await chat.health()
    print(f"    Chat: {settings.CHAT_MODEL}")
    print(f"      {'✓' if h2.get('ok') else '✗'} 服务可达, 模型存在={h2.get('model_present')}")
    if not h2.get("ok") or not h2.get("model_present"):
        print(f"      可用模型: {h2.get('available_models')}")
        return 2

    print(f"    Chroma: mode={settings.CHROMA_MODE}, heartbeat={chroma_store.heartbeat()}")

    # ---- 1. 解析 ----
    print(f"\n[1] 解析 {file_path.name}")
    t0 = time.perf_counter()
    doc = parse_file(file_path)
    print(f"    ✓ {doc.block_count} 块, {doc.total_chars} 字符, {time.perf_counter()-t0:.2f}s")
    for w in doc.warnings:
        print(f"    △ {w.code}: {w.message[:50]}")

    # ---- 2. 切块 ----
    print("\n[2] 切块")
    t0 = time.perf_counter()
    chunks = chunk_document(doc)
    print(f"    ✓ {len(chunks)} 块, 平均 {sum(c.token_estimate for c in chunks)//max(len(chunks),1)} token/块")
    print(f"    {'  '.join(f'#{c.seq}({c.token_estimate}t)' for c in chunks[:6])}…")

    # ---- 3. 向量化 ----
    print("\n[3] 向量化")
    t0 = time.perf_counter()
    vectors = await emb.embed_documents([c.text for c in chunks])
    el = time.perf_counter() - t0
    print(f"    ✓ {len(vectors)} 个向量, {len(vectors[0])} 维, {el:.2f}s "
          f"({len(vectors)/el:.1f} 块/秒)")

    # ---- 4. 写入 Chroma ----
    print("\n[4] 写入 Chroma")
    kb_id = uuid.uuid4()   # 每次跑用新 collection，便于观察
    if reset:
        chroma_store.drop_collection(SPIKE_KB_ID)
    t0 = time.perf_counter()
    n = chroma_store.add_chunks(kb_id, doc_id := uuid.uuid4(), chunks, vectors)
    print(f"    ✓ 写入 {n} 条到 {chroma_store.collection_name(kb_id)[:16]}…, "
          f"{time.perf_counter()-t0:.2f}s")
    st = chroma_store.stats(kb_id)
    print(f"    collection 计数: {st.get('count')}")

    # ---- 5-7. 逐题问答 ----
    print("\n[5] 检索 + 生成 + 引用校验")
    print("-" * 72)

    passed = 0
    for qi, (question, expect, should_refuse) in enumerate(CASES, start=1):
        print(f"\n  Q{qi}: {question}")

        # 检索
        t0 = time.perf_counter()
        qvec = await emb.embed_query(question)
        hits = chroma_store.query(kb_id, qvec, top_k=settings.RETRIEVE_FINAL_TOP_K)
        retrieve_ms = (time.perf_counter() - t0) * 1000

        if not hits:
            print("    ✗ 检索无结果")
            continue

        for h in hits:
            meta = h["metadata"]
            print(f"      · {h['score']:.3f}  p{meta.get('page', '-')}  "
                  f"{h['text'][:38].replace(chr(10), ' ')}…")

        # 拼上下文
        items = [
            {
                "text": h["text"],
                "label": f"{h['metadata'].get('source', '')} "
                         f"第{h['metadata'].get('page', '?')}页 "
                         f"{h['metadata'].get('heading_path', '')}".strip(),
                "score": h["score"],
            }
            for h in hits
        ]
        context, used = format_context(items, max_chars=settings.CHAT_CONTEXT_MAX_TOKENS * 2)

        # 生成
        t0 = time.perf_counter()
        try:
            result = await chat.complete(
                system=SYSTEM_PROMPT,
                user=build_user_prompt(context, question),
            )
        except ModelServiceError as e:
            print(f"    ✗ 生成失败: {e.message}")
            continue
        gen_ms = (time.perf_counter() - t0) * 1000

        answer = result["content"]
        print(f"    A: {answer}")

        # 引用校验
        check = check_citations(answer, available=len(used))
        ok_cite = "✓" if check.is_valid else "✗"
        print(f"    {ok_cite} 引用 {check.cited}"
              + (f"  越界 {check.invalid}" if check.invalid else "")
              + f" | 检索 {retrieve_ms:.0f}ms 生成 {gen_ms:.0f}ms "
                f"| 思考 {result['reasoning_tokens']}t")

        # 内容正确性
        if should_refuse:
            content_ok = ("未提及" in answer) or ("无法" in answer) or ("没有" in answer)
            print(f"    {'✓' if content_ok else '✗'} 拒答判断")
        else:
            content_ok = any(k in answer for k in expect)
            print(f"    {'✓' if content_ok else '✗'} 关键词 {expect}")

        if content_ok and check.is_valid:
            passed += 1
        else:
            # 引用校验失败 → 记录修复建议（竖切片只观察，不真修复）
            if not check.is_valid:
                print(f"    △ 需修复：引用无效（越界 {check.invalid}）")

    # ---- 汇总 ----
    print("\n" + "=" * 72)
    rate = passed / len(CASES) * 100
    print(f"结果：{passed}/{len(CASES)} 通过（{rate:.0f}%）")
    print(f"prompt 版本: {PROMPT_VERSION}")
    print(f"collection: {chroma_store.collection_name(kb_id)}（已保留，便于人工检查）")
    print("=" * 72)
    return 0 if passed == len(CASES) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    target = args.file or ensure_sample()
    if not target.exists():
        print(f"文件不存在：{target}", file=sys.stderr)
        return 2

    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(run(target, args.reset))
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
