"""解析层测试公共夹具。

**不需要模型、不需要数据库、不需要 Docker。**

素材来源：
- `eval/fixtures/synthetic/` 合成样例，每个只测一个刁钻点
- `eval/fixtures/public/` 真实公开文档，验证真实版式

素材缺失时自动跳过，不让测试因为环境问题挂掉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[3] / "eval" / "fixtures"
SYNTHETIC = FIXTURES / "synthetic"
PUBLIC = FIXTURES / "public"


def fixture_path(name: str, kind: str = "synthetic") -> Path:
    """取素材路径。不存在时跳过测试并说明原因。"""
    base = SYNTHETIC if kind == "synthetic" else PUBLIC
    path = base / name
    if not path.exists():
        pytest.skip(f"素材缺失：{path}")
    return path


@pytest.fixture
def synthetic_dir() -> Path:
    return SYNTHETIC


@pytest.fixture
def public_dir() -> Path:
    return PUBLIC
