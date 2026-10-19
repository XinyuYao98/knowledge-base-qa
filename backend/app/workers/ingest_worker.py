"""索引 Worker：轮询 ingest_jobs 并执行索引。

## 三个关键设计

### 1. 模型不可用 → waiting_model，不是 failed

用户上传了文档，但 LM Studio 没开。这不是「文档有问题」，
是「环境没准备好」。所以任务进入 `waiting_model` 并延迟重试，
文档状态也标成 `waiting_model`，前端提示「等待模型服务」。

**如果标成 failed，用户会以为文档坏了，删了重传 —— 重传还是会失败。**

### 2. 重试有上限，但 waiting_model 不计入

`attempts` 只在真正的错误时累加。模型没开可以无限等，
文件解析失败重试 3 次就够。

### 3. worker 用管理员连接，API 用应用角色

`ingest_jobs` 受 RLS 保护，但 worker 必须跨用户领任务 —— 领之前不知道是哪个用户的。
用 `worker_session`（管理员连接）解决这个鸡生蛋问题。

**不给应用角色提权**：隔离边界不变，提权仅限 worker 这个受信任组件。
详见 `app/db/worker_session.py` 的说明。

### 4. 池化并发，单任务超时

一个 worker 进程跑 N 个协程并发处理。每个任务有独立超时，
避免某个大文件把整个 worker 拖死。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repos import DocumentRepo, JobRepo
from app.db.worker_session import get_worker_factory
from app.services.indexer import Indexer

log = get_logger("worker")

#: 模型服务不可用时的重试间隔（秒）
WAITING_MODEL_DELAY = 30

#: 普通错误的重试间隔（指数退避基数）
RETRY_BASE_DELAY = 10

#: 最大尝试次数（不含 waiting_model）
MAX_ATTEMPTS = 3


class Worker:
    """单进程轮询 worker。"""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        poll_interval: float | None = None,
        once: bool = False,
    ) -> None:
        self.concurrency = concurrency or settings.WORKER_CONCURRENCY
        self.poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL
        self.once = once
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(self.concurrency)

    # ------------------------------------------------------------------
    def stop(self) -> None:
        log.info("worker_stop_requested")
        self._stop.set()

    async def _source_path(self, doc: dict[str, Any]) -> Path | None:
        """由 storage_key 还原源文件路径。"""
        key = doc.get("storage_key") or ""
        if not key:
            return None
        p = Path(key)
        if p.is_absolute():
            return p
        return settings.files_dir / key

    # ------------------------------------------------------------------
    async def _process(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        doc_id = job["document_id"]
        attempts = int(job.get("attempts") or 0)

        factory = get_worker_factory()
        async with factory() as session, session.begin():
            repo_job = JobRepo(session)
            repo_doc = DocumentRepo(session)

            doc = await repo_doc.get(doc_id)
            if doc is None:
                await repo_job.fail(job_id, error="文档不存在")
                return

            source = await self._source_path(doc)
            if source is None or not source.exists():
                await repo_job.fail(job_id, error=f"源文件缺失：{doc.get('storage_key')}")
                await repo_doc.update_status(
                    doc_id, "failed", error_code="source_missing", error_detail="源文件缺失"
                )
                return

            indexer = Indexer(session)
            try:
                outcome = await asyncio.wait_for(
                    indexer.index(doc_id, source),
                    timeout=settings.INDEX_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await repo_job.retry_later(
                    job_id, error="索引超时", delay_seconds=RETRY_BASE_DELAY * attempts
                )
                return

        # ---- 会话已提交，依据结果处理任务 ----
        if outcome.ok:
            async with factory() as s2, s2.begin():
                await JobRepo(s2).finish(job_id, stage="index")
            return

        is_model_issue = (
            outcome.error_code == "embed"
            and "模型" in (outcome.error_detail or "")
        ) or "ConnectError" in (outcome.error_detail or "")

        async with factory() as s2, s2.begin():
            rj = JobRepo(s2)
            rd = DocumentRepo(s2)

            if is_model_issue:
                # 模型没开：不累加失败次数，30 秒后再试
                await rj.retry_later(
                    job_id,
                    error=outcome.error_detail or "模型服务不可用",
                    delay_seconds=WAITING_MODEL_DELAY,
                    waiting_model=True,
                )
                await rd.update_status(
                    doc_id,
                    "waiting_model",
                    error_code="model_unavailable",
                    error_detail="模型服务不可用，等待中",
                )
                log.warning("waiting_model", doc_id=str(doc_id))
                return

            if attempts >= MAX_ATTEMPTS:
                await rj.fail(job_id, error=outcome.error_detail or "多次失败")
                log.error("job_failed", doc_id=str(doc_id), attempts=attempts)
            else:
                await rj.retry_later(
                    job_id,
                    error=outcome.error_detail or "未知错误",
                    delay_seconds=RETRY_BASE_DELAY * max(attempts, 1),
                )

    async def _run_one(self, job: dict[str, Any]) -> None:
        async with self._sem:
            try:
                await self._process(job)
            except Exception:
                log.exception("job_crashed", job_id=str(job.get("id")))
                # 崩溃也要把任务标出来，否则会永远卡在 running
                factory = get_worker_factory()
                async with factory() as s, s.begin():
                    await JobRepo(s).retry_later(
                        job["id"], error="worker 内部异常", delay_seconds=60
                    )

    async def _claim(self) -> dict[str, Any] | None:
        factory = get_worker_factory()
        async with factory() as session, session.begin():
            return await JobRepo(session).claim_one()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        log.info(
            "worker_start",
            concurrency=self.concurrency,
            poll_interval=self.poll_interval,
            once=self.once,
        )
        tasks: set[asyncio.Task] = set()

        while not self._stop.is_set():
            job = await self._claim()
            if job is None:
                if self.once:
                    # 单次模式：队列空了就退出
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
                continue

            log.info("job_claimed", job_id=str(job["id"]), doc_id=str(job["document_id"]))
            t = asyncio.create_task(self._run_one(job))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

            if self.once:
                # 单次模式串行处理，便于观察每一步
                await asyncio.gather(*tasks, return_exceptions=True)

        if tasks:
            log.info("worker_draining", pending=len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

        log.info("worker_exit")


async def run_worker(once: bool = False) -> None:
    w = Worker(once=once)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows 的 ProactorEventLoop 不支持 add_signal_handler；
        # 用 selector 循环时支持，但为稳妥起见统一走 suppress
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, w.stop)
    await w.run()


def _cli() -> int:
    """命令行入口：python -m app.workers.ingest_worker [--once]"""
    import argparse

    from app.core.platform import assert_event_loop_compatible, selector_loop_factory

    ap = argparse.ArgumentParser(description="索引 worker")
    ap.add_argument("--once", action="store_true", help="处理完队列即退出")
    ap.add_argument("--concurrency", type=int, default=None)
    args = ap.parse_args()

    assert_event_loop_compatible(where="ingest_worker CLI")
    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(
            Worker(once=args.once, concurrency=args.concurrency).run()
        ) or 0
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(_cli())
