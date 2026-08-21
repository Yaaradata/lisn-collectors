"""Trigger one sweep immediately and print the result dict.

Waiting two minutes for the cron tick in front of an audience is bad demo pacing.
"""

from __future__ import annotations

import asyncio
import sys
import time


def _run() -> None:
    # Psycopg async pools need SelectorEventLoop on Windows (same as
    # `python -m procrastinate`).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from collector.app import app
    from collector.tasks import sweep

    async def main() -> None:
        async with app.open_async():
            print(await sweep(int(time.time())))

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["loop_factory"] = asyncio.SelectorEventLoop
    asyncio.run(main(), **kwargs)


if __name__ == "__main__":
    _run()
