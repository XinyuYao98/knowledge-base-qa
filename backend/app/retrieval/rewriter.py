"""按需查询改写（架构 D13）。

## 要解决的问题

多轮追问时，第二句往往不完整：

    第1轮：一线城市住宿标准是多少？
    第2轮：那它的上限呢？          ← 孤立看这句，检索必失败

「它」没有指向，「上限」在文档里可能根本不存在（文档写的是「不超过 480 元」）。
向量召回拿到的是一个语义空壳，BM25 也召不回东西。

改写后：「一线城市住宿标准的上限是多少」—— 能正常检索。

## 为什么是「按需」而非「每轮都改」

| 方案 | 额外调用 | 延迟 | 问题 |
|---|---|---|---|
| 无条件改写 | 100% | +300-800ms × 每轮 | 首轮直白提问时纯浪费 |
| **按需改写** | 约 30% | 命中时 +300-800ms | 需要判断 |
| 不改写 | 0 | 0 | 多轮追问基本废掉 |

「每轮都有指代词」不成立 —— 用户直白提问时（「采购审批权限是什么」），
改写没有任何收益。

## 真正的风险：改错了你看不出来

改写偏离原意后，**后面的检索和生成都建立在错误的问题上**，
而用户看到的是一段自信的错误答案。

所以三条防线：

1. **触发保守**：正则命中指代词 **且** 轮次 > 1
2. **提示词设「原样输出」出口**：问题本身完整时不改
3. **结果入日志**：原文、改写文、是否触发、是否降级，全部记下来
   —— 出问题时能立刻判断是「没触发」还是「改错了」
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.models import ChatClient, ModelServiceError

log = get_logger("rewriter")

#: 指代词清单（架构 D13）。
#:
#: 注意 `该` 与 `此` 单独出现时误判率高（「该文档」「此方案」常是完整表达），
#: 所以要求它们后面**不跟**具体名词才算指代 —— 但正则做不到这层语义判断，
#: 这里选择宁可多触发一次（改写器有「原样输出」出口），也不要漏掉真指代。
PRONOUN_PATTERN = re.compile(
    r"(它|他们|她们|它们|这个|那个|这些|那些|上述|前述|前者|后者"
    r"|该[^文项条份]|此[^外前]|这几个|那几[个条]|刚才说|刚说的|前面说|上面说)"
)

#: 改写提示词版本，写入 query_logs 便于追溯
REWRITE_PROMPT_VERSION = "v1.0.0"

SYSTEM_PROMPT = """你是一个查询改写器。把用户的最新问题改写成一个不依赖上下文的完整问句。

规则：
1. 只补全指代与省略，不改变问题意图
2. 不引入对话中没有的信息
3. 输出改写后的问题本身，不要加任何解释、不要加引号
4. 若最新问题本身已完整，原样输出

对话历史：
{history}

最新问题：{question}"""


@dataclass(slots=True)
class RewriteResult:
    """改写结果。"""

    question: str                 # 最终用于检索的问题
    original: str                 # 用户原问题
    rewritten: bool               # 是否真的改了（文本不同）
    triggered: bool               # 是否触发改写（命中指代词且轮次>1）
    degraded: str = ""            # 降级原因，空表示正常
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "question": self.question,
            "rewritten": self.rewritten,
            "triggered": self.triggered,
            "degraded": self.degraded,
            "duration_ms": round(self.duration_ms, 1),
            "prompt_version": REWRITE_PROMPT_VERSION,
        }


def should_rewrite(question: str, turn_count: int) -> bool:
    """判断是否触发改写。

    两个条件必须同时满足：
    - 命中指代词
    - 轮次 > 1（首轮没有历史可指代）
    """
    if turn_count <= 1:
        return False
    return bool(PRONOUN_PATTERN.search(question))


def _format_history(history: list[dict[str, str]], max_turns: int = 4) -> str:
    """只取最近 N 轮。太长会让改写器分心，也浪费 token。"""
    if not history:
        return "（无）"
    recent = history[-max_turns * 2 :]
    lines = []
    for m in recent:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "").strip()
        if len(content) > 200:
            content = content[:200] + "…"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _clean(text: str) -> str:
    """清理模型输出。

    常见脏输出：引号包裹、前缀「改写后：」、多余换行。
    """
    t = text.strip()
    # 去掉「改写后：」这类前缀
    t = re.sub(r"^(改写后|改写结果|rewritten)\s*[:：]\s*", "", t, flags=re.IGNORECASE)
    # 去掉整体包裹的引号
    if len(t) >= 2 and t[0] in "\"'“「" and t[-1] in "\"'”」":
        t = t[1:-1]
    return t.strip()


async def rewrite(
    question: str,
    history: list[dict[str, str]] | None = None,
    *,
    turn_count: int = 0,
    client: ChatClient | None = None,
) -> RewriteResult:
    """按需改写。**永不抛异常** —— 失败就降级用原问题。

    为什么吞掉异常：改写是「锦上添花」，不是主链路。
    它挂了不该让整个问答失败。
    """
    original = question.strip()
    turn_count = turn_count or (len(history or []) // 2 + 1)

    if not should_rewrite(original, turn_count):
        return RewriteResult(
            question=original, original=original,
            rewritten=False, triggered=False,
        )

    t0 = time.perf_counter()
    client = client or ChatClient()

    try:
        system = SYSTEM_PROMPT.format(
            history=_format_history(history or []),
            question=original,
        )
        result = await client.complete(
            system="你是一个精确的查询改写器，只输出改写后的问句。",
            user=system,
            max_tokens=settings.REWRITE_MAX_TOKENS,
        )
        text = _clean(result["content"])
    except ModelServiceError as e:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning("rewrite_llm_failed", err=e.message[:100])
        return RewriteResult(
            question=original, original=original, rewritten=False,
            triggered=True, degraded="no_rewrite", duration_ms=elapsed,
        )
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning("rewrite_unexpected", err=type(e).__name__)
        return RewriteResult(
            question=original, original=original, rewritten=False,
            triggered=True, degraded="no_rewrite", duration_ms=elapsed,
        )

    elapsed = (time.perf_counter() - t0) * 1000

    # 防「改写器返回空」或「返回了一整段解释」
    if not text or len(text) > len(original) * 4 + 80:
        log.info("rewrite_rejected", reason="empty_or_too_long", out=text[:60])
        return RewriteResult(
            question=original, original=original, rewritten=False,
            triggered=True, degraded="no_rewrite", duration_ms=elapsed,
        )

    changed = text.strip() != original
    log.info(
        "rewrite_done",
        original=original[:40],
        rewritten=text[:40],
        changed=changed,
        ms=round(elapsed, 1),
    )

    return RewriteResult(
        question=text,
        original=original,
        rewritten=changed,
        triggered=True,
        duration_ms=elapsed,
    )
