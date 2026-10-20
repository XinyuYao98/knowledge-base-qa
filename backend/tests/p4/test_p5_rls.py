"""P5 核心验证：注册如何绕过 RLS。

## 这是 P5 最需要证明的一件事

P4 交付时发现：`users` 表受 RLS 保护，应用角色**连注册都插不进去**。
P5 的解法是加 `auth_register` SECURITY DEFINER 函数（Q1 选 A）。

**这个测试就是证明「解法真的有效」，且「没有滥用提权」。**

三段断言：

1. 应用角色直接 INSERT → **必须被拒**（证明 RLS 还在工作）
2. 通过 auth_register → **必须成功**（证明函数有效）
3. 重复注册 → **返回 None 而不是抛异常**（证明业务层能优雅处理）

如果第 1 条失败，说明 RLS 被绕过或关掉了 —— 那是严重倒退。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.session import get_session_factory

pytestmark = pytest.mark.db

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=2$ZmFrZXNhbHQ$ZmFrZWhhc2hmYWtlaGFzaA"


def _admin():
    return psycopg.connect(settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=5)


@pytest.fixture
async def username() -> AsyncIterator[str]:
    name = f"rls_{uuid.uuid4().hex[:10]}"
    yield name
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (name,))


class TestRLSBypass:
    async def test_direct_insert_blocked(self, username: str) -> None:
        """应用角色直接 INSERT users 必须被 RLS 拦住。

        这条失败 = 隔离失效，比任何功能 bug 都严重。
        """
        factory = get_session_factory()
        async with factory() as s, s.begin():
            with pytest.raises(Exception, match="row-level security"):
                await s.execute(
                    text("INSERT INTO users (username, password_hash) VALUES (:u, 'x')"),
                    {"u": username},
                )

    async def test_auth_register_succeeds(self, username: str) -> None:
        """通过 SECURITY DEFINER 函数可以插入。"""
        factory = get_session_factory()
        async with factory() as s, s.begin():
            r = await s.execute(
                text("SELECT auth_register(:u, :p) AS uid"),
                {"u": username, "p": FAKE_HASH},
            )
            uid = r.scalar_one()

        assert uid is not None, "auth_register 返回了 None，注册失败"

        with _admin() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT username, password_hash, is_active FROM users WHERE id = %s",
                (uid,),
            )
            row = cur.fetchone()

        assert row is not None
        assert row[0] == username
        assert row[1] == FAKE_HASH
        assert row[2] is True

    async def test_duplicate_returns_none_not_error(self, username: str) -> None:
        """重复注册返回 None，而不是抛唯一冲突异常。

        这样业务层能返回 409「用户名已存在」，
        而不是让用户看到一个 500。
        """
        factory = get_session_factory()

        async with factory() as s, s.begin():
            r = await s.execute(
                text("SELECT auth_register(:u, :p) AS uid"),
                {"u": username, "p": FAKE_HASH},
            )
            first = r.scalar_one()
        assert first is not None

        async with factory() as s, s.begin():
            r = await s.execute(
                text("SELECT auth_register(:u, :p) AS uid"),
                {"u": username, "p": FAKE_HASH},
            )
            second = r.scalar_one()
        assert second is None, "重复注册应返回 None"

    async def test_register_does_not_leak_other_users(self, username: str) -> None:
        """auth_register 只能插入，不能看到别人的数据。

        SECURITY DEFINER 只提升「插入 users」这一件事的权限，
        调用方仍然受 RLS 约束读不到其他用户。
        """
        factory = get_session_factory()

        # 先建两个用户
        uids = []
        names = [f"{username}_a", f"{username}_b"]
        for n in names:
            async with factory() as s, s.begin():
                r = await s.execute(
                    text("SELECT auth_register(:u, :p) AS uid"), {"u": n, "p": FAKE_HASH}
                )
                uids.append(r.scalar_one())

        try:
            # 不加 RLS 上下文时，users 表应该查不到任何行
            async with factory() as s, s.begin():
                r = await s.execute(text("SELECT count(*) FROM users"))
                visible = r.scalar_one()
            assert visible == 0, f"无身份时竟能看到 {visible} 个用户"

            # 设置身份后只能看到自己
            async with factory() as s, s.begin():
                await s.execute(
                    text("SELECT set_config('app.user_id', :u, true)"),
                    {"u": str(uids[0])},
                )
                r = await s.execute(text("SELECT id FROM users"))
                rows = r.fetchall()
            assert len(rows) == 1
            assert str(rows[0][0]) == str(uids[0])
        finally:
            with _admin() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE username LIKE %s", (f"{username}%",))
