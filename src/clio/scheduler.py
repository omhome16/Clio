# src/clio/scheduler.py
"""Async fan-out: bounded concurrency, per-item retries, per-item timeouts."""
import asyncio
from typing import Awaitable, Callable, Sequence, TypeVar

K = TypeVar("K")
R = TypeVar("R")


async def run_with_retries(
    fn: Callable[[], Awaitable[R]],
    *,
    max_retries: int = 2,
    backoff_s: float = 0.5,
    timeout_s: float | None = None,
) -> R:
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            coro = fn()
            if timeout_s is not None:
                return await asyncio.wait_for(coro, timeout=timeout_s)
            return await coro
        except Exception as exc:
            last = exc
            if attempt < max_retries:
                await asyncio.sleep(backoff_s)
    assert last is not None
    raise last


async def fan_out(
    items: Sequence[K],
    worker: Callable[[K], Awaitable[R]],
    *,
    max_concurrency: int = 4,
    max_retries: int = 2,
    backoff_s: float = 0.5,
    timeout_s: float | None = None,
) -> dict[K, R | BaseException]:
    semaphore = asyncio.Semaphore(max_concurrency)
    results: dict[K, R | BaseException] = {}

    async def run_one(item: K) -> None:
        async with semaphore:
            try:
                results[item] = await run_with_retries(
                    lambda: worker(item),
                    max_retries=max_retries,
                    backoff_s=backoff_s,
                    timeout_s=timeout_s,
                )
            except BaseException as exc:
                results[item] = exc

    await asyncio.gather(*(run_one(item) for item in items))
    return results
