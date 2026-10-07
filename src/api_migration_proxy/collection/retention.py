"""만료 이벤트를 제한된 묶음으로 주기적으로 삭제."""

from __future__ import annotations

import asyncio
from typing import Protocol

from .events import _finite


class RetentionStore(Protocol):
    """만료 요약·상세의 일괄 삭제 계약."""

    async def purge_expired(self, *, batch_size: int) -> dict[str, int]: ...


class RetentionWorker:
    """삭제 실패 뒤에도 다음 주기에 작업을 계속하는 보존 작업자."""

    def __init__(
        self,
        store: RetentionStore,
        *,
        interval_seconds: float = 60,
        batch_size: int = 1000,
    ):
        if not _finite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("invalid_retention_interval")
        if type(batch_size) is not int or not 0 < batch_size <= 2**63 - 1:
            raise ValueError("invalid_retention_batch")
        self._store = store
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        """중복 실행을 막고 주기적 삭제 작업 시작."""
        if self._stop.is_set():
            raise RuntimeError("retention worker is closed")
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self._interval)
                return
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            try:
                await self._store.purge_expired(batch_size=self._batch_size)
            except Exception:
                pass

    async def close(self, timeout: float) -> None:
        """중지 신호 전달 후 제한 시간 내 작업 종료."""
        if not _finite(timeout) or timeout < 0:
            raise ValueError("invalid_retention_close_timeout")
        self._stop.set()
        if self._worker is None:
            return
        _, pending = await asyncio.wait({self._worker}, timeout=timeout)
        if pending:
            self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
