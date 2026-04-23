import asyncio
import concurrent.futures
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from synchronous code.

    Works whether or not an event loop is already running (e.g. eager Celery
    tasks dispatched from inside a FastAPI request handler).
    """
    try:
        asyncio.get_running_loop()
        # Already inside a running loop — delegate to a fresh thread so we
        # don't block or nest the existing loop.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)
