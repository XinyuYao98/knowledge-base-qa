"""本地模型服务的 HTTP 客户端（OpenAI 兼容协议）。

## 三个实测踩到的坑，全部固化在这里

### 坑 1：系统代理会把 127.0.0.1 的请求也转走

环境里有 `HTTP_PROXY=http://127.0.0.1:52768` 时，
httpx 默认 `trust_env=True`，**连 localhost 请求都会走代理**，
结果是 `ReadTimeout`。

→ 所有客户端统一 `trust_env=False`。

### 坑 2：推理模型的思考会吃掉整个 token 预算

`qwen/qwen3.5-9b` 实测：

| max_tokens | 思考 tokens | 回答内容 |
|---|---|---|
| 20 | 20 | 空 |
| 300 | 300 | 空 |
| 1500 | 连续思考 86 秒 | 空 |

它不会自己停下来。`/no_think` 指令也无效。

→ **解法：预填充 assistant 消息**（如 `"根据资料，"`）。
实测思考 tokens 直接变 0，回答在 0.7-2 秒内出现。

### 坑 3：模型 ID 与服务端声明的不一致

LM Studio 里显示 `text-embedding-qwen3-embedding-4b`，
配置里写 `qwen3-embedding-4b` 也能调通（不校验），但以服务端声明为准。

→ 启动时做一次模型存在性检查。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.parsers.base import ParseError  # noqa: F401  （保持同层异常风格）

log = get_logger("llm")


class ModelServiceError(Exception):
    """模型服务不可用或返回异常。"""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    @property
    def retryable(self) -> bool:
        return True


def _client(base_url: str, api_key: str, timeout: float) -> httpx.AsyncClient:
    """统一的 httpx 客户端。

    关键：`trust_env=False` 阻止 httpx 读取 HTTP_PROXY/HTTPS_PROXY。
    不做这件事，localhost 请求会被转发到系统代理，表现为超时。
    """
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(timeout, connect=10.0),
        trust_env=not settings.LLM_DISABLE_PROXY,
    )


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
class EmbeddingClient:
    """向量化客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        dim: int | None = None,
    ) -> None:
        self.base_url = base_url or settings.EMBED_BASE_URL
        self.model = model or settings.EMBED_MODEL
        self.api_key = api_key or settings.EMBED_API_KEY
        self.dim = dim or settings.EMBED_DIM

    async def _embed(self, inputs: list[str], instruction: str = "") -> list[list[float]]:
        if not inputs:
            return []

        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        if instruction:
            # Qwen3-Embedding 的指令前缀：query 侧加，document 侧不加
            payload["input"] = [f"Instruct: {instruction}\nQuery: {t}" for t in inputs]
        if settings.EMBED_SEND_DIMENSIONS:
            payload["dimensions"] = self.dim

        async with _client(self.base_url, self.api_key, settings.EMBED_TIMEOUT_SECONDS) as c:
            try:
                resp = await c.post("/embeddings", json=payload)
            except httpx.HTTPError as e:
                raise ModelServiceError(
                    f"Embedding 服务连接失败（{self.base_url}）：{type(e).__name__}. "
                    "请确认模型服务已启动，且未设置会劫持 localhost 的代理。"
                ) from e

        if resp.status_code != 200:
            raise ModelServiceError(
                f"Embedding 返回 {resp.status_code}",
                detail={"body": resp.text[:300], "model": self.model},
            )

        data = resp.json()
        items = sorted(data["data"], key=lambda x: x["index"])
        vectors = [it["embedding"] for it in items]

        # 维度校验：与实际配置不符说明模型选错了，后面写 Chroma 会全部失败
        actual = len(vectors[0])
        if actual != self.dim:
            raise ModelServiceError(
                f"向量维度不符：配置 {self.dim}，实际 {actual}。"
                f"请检查 EMBED_MODEL（当前 {self.model}）与 EMBED_DIM。",
                detail={"expected": self.dim, "actual": actual},
            )
        return vectors

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档侧：不加指令前缀。按 batch 分批。"""
        out: list[list[float]] = []
        bs = settings.EMBED_BATCH_SIZE
        for i in range(0, len(texts), bs):
            out.extend(await self._embed(texts[i : i + bs]))
        return out

    async def embed_query(self, question: str) -> list[float]:
        """查询侧：加指令前缀（Qwen3-Embedding 是指令感知模型）。"""
        vecs = await self._embed([question], instruction=settings.EMBED_QUERY_INSTRUCTION)
        return vecs[0]

    async def health(self) -> dict[str, Any]:
        try:
            async with _client(self.base_url, self.api_key, 10.0) as c:
                resp = await c.get("/models")
            ok = resp.status_code == 200
            ids = [m["id"] for m in resp.json().get("data", [])] if ok else []
            return {
                "ok": ok,
                "model_present": self.model in ids,
                "model": self.model,
                "available_models": ids,
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
#: 预填充内容。作用见模块 docstring 的「坑 2」。
#: 它让模型跳过思考直接进入回答，否则会一直思考到 token 耗尽。
ASSISTANT_PREFILL = "根据资料，"


class ChatClient:
    """生成客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url or settings.CHAT_BASE_URL
        self.model = model or settings.CHAT_MODEL
        self.api_key = api_key or settings.CHAT_API_KEY

    def _messages(
        self,
        system: str,
        user: str,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
        if history:
            msgs.extend(history)
        msgs.append({"role": "user", "content": user})
        # 预填充：绕开推理模型的思考阶段
        msgs.append({"role": "assistant", "content": ASSISTANT_PREFILL})
        return msgs

    async def complete(
        self,
        system: str,
        user: str,
        *,
        history: list[dict[str, str]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """非流式生成。返回 {content, prompt_tokens, completion_tokens, reasoning_tokens}。"""
        payload = {
            "model": self.model,
            "messages": self._messages(system, user, history),
            "temperature": temperature if temperature is not None else settings.CHAT_TEMPERATURE,
            "max_tokens": max_tokens or settings.CHAT_MAX_OUTPUT_TOKENS,
        }

        async with _client(self.base_url, self.api_key, settings.CHAT_TOTAL_TIMEOUT_SECONDS) as c:
            try:
                resp = await c.post("/chat/completions", json=payload)
            except httpx.HTTPError as e:
                raise ModelServiceError(
                    f"生成服务连接失败（{self.base_url}）：{type(e).__name__}"
                ) from e

        if resp.status_code != 200:
            raise ModelServiceError(
                f"生成返回 {resp.status_code}",
                detail={"body": resp.text[:300], "model": self.model},
            )

        data = resp.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        content = (msg.get("content") or "").strip()

        # 预填充被回显时，说明模型把预填充当成了已有输出，需补回
        if content and not content.startswith(ASSISTANT_PREFILL):
            content = ASSISTANT_PREFILL + content

        reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        if not content:
            log.warning(
                "empty_completion",
                model=self.model,
                reasoning_tokens=reasoning,
                hint="模型只思考未回答，检查是否正确使用了预填充",
            )

        return {
            "content": content,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": reasoning,
        }

    async def stream(
        self,
        system: str,
        user: str,
        *,
        history: list[dict[str, str]] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式生成，逐段吐正文（不含预填充）。"""
        payload = {
            "model": self.model,
            "messages": self._messages(system, user, history),
            "temperature": settings.CHAT_TEMPERATURE,
            "max_tokens": max_tokens or settings.CHAT_MAX_OUTPUT_TOKENS,
            "stream": True,
        }

        async with (
            _client(self.base_url, self.api_key, settings.CHAT_TOTAL_TIMEOUT_SECONDS) as c,
            c.stream("POST", "/chat/completions", json=payload) as resp,
        ):
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise ModelServiceError(
                        f"生成返回 {resp.status_code}",
                        detail={"body": body[:300]},
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        delta = json.loads(raw)["choices"][0].get("delta", {})
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        yield piece

    async def health(self) -> dict[str, Any]:
        try:
            async with _client(self.base_url, self.api_key, 10.0) as c:
                resp = await c.get("/models")
            ok = resp.status_code == 200
            ids = [m["id"] for m in resp.json().get("data", [])] if ok else []
            return {
                "ok": ok,
                "model_present": self.model in ids,
                "model": self.model,
                "available_models": ids,
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
