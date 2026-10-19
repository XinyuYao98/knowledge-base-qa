"""引用提取与校验（纯函数）。

**为什么单独一个模块**：这是「答案可追溯」的核心，
必须能脱离数据库、脱离模型单独测试。

## 设计要点

1. **只认 `[数字]` 形式**，不认 `【1】`、`（1）`、`^1`
2. 提取后校验编号是否在有效范围内
3. 输出结构里保留原文位置，便于前端高亮
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 引用标记。支持三种写法，均实测出现过：
#:   [1]
#:   [1][2]
#:   [1-5]      ← 区间，模型很爱用
#:   [1,2]      ← 逗号分隔
#:
#: 只匹配方括号内由数字与分隔符组成的内容，避免误吞 [2024] 这类年份？
#: 不 —— 年份仍会被匹配，由 MAX_SANE_CITATION 上限过滤掉。
CITATION_RE = re.compile(r"\[(\d{1,3}(?:\s*[-–,，、]\s*\d{1,3})*)\]")

#: 允许的引用范围上限，防御模型编出 [999]
MAX_SANE_CITATION = 50


@dataclass(slots=True)
class Citation:
    """一处引用。"""

    index: int          # 引用编号（从 1 开始）
    start: int          # 在原文中的起始位置
    end: int            # 结束位置（不含）


@dataclass(slots=True)
class CitationCheck:
    """校验结果。"""

    cited: list[int]              # 实际被引用的编号（去重、升序）
    invalid: list[int]            # 超出有效范围的编号
    unused: list[int]             # 给了资料但没被引用
    is_valid: bool                # 全部编号合法且有至少一处引用

    def to_dict(self) -> dict:
        return {
            "cited": self.cited,
            "invalid": self.invalid,
            "unused": self.unused,
            "is_valid": self.is_valid,
        }


def _expand_group(body: str) -> list[int]:
    """把 `[1-5]` / `[1,2]` 这样的分组展开成编号列表。"""
    out: list[int] = []
    # 拆成若干段：可能是「单数字」或「a-b」区间
    for part in re.split(r"[,，、]", body):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,3})\s*[-–]\s*(\d{1,3})", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            # 区间过宽时截断，防止 [1-999] 生成巨量编号
            if hi - lo > MAX_SANE_CITATION:
                hi = lo + MAX_SANE_CITATION
            out.extend(range(lo, hi + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def extract_citations(text: str) -> list[Citation]:
    """提取文本中所有引用标记，按出现顺序。

    一个 `[1-3]` 会展开成三条 Citation，位置相同 —— 便于前端按位置高亮。
    """
    out: list[Citation] = []
    for m in CITATION_RE.finditer(text):
        for idx in _expand_group(m.group(1)):
            if 1 <= idx <= MAX_SANE_CITATION:
                out.append(Citation(index=idx, start=m.start(), end=m.end()))
    return out


#: 拒答措辞。命中任意一个时，允许「无引用」。
#:
#: 理由：模型说「资料中未提及」时不该硬套引用编号 ——
#: 没有结论可归属，强行标注反而是编造。所以拒答场景下引用为空是合法的。
REFUSAL_MARKERS = ("资料中未提及", "未提及", "资料中没有", "无法从资料", "资料未涉及")


def is_refusal(text: str) -> bool:
    return any(m in text for m in REFUSAL_MARKERS)


def check_citations(text: str, available: int) -> CitationCheck:
    """校验引用。

    `available`：本次上下文提供给了几段资料（编号 1..available）。

    判定规则：
    - 有越界编号 → 不通过（模型编造了不存在的来源）
    - 无任何引用，但属于拒答 → **通过**（无需归属）
    - 无任何引用，且不是拒答 → 不通过（结论没有来源）
    """
    found = extract_citations(text)
    cited = sorted({c.index for c in found})
    invalid = [i for i in cited if i > available]

    available_set = set(range(1, available + 1))
    unused = sorted(available_set - set(cited))

    refusal = is_refusal(text)
    has_citation = bool(cited)

    is_valid = (not invalid) and (has_citation or refusal)

    return CitationCheck(
        cited=cited,
        invalid=invalid,
        unused=unused,
        is_valid=is_valid,
    )


def strip_citations(text: str) -> str:
    """去掉引用标记，用于生成纯文本摘要。"""
    return CITATION_RE.sub("", text).strip()


def format_context(items: list[dict], max_chars: int = 6000) -> tuple[str, list[dict]]:
    """把检索结果拼成带编号的上下文。

    返回 (上下文文本, 实际采用的条目列表)。
    超长时截断 —— 但**不截断单条中间**，宁可少放一条。
    """
    lines: list[str] = []
    used: list[dict] = []
    total = 0

    for i, item in enumerate(items, start=1):
        label = item.get("label") or item.get("source") or f"资料{i}"
        text = (item.get("text") or "").strip()
        if not text:
            continue
        block = f"[{i}] {label}\n{text}"
        if total + len(block) > max_chars and used:
            break
        lines.append(block)
        used.append({**item, "citation_index": i})
        total += len(block)

    return "\n\n".join(lines), used
