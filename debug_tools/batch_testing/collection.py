"""独立收集队列：发送前预留容量，调用结束即归还请求槽位。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class CollectionPool:
    def __init__(
        self,
        collect: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        concurrency: int = 2,
        capacity: int = 128,
    ) -> None:
        if concurrency < 1 or capacity < 1:
            raise ValueError("收集并发及积压容量必须为正数")
        self.collect = collect
        self.capacity = capacity
        self.pending = 0
        self.slots = asyncio.Semaphore(capacity)
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(capacity)
        self.workers = [asyncio.create_task(self._worker()) for _ in range(concurrency)]
        self.errors: list[Exception] = []

    async def reserve(self) -> None:
        await self.slots.acquire()
        if self.errors:
            self.slots.release()
            raise RuntimeError("收集结果持久化失败，暂停发送新请求") from self.errors[0]
        self.pending += 1

    def release(self) -> None:
        self.pending -= 1
        self.slots.release()

    def submit(self, job: dict[str, Any]) -> None:
        self.queue.put_nowait(job)

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if job is None:
                    return
                await self.collect(job)
            except Exception as exc:
                self.errors.append(exc)
            finally:
                self.queue.task_done()
                if job is not None:
                    self.release()

    async def close(self) -> None:
        await self.queue.join()
        for _ in self.workers:
            await self.queue.put(None)
        await asyncio.gather(*self.workers)
        if self.errors:
            raise RuntimeError("收集结果持久化失败") from self.errors[0]
