"""P0 验收测试：验证骨架可导入、配置可加载、基础组件行为正确。

这些测试**不需要任何外部服务**（数据库 / Redis / 模型），
是 `make test` 的第一道门。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, settings


class TestConfig:
    def test_settings_loaded(self) -> None:
        assert settings.APP_ENV in ("dev", "staging", "prod")
        assert settings.API_PREFIX.startswith("/")

    def test_quota_matches_product_rules(self) -> None:
        """配额是产品规则（D8），写死在配置里，改动需同步文档。"""
        assert settings.MAX_KB_PER_USER == 5
        assert settings.KB_QUOTA_BYTES == 1024**3
        assert settings.MAX_SINGLE_FILE_BYTES == 200 * 1024 * 1024
        assert settings.MAX_FILES_PER_KB == 1000
        assert settings.DAILY_ASK_LIMIT == 200
        assert settings.MAX_CONCURRENT_ASK == 3

    def test_retrieval_returns_top5(self) -> None:
        """C1 决策：重排后取 top 5（不是 8）。"""
        assert settings.RETRIEVE_FINAL_TOP_K == 5
        assert settings.RRF_K == 60

    def test_repair_bounded_to_one(self) -> None:
        """C5 硬约束：修复不超过一次，防无限循环。"""
        assert settings.MAX_REPAIR_ATTEMPTS <= 1

    def test_cannot_exceed_repair_limit(self) -> None:
        with pytest.raises(ValueError, match="不得超过 1"):
            Settings(MAX_REPAIR_ATTEMPTS=3)

    def test_weak_jwt_secret_rejected(self) -> None:
        with pytest.raises(ValueError, match="至少 16 字符"):
            Settings(JWT_SECRET="short")

    def test_prod_requires_custom_secret(self) -> None:
        with pytest.raises(ValueError):
            Settings(APP_ENV="prod", JWT_SECRET="dev-only-change-me-in-production-please")

    def test_prod_requires_fernet(self) -> None:
        with pytest.raises(ValueError, match="FERNET_KEY"):
            Settings(APP_ENV="prod", JWT_SECRET="a" * 32, FERNET_KEY="")

    def test_prod_requires_http_chroma(self) -> None:
        """生产必须用 http 模式，否则多进程看不到同一份向量。"""
        with pytest.raises(ValueError, match="CHROMA_MODE"):
            Settings(
                APP_ENV="prod",
                JWT_SECRET="a" * 32,
                FERNET_KEY="x" * 43 + "=",
                CHROMA_MODE="embedded",
            )

    def test_cors_parsed(self) -> None:
        s = Settings(CORS_ORIGINS="http://a.com, http://b.com ")
        assert s.cors_origin_list == ["http://a.com", "http://b.com"]

    def test_dim_whitelist(self) -> None:
        with pytest.raises(ValueError, match="不常见"):
            Settings(EMBED_DIM=999)


class TestErrorClassification:
    """错误分类错误是最常见的翻车点：把 401 当可重试会打爆鉴权。"""

    def test_embedding_is_retryable(self) -> None:
        from app.core.errors import EmbeddingError, ErrorClass

        e = EmbeddingError("embedding_timeout", "超时")
        assert e.error_class == ErrorClass.RETRYABLE
        assert e.retryable is True

    def test_auth_is_fatal(self) -> None:
        from app.core.errors import AuthError, ErrorClass

        e = AuthError("token_expired", "过期")
        assert e.error_class == ErrorClass.FATAL
        assert e.retryable is False
        assert e.http_status == 401

    def test_quota_is_fatal(self) -> None:
        from app.core.errors import ErrorClass, QuotaError

        e = QuotaError("kb_limit_reached", "已满")
        assert e.error_class == ErrorClass.FATAL

    def test_not_found_payload(self) -> None:
        from app.core.errors import NotFoundError

        e = NotFoundError("knowledge_base", "abc")
        payload = e.to_payload("trace123")
        assert payload["code"] == "knowledge_base_not_found"
        assert payload["trace_id"] == "trace123"


class TestPasswordPolicy:
    """D12：6-12 位，只允许数字与字母，两者至少各一个。"""

    @pytest.mark.parametrize(
        "pwd",
        ["abc123", "Abc123", "a1b2c3", "Password1", "123456a"],
    )
    def test_valid(self, pwd: str) -> None:
        from app.core.security import validate_password_policy

        validate_password_policy(pwd)  # 不抛即通过

    @pytest.mark.parametrize(
        "pwd, reason",
        [
            ("abc12", "长度不足"),
            ("abcdefghij123", "超过 12 位"),
            ("123456", "无字母"),
            ("abcdef", "无数字"),
            ("abc 123", "含空格"),
            ("abc@123", "含特殊字符"),
            ("中文abc1", "含中文"),
        ],
    )
    def test_invalid(self, pwd: str, reason: str) -> None:
        from app.core.security import validate_password_policy

        with pytest.raises(ValueError):
            validate_password_policy(pwd)


class TestTokenFlow:
    def test_access_roundtrip(self) -> None:
        from app.core.security import ACCESS, create_access_token, decode_token

        tok = create_access_token("user-1")
        assert decode_token(tok, ACCESS) == "user-1"

    def test_token_type_mismatch(self) -> None:
        from app.core.security import ACCESS, TokenError, create_refresh_token, decode_token

        tok = create_refresh_token("user-1")
        with pytest.raises(TokenError, match="type_mismatch"):
            decode_token(tok, ACCESS)

    def test_tampered_token(self) -> None:
        from app.core.security import ACCESS, TokenError, create_access_token, decode_token

        tok = create_access_token("user-1")[:-3] + "xyz"
        with pytest.raises(TokenError):
            decode_token(tok, ACCESS)


class TestMasking:
    """日志脱敏：手机号/邮箱/身份证不得落原文。"""

    def test_phone(self) -> None:
        from app.core.logging import mask_text

        assert mask_text("联系 13812345678") == "联系 138****5678"

    def test_email(self) -> None:
        from app.core.logging import mask_text

        assert "a***@example.com" in mask_text("邮箱 a.b@example.com")

    def test_sensitive_key(self) -> None:
        from app.core.logging import _mask_value

        assert _mask_value("api_key", "sk-abcdef") == "***"
        assert _mask_value("Authorization", "Bearer xxx") == "***"

    def test_nested_dict(self) -> None:
        from app.core.logging import _mask_value

        out = _mask_value("payload", {"password": "p", "phone": "13812345678"})
        assert out["password"] == "***"
        assert out["phone"] == "138****5678"


class TestFakeEmbedder:
    def test_deterministic(self, fake_embedder) -> None:
        import asyncio

        v1 = asyncio.run(fake_embedder.embed_query("差旅住宿标准"))
        v2 = asyncio.run(fake_embedder.embed_query("差旅住宿标准"))
        assert v1 == v2

    def test_dim_and_norm(self, fake_embedder) -> None:
        import asyncio
        import math

        v = asyncio.run(fake_embedder.embed_query("测试文本"))
        assert len(v) == fake_embedder.dim
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6

    def test_different_texts_differ(self, fake_embedder) -> None:
        import asyncio

        a = asyncio.run(fake_embedder.embed_query("差旅住宿标准"))
        b = asyncio.run(fake_embedder.embed_query("年度财务报告"))
        assert a != b
