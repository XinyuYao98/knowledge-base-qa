"""项目根定位。

## 为什么不用 `Path(__file__).parents[N]`

P6/P7 累计四次栽在这个 off-by-one 上：
`app/eval/runner.py` 的 `parents[2]` 和 `parents[3]` 差一层，
算错时不会报错 —— 只会**静默写到别的目录**（有一次写到了项目根的上两级）。

## 正确做法：向上找标记文件

从当前文件所在目录往上走，遇到的第一个含 `.git` 或 `pyproject.toml`
的目录就是项目根。这样新增/移动文件都不用改这个逻辑。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path


@cache
def project_root() -> Path:
    """向上查找**仓库根**（含 .git 的目录）。

    注意：`pyproject.toml` 在 `backend/` 里，不能作为根标记 ——
    否则会定位到 backend 而不是项目根。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    # 兜底：含 backend/ 与 README.md 的目录
    for parent in here.parents:
        if (parent / "backend").is_dir() and (parent / "README.md").is_file():
            return parent
    return Path.cwd()


def data_path(*parts: str) -> Path:
    return project_root().joinpath(*parts)
