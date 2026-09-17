"""Worker 进程入口：``python -m appointment.worker``。

设计稿 §4.1 选定模块化单体 + **独立 Worker**：Worker 与 API 共享同一份代码与
数据库，但以独立进程运行，避免长轮询占住 Web 进程的并发额度。

退出语义刻意做对：

- ``SIGTERM``/``SIGINT`` 只设置"该停了"标志，**等当前一轮跑完再退出**。立即
  退出会把"已领取但未归还"的租约留在表里——那本身能被接管（lease_until 到期），
  但会让这一轮的其他待办白等一个租约周期。
- 空轮不清零、不退出：Worker 是常驻进程，没有待办是正常状态而不是结束条件。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from ..config.settings import get_settings
from ..db.session import dispose_engine
from .providers import build_provider
from .runner import Worker

logger = logging.getLogger("appointment.worker")


def build_worker(*, owner: str | None = None) -> Worker:
    """按配置构造 Worker。未配置通知渠道时 ``provider`` 为 None，
    Outbox 与回收照常运行，但不会假装发过通知。"""

    settings = get_settings()
    provider = build_provider(settings)
    import os

    resolved_owner = owner or f"worker-{os.getpid()}"
    if provider is not None:
        resolved_owner = f"{resolved_owner}:{provider.name}:{provider.account_id}"
    return Worker(owner=resolved_owner, settings=settings, provider=provider)


async def serve(
    *,
    owner: str | None = None,
    max_iterations: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """常驻循环。``max_iterations`` 仅用于测试与一次性回填。"""

    settings = get_settings()
    worker = build_worker(owner=owner)
    stop = stop_event or asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - 非 Unix 平台
            pass

    iterations = 0
    try:
        while not stop.is_set():
            report = await worker.run_once()
            if any(
                value
                for value in report.as_dict().values()
                if isinstance(value, int) and value
            ) or report.reclaimed:
                logger.info("worker 一轮：%s", report.as_dict())
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.worker_poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
    finally:
        await dispose_engine()


def main() -> None:  # pragma: no cover - 进程入口
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(serve())


__all__ = ["build_worker", "main", "serve"]


if __name__ == "__main__":  # pragma: no cover
    main()
