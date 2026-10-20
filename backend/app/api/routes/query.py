"""问答路由。

    POST /api/v1/kbs/{kb_id}/query          同步问答
    POST /api/v1/kbs/{kb_id}/query/stream   SSE 流式

## 为什么有同步和流式两个端点

- **流式**是给聊天界面用的（首 token 快，体验好）
- **同步**是给脚本、测试、非浏览器客户端用的（好调试，返回完整 JSON）

两个端点走同一套编排（`Answerer`），区别只在返回形式。

## 索引期间禁问

知识库还有文档在处理时，检索结果不完整 —— 回答会漏内容，而用户不知道。
所以有未就绪文档时返回 409 `kb_busy`，而不是给一个「看起来正常」的残缺答案。

**这是一个有争议的选择**：有人会觉得「先给能答的部分」更好。
但在 RAG 里，**残缺召回 + 自信回答 = 误导**，比明确说「正在处理」更糟。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import CurrentUserId, DBSession
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.services.qa import Answerer

log = get_logger("api.query")

router = APIRouter(prefix="/kbs/{kb_id}/query", tags=["query"])

#: 处理中的状态集合（未就绪）
_BUSY_STATUSES = ("pending", "parsing", "chunking", "embedding", "waiting_model")


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


async def _assert_kb_ready(session: Any, kb_id: uuid.UUID) -> None:
    """知识库必须存在且有 ready 文档。"""
    row = await session.execute(
        text("SELECT id FROM knowledge_bases WHERE id = :id"), {"id": kb_id}
    )
    if row.first() is None:
        raise AppError("kb_not_found", "知识库不存在", http_status=404)

    busy = await session.execute(
        text(
            "SELECT count(*) FROM documents "
            " WHERE kb_id = :kb AND status = ANY(:st)"
        ),
        {"kb": kb_id, "st": list(_BUSY_STATUSES)},
    )
    if int(busy.scalar_one()) > 0:
        raise AppError(
            "kb_busy",
            "有文档正在索引中，请稍后再问",
            http_status=409,
        )

    ready = await session.execute(
        text("SELECT count(*) FROM documents WHERE kb_id = :kb AND status = 'ready'"),
        {"kb": kb_id},
    )
    if int(ready.scalar_one()) == 0:
        raise AppError("kb_empty", "该知识库还没有可检索的文档", http_status=409)


@router.post("")
async def query(
    kb_id: uuid.UUID,
    body: QueryRequest,
    session: DBSession,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """同步问答。返回完整结果（含引用与耗时）。"""
    await _assert_kb_ready(session, kb_id)

    answerer = Answerer()
    result = await answerer.answer(
        kb_id,
        body.question,
        user_id=user_id,
        history=body.history,
        turn_count=len(body.history) // 2 + 1,
    )

    # 埋点：异步写 query_logs（失败不影响响应）
    await _log_query(session, kb_id, user_id, result)

    return result.to_dict()


@router.post("/stream")
async def query_stream(
    kb_id: uuid.UUID,
    body: QueryRequest,
    session: DBSession,
    user_id: CurrentUserId,
) -> StreamingResponse:
    """SSE 流式问答。

    事件：`meta` → `delta`* → `done`（或 `error`）。
    """
    await _assert_kb_ready(session, kb_id)

    answerer = Answerer()

    async def event_source():
        try:
            async for evt in answerer.stream_answer(
                kb_id,
                body.question,
                user_id=user_id,
                history=body.history,
                turn_count=len(body.history) // 2 + 1,
            ):
                yield _sse(evt["event"], evt["data"])
        except Exception as e:
            log.exception("stream_failed", kb_id=str(kb_id))
            yield _sse("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            # SSE 结束标记：某些客户端靠这个判断流已关闭
            yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 Nginx 的缓冲，否则流式会被攒成一个大包（部署时会用到）
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _log_query(session: Any, kb_id: uuid.UUID, user_id: str, result: Any) -> None:
    """写 query_logs。**失败不影响响应**。

    ## 字段与表结构对齐（实测踩到）

    第一版按「question / answer / rewritten_question」写，但真实表里没有这些列 ——
    只有 `question_chars`、`retrieve_hits`、`citation_valid` 这类**统计量**。

    这个设计值得留意：**表结构刻意不存原文与答案**，只存可聚合的指标。
    所以「改写前后对比」这类排查不靠这张表，要看别的地方（日志或后续新增字段）。

    P7 评测需要的原始问答对，应该单独建表，而不是往这里塞正文。
    """
    try:
        rw = result.retrieval.rewrite if result.retrieval else None
        chk = result.citation_check or {}
        await session.execute(
            text(
                """
                INSERT INTO query_logs
                    (user_id, kb_id, trace_id, question_chars, retrieve_hits,
                     top_score, latency_ms, citation_valid, repair_attempted,
                     grounded, rewrite_triggered, status, prompt_version, embed_model)
                VALUES
                    (:uid, :kb, :tid, :qc, :rh,
                     :ts, :ms, :cv, :ra,
                     :gd, :rw, :st, :pv, :em)
                """
            ),
            {
                "uid": uuid.UUID(str(user_id)),
                "kb": kb_id,
                "tid": uuid.uuid4().hex[:32],
                "qc": len(result.retrieval.original_question) if result.retrieval else 0,
                "rh": result.retrieval.fused_count if result.retrieval else 0,
                "ts": _top_score(result),
                "ms": int(result.duration_ms),
                "cv": bool(chk.get("ok", True)),
                "ra": bool(result.repaired),
                "gd": not result.caveat,
                "rw": bool(rw.triggered) if rw else False,
                "st": "ok" if not result.degraded else "degraded",
                "pv": result.prompt_version,
                "em": settings.EMBED_MODEL,
            },
        )
    except Exception as e:
        # 埋点失败绝不能影响用户拿到答案
        log.warning("query_log_failed", err=type(e).__name__)


def _top_score(result: Any) -> float | None:
    """取候选里最高的 rerank 分或 RRF 分，用于后续分析「低分查询」。"""
    if not result.retrieval or not result.retrieval.candidates:
        return None
    cands = result.retrieval.candidates
    return round(max(c.rrf_score for c in cands), 6)
