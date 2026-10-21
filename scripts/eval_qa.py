#!/usr/bin/env python
"""P7 评测命令行。

    # 单次基线
    python scripts/eval_qa.py run

    # 只跑指定类别
    python scripts/eval_qa.py run --category normal

    # 四组对照实验
    python scripts/eval_qa.py compare

    # 看历史结果
    python scripts/eval_qa.py report

## 为什么评测结果要落盘成 JSON

一次评测包含 26 条用例的全部细节（答案原文、命中位置、耗时）。
只打印摘要等于丢掉排查依据 —— 某条失败了，需要看它到底答了什么。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.core.logging import get_logger  # noqa: E402
from app.core.paths import data_path  # noqa: E402
from app.core.platform import selector_loop_factory  # noqa: E402
from app.eval.runner import (  # noqa: E402
    Case,
    EvalConfig,
    build_env,
    load_cases,
    patched_settings,
    run_case,
    summarize,
)

log = get_logger("eval_cli")

RESULTS_DIR = data_path("eval", "results")


def _out_path(tag: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return RESULTS_DIR / f"{tag}_{ts}.json"


async def _run_one(cfg: EvalConfig, cases: list[Case]) -> dict:
    from app.services.qa import Answerer

    print(f"\n▶ 配置：{cfg.describe()}")
    print(f"  用例：{len(cases)} 条")

    async with build_env() as env:
        print(f"  环境：kb={env.kb_id[:8]} 文档={len(env.doc_ids)}")

        answerer = Answerer()
        results = []

        async with patched_settings(cfg):
            for i, case in enumerate(cases, start=1):
                r = await run_case(case, env, answerer)
                results.append(r)
                mark = "✓" if r.passed else "✗"
                print(f"  {mark} [{i:2}/{len(cases)}] {case.id} {case.question[:28]}")
                if not r.passed:
                    print(f"        ↳ {r.reason}")

    summary = summarize(results, cfg)
    return {
        "summary": summary,
        "results": [r.to_dict() for r in results],
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(),
            "chat_model": settings.CHAT_MODEL,
            "embed_model": settings.EMBED_MODEL,
            "rerank_model": settings.RERANK_MODEL,
        },
    }


def _print_summary(s: dict) -> None:
    print(f"\n{'=' * 62}")
    print(f"配置：{s['config']}")
    print(f"通过：{s['passed']}/{s['total']}  ({s['pass_rate']:.1%})")
    for cat, v in s["by_category"].items():
        print(f"  {cat:12} {v['passed']}/{v['total']}")

    r = s["retrieval"]
    print(f"\n检索（{r['cases_with_doc']} 条有期望文档）")
    print(f"  recall@5  {r['recall@5']:.1%}")
    print(f"  recall@20 {r['recall@20']:.1%}")
    print(f"  MRR       {r['mrr']:.3f}")

    g = s["generation"]
    print("生成")
    print(f"  引用有效  {g['citation_valid_rate']:.1%}")
    print(f"  事实命中  {g['fact_hit_rate']:.1%}")

    lat = s["latency"]
    print(f"耗时  p50={lat['p50_ms'] / 1000:.1f}s  "
          f"p95={lat['p95_ms'] / 1000:.1f}s  max={lat['max_ms'] / 1000:.1f}s")


async def cmd_run(args: argparse.Namespace) -> int:
    cases = load_cases()
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.limit:
        cases = cases[: args.limit]

    cfg = EvalConfig(name="baseline")
    data = await _run_one(cfg, cases)

    path = _out_path(args.tag or "run")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(data["summary"])
    print(f"\n详情已保存：{path}")
    return 0 if data["summary"]["pass_rate"] >= 0.5 else 1


async def cmd_compare(args: argparse.Namespace) -> int:
    """四组对照实验（架构 §11.4）。

    每组只改一个开关，其余保持基线值 —— 单变量对比才有意义。
    """
    all_cases = load_cases()

    experiments = [
        EvalConfig(name="baseline"),
        EvalConfig(name="no-rerank", use_rerank=False),
        EvalConfig(name="no-bm25", use_bm25=False),
        EvalConfig(name="no-rewrite", use_rewrite=False),
    ]

    if args.only:
        experiments = [e for e in experiments if e.name in args.only]

    collected = []
    for cfg in experiments:
        cases = all_cases
        if cfg.name == "no-rewrite":
            # 关改写只影响多轮追问，单轮没有差别 —— 需要专门的多轮用例
            cases = [c for c in all_cases if c.category == "normal"]
        data = await _run_one(cfg, cases)
        collected.append(data)
        _print_summary(data["summary"])

    # 对照表
    print(f"\n{'=' * 62}")
    print("对照结果")
    print(f"{'配置':<22}{'通过率':>10}{'recall@5':>12}{'MRR':>8}")
    for d in collected:
        s = d["summary"]
        print(
            f"{s['config']:<22}{s['pass_rate']:>10.1%}"
            f"{s['retrieval']['recall@5']:>12.1%}{s['retrieval']['mrr']:>8.3f}"
        )

    path = _out_path("compare")
    path.write_text(
        json.dumps({"experiments": collected}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n详情已保存：{path}")
    return 0


async def cmd_stability(args: argparse.Namespace) -> int:
    """稳定性实验：同一配置重复跑，看结果波动。

    ## 为什么必须做这个

    P7 第一次对照实验里，同一个 no-rerank 配置跑了两次：
    第一次 92/109、第二次 86/109 —— **差 6 条**。

    如果单次结果就波动 5%，那任何「A 比 B 高 7%」的结论都不可信。
    稳定性是评测结论的前提。
    """
    cases = load_cases()[: args.limit]
    cfg = EvalConfig(name="stability")

    runs: list[dict[str, bool]] = []
    answers: list[dict[str, str]] = []

    for i in range(args.repeat):
        print(f"\n▶ 第 {i + 1}/{args.repeat} 轮")
        data = await _run_one(cfg, cases)
        runs.append({r["case_id"]: r["passed"] for r in data["results"]})
        answers.append({r["case_id"]: r["answer"] for r in data["results"]})

    ids = list(runs[0].keys())
    unstable = [cid for cid in ids if len({run[cid] for run in runs}) > 1]
    always_pass = [cid for cid in ids if all(run[cid] for run in runs)]
    always_fail = [cid for cid in ids if not any(run[cid] for run in runs)]

    n = len(ids)
    print(f"\n{'=' * 62}")
    print(f"稳定性实验：{args.repeat} 轮 × {n} 条")
    print(f"  每轮通过数：{[sum(r.values()) for r in runs]}")
    print(f"  稳定通过：{len(always_pass)}  稳定失败：{len(always_fail)}  抖动：{len(unstable)}")
    print(f"  **一致率：{(n - len(unstable)) / n:.1%}**")

    if unstable:
        print(f"\n抖动用例（{len(unstable)} 条）：")
        for cid in unstable[:10]:
            pattern = "".join("✓" if run[cid] else "✗" for run in runs)
            print(f"  {cid}: {pattern}")

    path = _out_path("stability")
    path.write_text(
        json.dumps(
            {
                "repeat": args.repeat,
                "total": n,
                "consistent": n - len(unstable),
                "unstable": unstable,
                "runs": runs,
                "answers": answers,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n详情已保存：{path}")
    return 0 if len(unstable) / n < 0.05 else 1


def cmd_report(_args: argparse.Namespace) -> int:
    if not RESULTS_DIR.exists():
        print("还没有评测结果")
        return 1

    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        print("还没有评测结果")
        return 1

    print(f"共 {len(files)} 份结果\n")
    print(f"{'文件':<40}{'通过率':>10}{'recall@5':>12}")
    for f in files[-20:]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        s = d.get("summary") or {}
        if not s:
            continue
        print(f"{f.name:<40}{s.get('pass_rate', 0):>10.1%}{s.get('retrieval', {}).get('recall@5', 0):>12.1%}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P7 问答评测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一次评测")
    r.add_argument("--category", choices=["normal", "boundary", "adversarial"])
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--tag", default="run")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="四组对照实验")
    c.add_argument("--only", nargs="*", help="只跑指定实验名")
    c.set_defaults(func=cmd_compare)

    rp = sub.add_parser("report", help="列出历史结果")
    rp.set_defaults(func=cmd_report)

    st = sub.add_parser("stability", help="稳定性实验：同配置跑 N 遍")
    st.add_argument("--repeat", type=int, default=3)
    st.add_argument("--limit", type=int, default=40)
    st.set_defaults(func=cmd_stability)

    args = ap.parse_args()

    from app.core.platform import assert_event_loop_compatible

    assert_event_loop_compatible(where="eval_qa CLI")
    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(args.func(args)) or 0
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
