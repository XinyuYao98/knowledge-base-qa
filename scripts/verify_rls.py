#!/usr/bin/env python3
"""RLS 隔离验证脚本。

这是 P1 的核心验收：不是"我写了策略所以它应该生效"，
而是"用两个真实用户跑一遍，看能不能互相看到"。

用法：
    python scripts/verify_rls.py

退出码：0 = 全部通过；1 = 有失败项
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg
from psycopg import sql

from app.core.config import settings

PASS = "✓"
FAIL = "✗"

results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    results.append((ok, msg))
    print(f"  {PASS if ok else FAIL} {msg}")


def main() -> int:
    admin = settings.DATABASE_ADMIN_URL
    app_url = settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

    print(f"管理连接：{_mask(admin)}")
    print(f"应用连接：{_mask(app_url)}")
    print()

    with psycopg.connect(admin, autocommit=True) as conn:
        with conn.cursor() as cur:
            # ---------------------------------------------------------------
            print("【准备】创建两个测试用户与各自的知识库")
            # ---------------------------------------------------------------
            cur.execute("DELETE FROM users WHERE username IN ('rls_alice','rls_bob')")
            alice, bob = None, None
            for name in ("rls_alice", "rls_bob"):
                cur.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                    (name, "x"),
                )
                uid = cur.fetchone()[0]
                if name.endswith("alice"):
                    alice = uid
                else:
                    bob = uid
                cur.execute(
                    """
                    INSERT INTO knowledge_bases (user_id, name, quota_bytes)
                    VALUES (%s, %s, %s) RETURNING id
                    """,
                    (uid, f"{name}的库", settings.KB_QUOTA_BYTES),
                )
            print(f"  alice={alice}  bob={bob}")

    # ---------------------------------------------------------------
    print("\n【验证 1】应用角色不是 superuser、不能绕过 RLS")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        role_name, is_super, can_bypass = cur.fetchone()
        check(not is_super, f"应用角色 {role_name} 不是 superuser")
        check(not can_bypass, "应用角色无 BYPASSRLS 权限")

    # ---------------------------------------------------------------
    print("\n【验证 2】alice 只能看到自己的库")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(alice),))
        cur.execute("SELECT user_id, name FROM knowledge_bases ORDER BY name")
        rows = cur.fetchall()
        check(len(rows) == 1, f"alice 看到 {len(rows)} 个库（应为 1）")
        check(all(r[0] == alice for r in rows), "alice 看到的都是自己的库")
        print(f"      可见：{[(str(r[0])[:8], r[1]) for r in rows]}")

    # ---------------------------------------------------------------
    print("\n【验证 3】bob 看不到 alice 的库")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(bob),))
        cur.execute("SELECT count(*) FROM knowledge_bases WHERE user_id = %s", (alice,))
        n = cur.fetchone()[0]
        check(n == 0, f"bob 用 alice 的 user_id 直接查，看到 {n} 条（应为 0）")

    # ---------------------------------------------------------------
    print("\n【验证 4】未设置 app.user_id 时，查不到任何业务数据")
    print("          —— 这是「忘记设置就什么都查不到」的安全默认值")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM knowledge_bases")
        n_all = cur.fetchone()[0]
        check(n_all == 0, f"未设置 user_id 时看到 {n_all} 条（应为 0）")

        cur.execute("SELECT count(*) FROM documents")
        check(cur.fetchone()[0] == 0, "documents 同样返回 0 条")

    # ---------------------------------------------------------------
    print("\n【验证 5】设置错误的 user_id（不存在的 uuid）也查不到")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.user_id', '00000000-0000-0000-0000-000000000000', false)"
        )
        cur.execute("SELECT count(*) FROM knowledge_bases")
        check(cur.fetchone()[0] == 0, "不存在的 user_id 返回 0 条")

    # ---------------------------------------------------------------
    print("\n【验证 6】不能替别人插入数据（WITH CHECK 生效）")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(alice),))
        blocked = False
        try:
            cur.execute(
                """
                INSERT INTO knowledge_bases (user_id, name, quota_bytes)
                VALUES (%s, '伪造的库', %s)
                """,
                (bob, settings.KB_QUOTA_BYTES),
            )
        except psycopg.errors.InsufficientPrivilege:
            blocked = True
        except Exception as e:
            blocked = True
            print(f"      （异常类型：{type(e).__name__}）")
        check(blocked, "alice 无法以 bob 的身份插入数据")

    # ---------------------------------------------------------------
    print("\n【验证 7】超管仍可看到全部（用于运维与数据修复）")
    # ---------------------------------------------------------------
    with psycopg.connect(admin, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM knowledge_bases")
        n = cur.fetchone()[0]
        check(n >= 2, f"超管看到 {n} 条（应 ≥2，说明 FORCE RLS 未把 owner 也挡住）")

    # ---------------------------------------------------------------
    print("\n【验证 8】应用角色不能执行 DDL")
    # ---------------------------------------------------------------
    with psycopg.connect(app_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute("SELECT set_config('app.user_id', %s, false)", (str(alice),))
        blocked = False
        try:
            cur.execute("CREATE TABLE should_fail (id int)")
        except psycopg.errors.InsufficientPrivilege:
            blocked = True
        except Exception:
            blocked = True
        check(blocked, "应用角色无法建表（权限最小化）")

    # ---------------------------------------------------------------
    print("\n【清理】")
    # ---------------------------------------------------------------
    with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username IN ('rls_alice','rls_bob')")
    print("  测试用户已删除")

    # ---------------------------------------------------------------
    total = len(results)
    passed = sum(1 for ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{total} 通过")
    if passed == total:
        print("✅ RLS 隔离验证全部通过")
    else:
        print("❌ 存在失败项：")
        for ok, msg in results:
            if not ok:
                print(f"   - {msg}")
    print("=" * 60)
    return 0 if passed == total else 1


def _mask(url: str) -> str:
    import re

    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", url)


if __name__ == "__main__":
    sys.exit(main())
