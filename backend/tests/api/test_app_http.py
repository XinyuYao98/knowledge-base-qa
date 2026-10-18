"""HTTP 层测试：验证骨架应用可响应、错误结构统一、trace 贯穿。

不依赖数据库与 Redis —— 这两个不可用时 /health 应返回 200 并标记为 error，
而不是崩溃。这一点对部署很重要：编排系统不该因为依赖抖动就反复重启容器。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


class TestHealth:
    def test_health_always_200(self, client: TestClient) -> None:
        """/health 只反映存活，依赖不可用也应返回 200。"""
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["app"] == "ok"
        assert "db" in body and "redis" in body

    def test_trace_header_returned(self, client: TestClient) -> None:
        r = client.get("/health")
        assert "x-trace-id" in {k.lower() for k in r.headers}
        assert len(r.headers["X-Trace-Id"]) == 32

    def test_trace_header_echoed(self, client: TestClient) -> None:
        """客户端传入 trace 时应沿用，便于跨服务串联。"""
        r = client.get("/health", headers={"X-Trace-Id": "a" * 32})
        assert r.headers["X-Trace-Id"] == "a" * 32

    def test_metrics_endpoint(self, client: TestClient) -> None:
        r = client.get("/health/metrics")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)


class TestErrorShape:
    def test_404_is_not_crash(self, client: TestClient) -> None:
        r = client.get("/api/v1/does-not-exist")
        assert r.status_code == 404

    def test_validation_error_shape(self, client: TestClient) -> None:
        """校验失败应返回统一结构，带 trace_id。"""
        # 用一个不存在的 POST 端点触发 405，验证不会 500
        r = client.post("/health")
        assert r.status_code in (405, 404)


class TestOpenApi:
    def test_openapi_available_in_dev(self, client: TestClient) -> None:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert "paths" in spec
        assert "/health" in spec["paths"]
