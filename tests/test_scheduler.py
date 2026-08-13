# tests/test_scheduler.py
import asyncio
import time

import pytest

from clio.scheduler import fan_out, run_with_retries


async def test_fan_out_returns_results():
    async def double(x):
        return x * 2

    results = await fan_out([1, 2, 3], double, max_concurrency=2)
    assert results == {1: 2, 2: 4, 3: 6}


async def test_fan_out_respects_concurrency_cap():
    active = 0
    peak = 0

    async def slow(x):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return x

    results = await fan_out(list(range(6)), slow, max_concurrency=3)
    assert peak == 3
    assert len(results) == 6


async def test_fan_out_captures_failures():
    async def boom(x):
        if x == 2:
            raise ValueError("nope")
        return x

    results = await fan_out([1, 2, 3], boom, max_concurrency=2)
    assert results[1] == 1
    assert isinstance(results[2], ValueError)
    assert results[3] == 3


async def test_run_with_retries_eventually_succeeds():
    calls = {"n": 0}

    async def worker():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("flaky")
        return "ok"

    out = await run_with_retries(
        worker, max_retries=3, backoff_s=0, retryable=lambda e: True
    )
    assert out == "ok" and calls["n"] == 3


async def test_run_with_retries_default_only_retries_transient():
    from clio.llm import RateLimitError, LLMError

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("quota", retry_after=0.0)
        return "ok"

    assert await run_with_retries(flaky, max_retries=3, backoff_s=0) == "ok"

    calls["n"] = 0

    async def permanent():
        calls["n"] += 1
        raise LLMError("bad request 400")

    with pytest.raises(LLMError):
        await run_with_retries(permanent, max_retries=3, backoff_s=0)
    assert calls["n"] == 1  # permanent errors never retried


async def test_run_with_retries_raises_after_exhaustion():
    from clio.llm import RateLimitError

    async def always_fails():
        raise RateLimitError("quota", retry_after=0.0)

    with pytest.raises(RateLimitError):
        await run_with_retries(
            always_fails, max_retries=1, backoff_s=0, retryable=lambda e: True
        )


async def test_run_with_retries_timeout():
    async def sleepy():
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(TimeoutError):
        await run_with_retries(sleepy, max_retries=0, timeout_s=0.05)
