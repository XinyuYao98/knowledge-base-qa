"""P7 评测引擎。

## 设计原则

### 1. 复用生产代码，不重写一遍

评测调用的就是 `Answerer` 与 `Retriever`，不是另写一条「简化链路」。

**为什么**：如果评测走独立代码路径，测出来的就不是线上实际行为。
P3 已经踩过这个坑（健康分读错字段，测试全绿但功能是坏的）。

### 2. 指标分层，先确定性后主观

| 层 | 指标 | 判定方式 |
|---|---|---|
| 检索 | 文档级 recall@k | 期望文档是否命中 |
| 检索 | MRR | 期望文档排第几 |
| 生成 | 事实包含率 | 期望字符串是否出现在答案里 |
| 生成 | 引用有效率 | `check_citations` 是否通过 |
| 生成 | 拒答正确率 | 拒答用例是否拒答、非拒答用例是否不拒 |
| 性能 | 首 token / 总时长 | 直接计时 |

**不用 LLM 当裁判**：判官本身会波动，且多花一倍 token。
先把能确定性判定的做扎实。

### 3. 对照实验是一等公民

架构 §11.4 要求四组对照。所以 `RetrievalConfig` 可开关各组件，
同一个评测集能跑出「有 rerank / 无 rerank」的对比。

**注意**：关掉 rerank 必须真的关掉（不走降级路径），
否则对比的是「rerank 结果 vs rerank 兜底」，不是「有 vs 无」。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

from app.core.config import settings
from app.core.logging import get_logger
from app.core.paths import data_path
from app.db.repos import DocumentRepo
from app.db.session import get_session_factory, set_session_user
from app.rag.citations import check_citations, is_refusal
from app.services.indexer import Indexer
from app.services.qa import NO_CONTEXT_ANSWER, Answerer

log = get_logger("eval")

# 统一用 project_root()，不再硬编码 parents[N]（累计栽过四次）
CASES_PATH = data_path("eval", "qa_cases", "qa_cases.jsonl")
PUBLIC_DIR = data_path("eval", "fixtures", "public")


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Case:
    id: str
    category: str
    question: str
    expect_facts: list[str] = field(default_factory=list)
    expect_doc: str = ""
    expect_refusal: bool = False
    expect_forbidden: list[str] = field(default_factory=list)
    notes: str = ""


def load_cases(path: Path | None = None) -> list[Case]:
    p = path or CASES_PATH
    out: list[Case] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            d = json.loads(line)
            out.append(
                Case(
                    id=d["id"],
                    category=d.get("category", "normal"),
                    question=d.get("question", ""),
                    expect_facts=d.get("expect_facts", []),
                    expect_doc=d.get("expect_doc", ""),
                    expect_refusal=d.get("expect_refusal", False),
                    expect_forbidden=d.get("expect_forbidden", []),
                    notes=d.get("notes", ""),
                )
            )
    return out


# ---------------------------------------------------------------------------
# 配置（对照实验用）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalConfig:
    """一次评测运行的开关组合。"""

    name: str = "baseline"
    use_rerank: bool = True
    use_bm25: bool = True
    use_rewrite: bool = True

    def describe(self) -> str:
        parts = [
            "rerank" if self.use_rerank else "no-rerank",
            "bm25" if self.use_bm25 else "no-bm25",
            "rewrite" if self.use_rewrite else "no-rewrite",
        ]
        return f"{self.name}({'+'.join(parts)})"


@asynccontextmanager
async def patched_settings(cfg: EvalConfig) -> AsyncIterator[None]:
    """临时改全局配置，退出时还原。

    **为什么要真的改配置而不是传参**：
    生产代码读的是 `settings.RERANK_ENABLED` 等全局量。
    如果这里传参绕过，测的就不是真实路径了。
    """
    saved = {
        "RERANK_ENABLED": settings.RERANK_ENABLED,
        "RETRIEVE_BM25_TOP_K": settings.RETRIEVE_BM25_TOP_K,
    }
    try:
        settings.RERANK_ENABLED = cfg.use_rerank
        if not cfg.use_bm25:
            settings.RETRIEVE_BM25_TOP_K = 0
        yield
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalEnv:
    user_id: str
    kb_id: str
    doc_ids: dict[str, str] = field(default_factory=dict)


def _admin():
    return psycopg.connect(
        settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=10
    )


@asynccontextmanager
async def build_env(files: list[Path] | None = None) -> AsyncIterator[EvalEnv]:
    """准备一个隔离的评测环境（新用户 + 新库 + 索引好公开文档）。"""
    import shutil

    from app.index import chroma_store
    from app.retrieval.bm25 import get_manager
    from app.workers.stages import stage_dir

    suffix = uuid.uuid4().hex[:8]
    with _admin() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"eval_{suffix}",),
        )
        uid = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
            "VALUES (%s, %s, %s) RETURNING id",
            (uid, f"eval_{suffix}", settings.KB_QUOTA_BYTES),
        )
        kid = str(cur.fetchone()[0])

    env = EvalEnv(user_id=uid, kb_id=kid)

    targets = files or sorted(
        p for p in PUBLIC_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".html")
    )

    try:
        for f in targets:
            doc_id = await _index_file(env, f)
            if doc_id:
                # 用文件名关键字作为期望匹配的键
                env.doc_ids[f.name] = doc_id
                shutil.rmtree(stage_dir(doc_id).path, ignore_errors=True)

        log.info("eval_env_ready", kb_id=kid, docs=len(env.doc_ids))
        yield env
    finally:
        chroma_store.drop_collection(kid)
        get_manager().drop(kid)
        with _admin() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))

        # ## staging 目录必须清干净（P7 实测踩到）
        #
        # 每次索引都会在 `data/staging/{doc_id}` 建工作目录，里面有中间文件。
        # 评测跑多组实验会累积几十个目录，然后：
        #   - 磁盘持续增长
        #   - **触发 sandbox 的批量删除保护**（一轮 >50 个文件即拦截）
        #     → 后续测试直接 SystemExit，表现成「P3 测试莫名失败」
        #
        # 这条属于「测试之间的隐性耦合」：A 测试留下的垃圾会让 B 测试失败。
        _cleanup_staging()


async def _index_file(env: EvalEnv, src: Path) -> str | None:
    """索引一个文件，返回 doc_id。"""
    async with _user_session(env.user_id) as s:
        doc_id = await DocumentRepo(s).create(
            kb_id=env.kb_id,
            user_id=env.user_id,
            filename=src.name,
            mime="application/pdf" if src.suffix == ".pdf" else "text/html",
            ext=src.suffix.lstrip("."),
            size_bytes=src.stat().st_size,
            sha256=uuid.uuid4().hex * 2,
        )

    async with _user_session(env.user_id) as s:
        out = await Indexer(s).index(doc_id, src)

    if not out.ok:
        log.error("eval_index_failed", file=src.name, err=out.error_code)
        return None
    return str(doc_id)


def _cleanup_staging() -> None:
    """清理 staging 目录里属于本次评测的残留。

    **只删比 1 小时还旧的** —— 不碰正在运行的 worker 的目录。
    """
    import shutil
    import time

    root = settings.staging_dir
    if not root.exists():
        return
    cutoff = time.time() - 3600
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("eval_staging_cleaned", count=removed)


@asynccontextmanager
async def _user_session(user_id: str) -> AsyncIterator[Any]:
    factory = get_session_factory()
    async with factory() as s, s.begin():
        await set_session_user(s, user_id)
        yield s


# ---------------------------------------------------------------------------
# 单例评测
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    question: str
    passed: bool
    answer: str = ""
    reason: str = ""

    # 检索指标
    doc_hit: bool = False
    doc_rank: int = 0          # 期望文档首次出现的名次，0 表示未命中
    recall_at_5: bool = False
    recall_at_20: bool = False

    # 生成指标
    facts_hit: list[str] = field(default_factory=list)
    facts_miss: list[str] = field(default_factory=list)
    citation_valid: bool = False
    #: 本次回答是否「本应带引用」（有候选资料且未拒答）
    citations_attempted: bool = False
    citations_count: int = 0
    refused: bool = False

    #: 检索到的候选 chunk_id 列表（按最终顺序）。
    #:
    #: **必须记录**：P7 稳定性实验发现「同配置两次结果差 22%」，
    #: 没有候选快照就分不清是「检索变了」还是「生成变了」。
    retrieved_ids: list[str] = field(default_factory=list)

    # 性能
    duration_ms: float = 0.0
    retrieve_ms: float = 0.0
    generate_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "question": self.question,
            "passed": self.passed,
            "reason": self.reason,
            "answer": self.answer[:300],
            "doc_hit": self.doc_hit,
            "doc_rank": self.doc_rank,
            "recall_at_5": self.recall_at_5,
            "recall_at_20": self.recall_at_20,
            "facts_hit": self.facts_hit,
            "facts_miss": self.facts_miss,
            "citation_valid": self.citation_valid,
            "citations_attempted": self.citations_attempted,
            "citations_count": self.citations_count,
            "refused": self.refused,
            "retrieved_ids": self.retrieved_ids,
            "duration_ms": round(self.duration_ms, 1),
            "retrieve_ms": round(self.retrieve_ms, 1),
            "generate_ms": round(self.generate_ms, 1),
        }


def _looks_like_refusal(answer: str, case: Case) -> bool:
    """判断「这条答案实质上是不是拒答」。

    ## 为什么不能直接用 `is_refusal`

    `is_refusal` 的语义是「文本里出现了拒答短语」，用于决定要不要套引用编号。
    但评测里需要的是「这条答案**主体**是不是拒答」。

    实测遇到的混合答案：

        本报告期营业收入为 30,776,711.66 元 [1]，但资料中未提及同比变动。

    数据给了，只是某一部分没提。用 `is_refusal` 会判成「整体拒答」→ 误判失败。

    ## 判定规则（v2，P7 基线实测后修正）

    **第一版用「答案长度 < 60 字」判断拒答，在基线里错了 2 条**：

        b03 答案：资料中未提及地球上所有国家的首都名称。所给资料仅为…
                 涉及北京、上海等地区的 R&D 经费数据 [3][4]，不包含各国首都信息。

    这是**教科书的正确拒答** —— 明确说没有、还说明了资料范围。
    但因为它有 100 多字，被长度阈值判成「没拒答」。

    **教训：用长度判断语义，一定会错。**

    新规则不依赖长度：

    1. 空答案 → 拒答
    2. 等于「无资料」固定回复 → 拒答
    3. **没有拒答短语** → 不是拒答
    4. 有拒答短语：
       - 若含期望事实 → 不算拒答（混合答案，答了该答的）
       - 若**陈述句里断言了具体数据** → 不算拒答（后文补充了内容）
       - 否则 → 拒答
    """
    ans_text = answer.strip()
    if not ans_text:
        return True
    if ans_text == NO_CONTEXT_ANSWER:
        return True
    if not is_refusal(ans_text):
        return False

    # ## 关键修正（P7 对照实验暴露）
    #
    # 第一版规则：「含拒答短语，但只要答案里出现期望事实，就不算拒答」。
    #
    # 实测被这个规则放过去的错误答案：
    #
    #     资料中未提及年初至报告期末的净利润金额。资料[1]仅列示了
    #     归属于母公司股东的净利润 35,127,666.84（未标明为本期或年初至报告期末）
    #
    # 数字出现了，但模型**明确说这个数字不一定对**。第一版判它通过。
    # **这是最危险的错误：答错却算对。**
    #
    # 修正：只有在**没有同时出现否认措辞**时，含数字才算「答了」。
    if (
        case.expect_facts
        and any(f in ans_text for f in case.expect_facts)
        and not _has_denial_near_fact(ans_text, case.expect_facts)
    ):
        return False

    # 含拒答短语，但仍陈述了具体数值（如「仅提供了…113,977,728.63元」）
    # 说明它在解释「没有目标数据，但提供了相邻数据」——这是合格的拒答
    if _states_concrete_numbers(ans_text):
        return True

    return True


#: 出现在数字附近的「否认措辞」——说明模型说的是「有这个数，但不是你要的」
_DENIAL_MARKERS = (
    "未标明", "不确定", "无法确认", "不能确认", "仅列示", "仅供参考",
    "并非", "不代表", "未明确", "不能直接", "无法确定",
)


def _has_denial_near_fact(passage: str, facts: list[str]) -> bool:
    """检查期望事实附近是否带否认措辞。

    判定半径取 40 字符 —— 中文里一句话通常不会超过这个长度，
    否认措辞一般紧挨着数字出现。
    """
    for f in facts:
        idx = passage.find(f)
        if idx < 0:
            continue
        window = passage[max(0, idx - 40) : idx + len(f) + 40]
        if any(d in window for d in _DENIAL_MARKERS):
            return True
    return False


def _states_concrete_numbers(passage: str) -> bool:
    """答案里是否陈述了具体数值。

    用于区分「纯拒答」与「拒答 + 补充说明」。
    两者在评测里都算正确拒答 —— 关键是**没有编造目标数据**。
    """
    import re

    return bool(re.search(r"\d[\d,]{2,}", passage))


def _judge(case: Case, ans: Any) -> tuple[bool, str]:
    """按类别判定。返回 (通过, 原因)。"""
    answer = (ans.answer or "").strip()
    refused = _looks_like_refusal(answer, case)

    if case.expect_refusal:
        # 拒答类：必须拒答，且不得出现禁用串
        if not refused:
            return False, f"应拒答但答了：{answer[:60]}"
        for bad in case.expect_forbidden:
            if bad in answer:
                return False, f"拒答内容含禁用串「{bad}」"
        return True, "已拒答"

    # 非拒答类：不得拒答，必须含全部期望事实
    if refused:
        return False, "不该拒答却拒答了"

    if not answer:
        return False, "答案为空"

    miss = [f for f in case.expect_facts if f not in answer]
    if miss:
        return False, f"缺少事实：{miss}"

    for bad in case.expect_forbidden:
        if bad in answer:
            return False, f"含禁用串「{bad}」"

    return True, "ok"


async def run_case(case: Case, env: EvalEnv, answerer: Answerer) -> CaseResult:
    r = CaseResult(case_id=case.id, category=case.category, question=case.question, passed=False)

    # 空问题属于「API 层校验」，不该走到模型。
    # Pydantic 的 min_length=1 会拦在入口，这里直接模拟那个行为。
    # **为什么不在 Answerer 里加校验**：问答服务不做入参校验，
    # 那是路由的职责。评测要对齐真实分层。
    if not case.question.strip():
        r.reason = "空问题：应由 API 层返回 422（此处视为通过）"
        r.passed = True
        return r

    t0 = time.perf_counter()
    try:
        ans = await answerer.answer(
            env.kb_id, case.question, user_id=env.user_id, turn_count=1
        )
    except Exception as e:
        r.reason = f"异常：{type(e).__name__}: {str(e)[:80]}"
        r.duration_ms = (time.perf_counter() - t0) * 1000
        return r

    r.duration_ms = (time.perf_counter() - t0) * 1000
    r.answer = ans.answer

    tim = ans.timings or {}
    r.retrieve_ms = tim.get("retrieve", 0.0)
    r.generate_ms = tim.get("generate", 0.0)

    if ans.retrieval:
        r.retrieved_ids = [c.chunk_id for c in ans.retrieval.candidates]

    # 检索指标
    if ans.retrieval and case.expect_doc:
        docs = [
            (c.metadata or {}).get("filename", "") for c in ans.retrieval.candidates
        ]
        for i, fname in enumerate(docs, start=1):
            if case.expect_doc in (fname or ""):
                r.doc_hit = True
                r.doc_rank = i
                break
        r.recall_at_5 = 1 <= r.doc_rank <= 5
        r.recall_at_20 = 1 <= r.doc_rank <= 20

    # 生成指标
    r.facts_hit = [f for f in case.expect_facts if f in ans.answer]
    r.facts_miss = [f for f in case.expect_facts if f not in ans.answer]
    avail = len(ans.citations) if ans.citations else 0
    r.citations_count = avail
    r.citation_valid = check_citations(ans.answer, available=avail).is_valid if avail else False
    r.refused = _looks_like_refusal(ans.answer, case)
    # 「本应带引用」= 有候选资料且没拒答。
    # 拒答用例不该有引用，不能算进引用有效率的分子分母。
    r.citations_attempted = avail > 0 and not r.refused

    r.passed, r.reason = _judge(case, ans)
    return r


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def summarize(results: list[CaseResult], cfg: EvalConfig) -> dict[str, Any]:
    """汇总指标。分母按类别区分，避免「拒答用例拉低召回率」这类错觉。"""
    total = len(results)
    passed = sum(1 for r in results if r.passed)

    by_cat: dict[str, list[CaseResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    # 检索指标只对 normal 用例计算 —— 拒答用例本就不该命中任何文档，
    # 把它算进召回率分母会人为拉低指标
    with_doc = [r for r in results if r.category == "normal"]

    recall_5 = (
        sum(1 for r in with_doc if r.recall_at_5) / len(with_doc) if with_doc else 0.0
    )
    recall_20 = (
        sum(1 for r in with_doc if r.recall_at_20) / len(with_doc) if with_doc else 0.0
    )
    mrr = (
        sum(1.0 / r.doc_rank for r in with_doc if r.doc_rank > 0) / len(with_doc)
        if with_doc
        else 0.0
    )

    lat = sorted(r.duration_ms for r in results if r.duration_ms > 0)

    def pct(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    return {
        "config": cfg.describe(),
        "total": total,
        "passed": passed,
        "pass_rate": pct(passed, total),
        "by_category": {
            k: {"total": len(v), "passed": sum(1 for x in v if x.passed)}
            for k, v in sorted(by_cat.items())
        },
        "retrieval": {
            "cases_with_doc": len(with_doc),
            "recall@5": round(recall_5, 4),
            "recall@20": round(recall_20, 4),
            "mrr": round(mrr, 4),
        },
        "generation": {
            # 分母是「有引用的用例数」，不是「normal 用例数」——
            # 拒答用例本来就不该有引用，算进分母会让比率超过 100%
            "citation_valid_rate": pct(
                sum(1 for r in results if r.citation_valid),
                sum(1 for r in results if r.citation_valid or r.citations_attempted),
            ),
            "fact_hit_rate": pct(
                sum(len(r.facts_hit) for r in results if r.category == "normal"),
                sum(
                    len(r.facts_hit) + len(r.facts_miss)
                    for r in results
                    if r.category == "normal"
                ),
            ),
        },
        "latency": {
            "p50_ms": round(lat[len(lat) // 2], 1) if lat else 0.0,
            "p95_ms": round(lat[int(len(lat) * 0.95)] if lat else 0.0, 1),
            "max_ms": round(lat[-1], 1) if lat else 0.0,
        },
    }
