"""Background worker: pulls run ids off the queue and drives them.

Also periodically reclaims runs whose worker died (lease expired) so that no
run is stuck in RUNNING forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from agentplat.orchestrator import Runtime
from agentplat.runtime.queue import JobQueue

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        runtime: Runtime,
        queue: JobQueue,
        *,
        reclaim_interval_s: float = 10.0,
        concurrency: int = 4,
    ) -> None:
        self.runtime = runtime
        self.queue = queue
        self.reclaim_interval_s = reclaim_interval_s
        self._sem = asyncio.Semaphore(concurrency)

    async def process_one(self, timeout_s: float = 1.0) -> str | None:
        run_id = await self.queue.dequeue(timeout_s)
        if run_id is None:
            return None
        async with self._sem:
            await self.runtime.execute(run_id)
        return run_id

    async def reclaim_once(self) -> list[str]:
        ids = await self.runtime.store.reclaimable_runs()
        for run_id in ids:
            await self.queue.enqueue(run_id)
        if ids:
            log.info("reclaimed runs", extra={"run_ids": ids})
        return ids

    async def run_forever(self, stop: asyncio.Event) -> None:
        reclaimer = asyncio.create_task(self._reclaim_loop(stop))
        tasks: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                run_id = await self.queue.dequeue(1.0)
                if run_id is None:
                    continue
                await self._sem.acquire()
                task = asyncio.create_task(self._run(run_id))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            reclaimer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reclaimer
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, run_id: str) -> None:
        try:
            await self.runtime.execute(run_id)
        except Exception:
            log.exception("run crashed", extra={"run_id": run_id})
        finally:
            self._sem.release()

    async def _reclaim_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.reclaim_interval_s)
            try:
                await self.reclaim_once()
            except Exception:
                log.exception("reclaim failed")
