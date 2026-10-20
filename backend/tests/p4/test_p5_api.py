"""API 测试。

## 为什么用 httpx.ASGITransport 而不是起真实服务

ASGI 传输直接调用应用，不经过网络栈：
- 快（不需要端口、不需要等启动）
- 能拿到精确的异常栈
- 但**不能测代理、超时、真实客户端 IP** —— 那些属于部署验证

真实连接串（数据库 + Redis）仍然要用真的：mock 掉就测不到 RLS
和限流计数这些「真出问题的地方」。

## 测试组织

| 类 | 依赖 |
|---|---|
| TestAuthFlow | PG + Redis |
| TestRateLimit | Redis（不需要 PG） |
| TestKBFlow | PG |
| TestUpload | PG |
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest

from app.core.config import settings
from app.core.ratelimit import _client as redis_client
from app.main import app

pytestmark = [pytest.mark.db]

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "eval" / "fixtures" / "synthetic" / "txt_gbk_chinese.txt"
)


def _admin():
    return psycopg.connect(settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=5)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        yield c


@pytest.fixture(autouse=True, scope="session")
def _require_redis():
    """Redis 不可用时整批跳过。

    **为什么要这个 fixture**：限流是 fail-open（Redis 挂了放行），
    每个请求都会等 2 秒超时。没有这个检查，整批测试会因为
    「连不上 Redis」而卡几分钟才结束 —— 上一次实测就是这个症状。

    快速失败比慢慢超时有用得多。
    """
    import redis as sync_redis

    try:
        r = sync_redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        r.ping()
        r.close()
    except Exception as e:
        pytest.skip(f"Redis 不可用（{type(e).__name__}），跳过 API 测试")


async def _clear_ratelimit() -> None:
    """清掉限流计数。用批量删除，不用 scan 循环。"""
    try:
        r = redis_client()
        keys = await r.keys("rl:*")
        if keys:
            await r.delete(*keys)
    except Exception:
        pass


@pytest.fixture(autouse=True)
async def _clean_ratelimit():
    """每个测试前清掉限流计数，避免测试之间互相干扰。"""
    await _clear_ratelimit()
    yield
    await _clear_ratelimit()


@pytest.fixture
async def user(client: httpx.AsyncClient) -> dict:
    """注册一个测试用户，返回 {username, password, token, user_id}。"""
    name = f"u{uuid.uuid4().hex[:10]}"
    pwd = "TestPass123"

    r = await client.post(
        "/api/v1/auth/register", json={"username": name, "password": pwd}
    )
    assert r.status_code == 201, r.text
    data = r.json()

    yield {
        "username": name,
        "password": pwd,
        "token": data["access_token"],
        "user_id": data["user_id"],
    }

    # 清理
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (data["user_id"],))


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


class TestAuthFlow:
    async def test_register_success(self, client: httpx.AsyncClient) -> None:
        name = f"u{uuid.uuid4().hex[:10]}"
        r = await client.post(
            "/api/v1/auth/register", json={"username": name, "password": "TestPass123"}
        )
        try:
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["username"] == name
            assert body["access_token"]
            assert body["expires_in"] == 24 * 3600
            # payload 只放 sub（Q2 决策）
        finally:
            with _admin() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE username = %s", (name,))

    async def test_register_duplicate(self, client: httpx.AsyncClient, user: dict) -> None:
        r = await client.post(
            "/api/v1/auth/register",
            json={"username": user["username"], "password": "Another123"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "username_taken"

    async def test_register_weak_password(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            "/api/v1/auth/register",
            json={"username": f"u{uuid.uuid4().hex[:8]}", "password": "12345678"},
        )
        assert r.status_code == 400

    async def test_register_short_username(self, client: httpx.AsyncClient) -> None:
        r = await client.post(
            "/api/v1/auth/register", json={"username": "ab", "password": "TestPass123"}
        )
        assert r.status_code in (400, 422)

    async def test_login_success(self, client: httpx.AsyncClient, user: dict) -> None:
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": user["username"], "password": user["password"]},
        )
        assert r.status_code == 200
        assert r.json()["access_token"]

    async def test_login_wrong_password(self, client: httpx.AsyncClient, user: dict) -> None:
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": user["username"], "password": "WrongPass999"},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "invalid_credentials"

    async def test_login_unknown_user_same_message(
        self, client: httpx.AsyncClient
    ) -> None:
        """用户不存在与密码错误必须返回相同的 code 和文案 —— 否则可枚举用户名。"""
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "no_such_user_xyz", "password": "Whatever123"},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "invalid_credentials"
        assert r.json()["detail"]["message"] == "用户名或密码错误"

    async def test_me(self, client: httpx.AsyncClient, user: dict) -> None:
        r = await client.get("/api/v1/auth/me", headers=auth(user["token"]))
        assert r.status_code == 200
        assert r.json()["username"] == user["username"]

    async def test_me_without_token(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/v1/auth/me")
        assert r.status_code == 401

    async def test_me_with_bad_token(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/v1/auth/me", headers=auth("not-a-jwt"))
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_username_limit_triggers_first(self, client: httpx.AsyncClient) -> None:
        """同一用户名连续失败，先撞上「用户名维度」的 5 次限制。

        实测发现：用户名阈值（5 次/15 分钟）比 IP 阈值（10 次/1 分钟）
        更严格，所以固定猜一个账号时，先被用户名维度挡住。

        这正是双维度设计的意图 —— 攻击者换 IP 打同一账号也挡得住。
        """
        name = "ratelimit_probe"
        codes = []
        for _ in range(settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS + 3):
            r = await client.post(
                "/api/v1/auth/login",
                json={"username": name, "password": "Whatever123"},
            )
            codes.append(r.status_code)

        assert 429 in codes, f"未触发限流：{codes}"
        first_429 = codes.index(429)
        # 第 N+1 次开始拒绝（前 N 次放行）
        assert first_429 == settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS, (
            f"应在第 {settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS + 1} 次拒绝，"
            f"实际第 {first_429 + 1} 次。codes={codes}"
        )

    async def test_ip_limit_blocks_multiple_accounts(
        self, client: httpx.AsyncClient
    ) -> None:
        """换账号打也挡得住 —— 这是 IP 维度的价值。

        每次用一个新用户名，用户名维度不会触发，
        只有 IP 维度能挡住。
        """
        codes = []
        for _ in range(settings.RATE_LIMIT_LOGIN_PER_MINUTE + 3):
            r = await client.post(
                "/api/v1/auth/login",
                json={"username": f"scan_{uuid.uuid4().hex[:8]}", "password": "x"},
            )
            codes.append(r.status_code)

        assert 429 in codes, f"换账号扫号竟然没被 IP 挡住：{codes}"

    async def test_rate_limited_has_retry_after(self, client: httpx.AsyncClient) -> None:
        """429 必须带 Retry-After，否则客户端不知道等多久。"""
        for _ in range(settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS + 2):
            r = await client.post(
                "/api/v1/auth/login",
                json={"username": "probe2", "password": "x"},
            )
            if r.status_code == 429:
                assert "retry-after" in {k.lower() for k in r.headers}
                return
        pytest.fail("未触发限流")

    async def test_register_limited(self, client: httpx.AsyncClient) -> None:
        codes = []
        for _ in range(settings.RATE_LIMIT_REGISTER_PER_HOUR + 2):
            r = await client.post(
                "/api/v1/auth/register",
                json={"username": f"rl{uuid.uuid4().hex[:8]}", "password": "TestPass123"},
            )
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in codes
        # 清理可能建出来的用户
        with _admin() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username LIKE 'rl%'")


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class TestKBFlow:
    async def test_create_and_list(self, client: httpx.AsyncClient, user: dict) -> None:
        name = f"库{uuid.uuid4().hex[:6]}"
        r = await client.post(
            "/api/v1/kbs",
            json={"name": name, "description": "测试用"},
            headers=auth(user["token"]),
        )
        assert r.status_code == 201, r.text
        kb_id = r.json()["id"]

        r2 = await client.get("/api/v1/kbs", headers=auth(user["token"]))
        assert r2.status_code == 200
        assert any(k["id"] == kb_id for k in r2.json())

    async def test_duplicate_name(self, client: httpx.AsyncClient, user: dict) -> None:
        name = f"库{uuid.uuid4().hex[:6]}"
        h = auth(user["token"])
        await client.post("/api/v1/kbs", json={"name": name}, headers=h)
        r = await client.post("/api/v1/kbs", json={"name": name}, headers=h)
        assert r.status_code == 409

    async def test_requires_auth(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/v1/kbs")
        assert r.status_code == 401

    async def test_delete_wrong_confirm(self, client: httpx.AsyncClient, user: dict) -> None:
        """库名不匹配必须拒绝 —— 后端校验，不是只靠前端。"""
        name = f"库{uuid.uuid4().hex[:6]}"
        h = auth(user["token"])
        r = await client.post("/api/v1/kbs", json={"name": name}, headers=h)
        kb_id = r.json()["id"]

        d = await client.request(
            "DELETE",
            f"/api/v1/kbs/{kb_id}",
            json={"confirm_name": "写错了"},
            headers=h,
        )
        assert d.status_code == 400
        assert d.json()["detail"]["code"] == "confirm_mismatch"

        # 库还在
        g = await client.get(f"/api/v1/kbs/{kb_id}", headers=h)
        assert g.status_code == 200

    async def test_delete_with_confirm(self, client: httpx.AsyncClient, user: dict) -> None:
        name = f"库{uuid.uuid4().hex[:6]}"
        h = auth(user["token"])
        r = await client.post("/api/v1/kbs", json={"name": name}, headers=h)
        kb_id = r.json()["id"]

        d = await client.request(
            "DELETE", f"/api/v1/kbs/{kb_id}", json={"confirm_name": name}, headers=h
        )
        assert d.status_code == 204

        g = await client.get(f"/api/v1/kbs/{kb_id}", headers=h)
        assert g.status_code == 404

    async def test_cross_user_isolation(self, client: httpx.AsyncClient, user: dict) -> None:
        """A 的库，B 看不到 —— 这是 RLS 的核心断言。"""
        name = f"库{uuid.uuid4().hex[:6]}"
        h = auth(user["token"])
        r = await client.post("/api/v1/kbs", json={"name": name}, headers=h)
        kb_id = r.json()["id"]

        other = f"o{uuid.uuid4().hex[:10]}"
        reg = await client.post(
            "/api/v1/auth/register",
            json={"username": other, "password": "TestPass123"},
        )
        other_token = reg.json()["access_token"]
        try:
            g = await client.get(f"/api/v1/kbs/{kb_id}", headers=auth(other_token))
            assert g.status_code == 404, "跨用户访问竟然成功了"
        finally:
            with _admin() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE username = %s", (other,))


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


class TestUpload:
    async def _kb(self, client: httpx.AsyncClient, token: str) -> str:
        r = await client.post(
            "/api/v1/kbs",
            json={"name": f"库{uuid.uuid4().hex[:6]}"},
            headers=auth(token),
        )
        return r.json()["id"]

    async def test_upload_txt(self, client: httpx.AsyncClient, user: dict) -> None:
        h = auth(user["token"])
        kb_id = await self._kb(client, user["token"])

        with FIXTURE.open("rb") as f:
            r = await client.post(
                f"/api/v1/kbs/{kb_id}/documents",
                files={"file": (FIXTURE.name, f, "text/plain")},
                headers=h,
            )

        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["document_id"]
        assert body["job_id"]

    async def test_upload_dedup(self, client: httpx.AsyncClient, user: dict) -> None:
        """同内容第二次上传应命中秒传，不产生新文档。"""
        h = auth(user["token"])
        kb_id = await self._kb(client, user["token"])

        results = []
        for _ in range(2):
            with FIXTURE.open("rb") as f:
                r = await client.post(
                    f"/api/v1/kbs/{kb_id}/documents",
                    files={"file": (FIXTURE.name, f, "text/plain")},
                    headers=h,
                )
            results.append(r.json())

        assert results[0]["status"] == "pending"
        assert results[1]["status"] in ("exists", "requeued")
        assert results[1]["document_id"] == results[0]["document_id"]

    async def test_upload_unsupported_format(
        self, client: httpx.AsyncClient, user: dict
    ) -> None:
        h = auth(user["token"])
        kb_id = await self._kb(client, user["token"])

        r = await client.post(
            f"/api/v1/kbs/{kb_id}/documents",
            files={"file": ("x.xyz", b"data", "application/octet-stream")},
            headers=h,
        )
        assert r.status_code == 415
        assert r.json()["detail"]["code"] == "unsupported_format"

    async def test_upload_empty_file(self, client: httpx.AsyncClient, user: dict) -> None:
        h = auth(user["token"])
        kb_id = await self._kb(client, user["token"])

        r = await client.post(
            f"/api/v1/kbs/{kb_id}/documents",
            files={"file": ("empty.txt", b"", "text/plain")},
            headers=h,
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "empty_file"

    async def test_documents_list(self, client: httpx.AsyncClient, user: dict) -> None:
        h = auth(user["token"])
        kb_id = await self._kb(client, user["token"])

        with FIXTURE.open("rb") as f:
            await client.post(
                f"/api/v1/kbs/{kb_id}/documents",
                files={"file": (FIXTURE.name, f, "text/plain")},
                headers=h,
            )

        r = await client.get(f"/api/v1/kbs/{kb_id}/documents", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert body["items"][0]["status"] == "pending"
