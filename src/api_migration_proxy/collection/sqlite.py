"""단일 전용 스레드로 관리하는 로컬 SQLite 이벤트 저장소."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .collector import BatchResult
from .events import CollectionEvent, _finite, _utc
from .query import EventQuery, QueryAccess


class SQLiteEventStore:
    """단일 전용 스레드로 관리하는 로컬 SQLite 이벤트 저장소."""

    def __init__(self, path: str, *, create: bool = True):
        self._path = path
        self._create = create
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comparison-store")
        self._slot = asyncio.Semaphore(1)
        self._connection: sqlite3.Connection | None = None
        self._closing: asyncio.Future | None = None
        self.counters: Counter[str] = Counter()

    def _prepare_file(self) -> None:
        if self._path == ":memory:":
            return
        path = Path(self._path)
        if self._create:
            missing = []
            directory = path.parent
            while not directory.exists():
                missing.append(directory)
                directory = directory.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | (os.O_CREAT if self._create else 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self._prepare_file()
            if not self._create:
                self._connection = sqlite3.connect(
                    Path(self._path).resolve().as_uri() + "?mode=rw", uri=True, timeout=1
                )
                return self._connection
            connection = sqlite3.connect(self._path, timeout=1)
            try:
                connection.execute("""CREATE TABLE IF NOT EXISTS comparison_event (
                    event_id TEXT PRIMARY KEY, created_at REAL NOT NULL, summary_expires_at REAL NOT NULL,
                    detail_expires_at REAL, route_id TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
                    configuration_revision TEXT NOT NULL, comparison_policy_revision TEXT NOT NULL,
                    summary TEXT NOT NULL, detail TEXT, stored_at REAL NOT NULL
                )""")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS event_route_time ON comparison_event(route_id, created_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS event_summary_expiry ON comparison_event(summary_expires_at)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS event_detail_expiry ON comparison_event(detail_expires_at) WHERE detail IS NOT NULL"
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    async def _run(self, operation: Callable[[], Any]) -> Any:
        if self._closing is not None:
            raise RuntimeError("event store is closed")
        await self._slot.acquire()
        try:
            if self._closing is not None:
                raise RuntimeError("event store is closed")
            future = asyncio.get_running_loop().run_in_executor(self._executor, operation)
        except BaseException:
            self._slot.release()
            raise

        def completed(operation: asyncio.Future) -> None:
            self._slot.release()
            if not operation.cancelled():
                operation.exception()

        future.add_done_callback(completed)
        await asyncio.wait((future,))
        return future.result()

    async def write_batch(self, events: Sequence[CollectionEvent]) -> BatchResult:
        """이벤트 ID 중복을 무시하며 묶음을 트랜잭션으로 저장.

        Args:
            events: 검증과 마스킹을 마친 이벤트 목록.

        Returns:
            커밋 후 확인된 모든 입력 이벤트 ID.

        Raises:
            RuntimeError: 이미 종료 중인 저장소 접근.
            sqlite3.Error: SQLite 연결 또는 쿼리 실행 실패.
        """

        def write() -> BatchResult:
            db = self._db()
            with db:
                for event in events:
                    summary = dict(event.summary)
                    if event.detail is not None:
                        summary["detail_state"] = "stored"
                    db.execute(
                        "INSERT INTO comparison_event ("
                        "event_id, created_at, summary_expires_at, detail_expires_at, route_id, result, "
                        "reason, configuration_revision, comparison_policy_revision, summary, detail, stored_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                        (
                            event.event_id,
                            event.created_at,
                            event.summary_expires_at,
                            event.detail_expires_at,
                            summary["route_id"],
                            summary["comparison"]["result"],
                            summary["comparison"]["reason"],
                            summary["configuration_revision"],
                            summary["comparison_policy_revision"],
                            json.dumps(summary, allow_nan=False),
                            json.dumps(event.detail, allow_nan=False, ensure_ascii=False)
                            if event.detail is not None
                            else None,
                            time.time(),
                        ),
                    )
            return BatchResult(acknowledged=frozenset(event.event_id for event in events))

        return await self._run(write)

    async def query(
        self, query: EventQuery, access: QueryAccess, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        """접근 권한과 보존 기한을 적용하여 이벤트 조회.

        Args:
            query: 시간 구간·라우트·결과 필터.
            access: 허용 라우트·기간·건수 및 상세 접근 권한.
            now: 보존 기한 확인용 Unix 타임스탬프. None이면 현재 시각.

        Returns:
            만료 상세와 비허용 상세를 제외한 이벤트 목록.

        Raises:
            PermissionError: 허용 범위를 벗어난 라우트 조회.
            ValueError: 기간·건수·필터·조회 시각 제약 위반.
            RuntimeError: 이미 종료 중인 저장소 접근.
            sqlite3.Error: SQLite 연결 또는 쿼리 실행 실패.
        """
        if not query.routes or not query.routes <= access.routes:
            raise PermissionError("query_route_denied")
        if (
            not all(_finite(value) for value in (query.start, query.end, access.max_period_seconds))
            or not 0 < query.end - query.start <= access.max_period_seconds
            or type(query.limit) is not int
            or not 0 < query.limit <= access.max_rows
        ):
            raise ValueError("query_limit_exceeded")
        if query.backend not in (None, "v1", "v2") or query.role not in (None, "serving", "shadow"):
            raise ValueError("invalid_backend_filter")
        if now is not None and not _finite(now):
            raise ValueError("invalid_query_time")

        def read() -> list[dict[str, Any]]:
            current = time.time() if now is None else now
            clauses = [
                "created_at >= ?",
                "created_at < ?",
                "summary_expires_at > ?",
                f"route_id IN ({','.join('?' for _ in query.routes)})",
            ]
            parameters: list[Any] = [query.start, query.end, current, *sorted(query.routes)]
            for name in (
                "result",
                "reason",
                "configuration_revision",
                "comparison_policy_revision",
                "event_id",
            ):
                value = getattr(query, name)
                if value is not None:
                    clauses.append(f"{name} = ?")
                    parameters.append(value)
            if query.backend or query.role or query.deployment_revision is not None:
                backend_clauses = []
                for backend in (query.backend,) if query.backend else ("v1", "v2"):
                    filters = []
                    for name, value in (
                        ("role", query.role),
                        ("deployment_revision", query.deployment_revision),
                    ):
                        if value is not None:
                            filters.append(
                                f"json_extract(summary, '$.backends.{backend}.{name}') = ?"
                            )
                            parameters.append(value)
                    backend_clauses.append("(" + " AND ".join(filters or ["1 = 1"]) + ")")
                clauses.append("(" + " OR ".join(backend_clauses) + ")")
            rows = (
                self._db()
                .execute(
                    "SELECT event_id, created_at, summary_expires_at, detail_expires_at, summary, detail, stored_at FROM comparison_event WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY created_at, event_id LIMIT ?",
                    (*parameters, query.limit),
                )
                .fetchall()
            )
            current = time.time() if now is None else now
            results = []
            for event_id, created, expires, detail_expires, raw, detail, stored in rows:
                if expires <= current:
                    continue
                summary = json.loads(raw)
                expired = detail_expires is not None and detail_expires <= current
                if expired and summary["detail_state"] == "stored":
                    summary["detail_state"] = "expired"
                results.append(
                    {
                        "event_id": event_id,
                        "created_at": _utc(created),
                        "summary_expires_at": _utc(expires),
                        "stored_at": _utc(stored),
                        "summary": summary,
                        "detail": json.loads(detail)
                        if detail and access.allow_details and not expired
                        else None,
                    }
                )
            return results

        return await self._run(read)

    async def purge_expired(self, *, now: float | None = None, batch_size: int) -> dict[str, int]:
        """만료 상세와 요약을 각각 제한된 건수만큼 삭제.

        Args:
            now: 만료 판정용 Unix 타임스탬프. None이면 현재 시각.
            batch_size: 상세와 요약 각각의 최대 삭제 건수.

        Returns:
            상세·요약 삭제 건수와 남은 만료 이벤트 수.

        Raises:
            ValueError: 삭제 시각 또는 묶음 크기 오류.
            RuntimeError: 이미 종료 중인 저장소 접근.
            sqlite3.Error: SQLite 연결 또는 쿼리 실행 실패.
        """
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("invalid_delete_batch")
        current = time.time() if now is None else now
        if not _finite(current):
            raise ValueError("invalid_delete_time")

        def purge() -> dict[str, int]:
            try:
                db = self._db()
                with db:
                    details = db.execute(
                        "UPDATE comparison_event SET detail = NULL, summary = json_set(summary, '$.detail_state', 'expired') WHERE event_id IN (SELECT event_id FROM comparison_event WHERE detail IS NOT NULL AND detail_expires_at <= ? LIMIT ?)",
                        (current, batch_size),
                    ).rowcount
                    summaries = db.execute(
                        "DELETE FROM comparison_event WHERE event_id IN (SELECT event_id FROM comparison_event WHERE summary_expires_at <= ? LIMIT ?)",
                        (current, batch_size),
                    ).rowcount
                pending = db.execute(
                    "SELECT count(*) FROM comparison_event WHERE summary_expires_at <= ? OR (detail IS NOT NULL AND detail_expires_at <= ?)",
                    (current, current),
                ).fetchone()[0]
            except Exception:
                self.counters["purge_failures"] += 1
                raise
            self.counters["expired_pending"] = pending
            return {
                "details_deleted": details,
                "summaries_deleted": summaries,
                "expired_pending": pending,
            }

        return await self._run(purge)

    async def close(self) -> None:
        """진행 중 작업 뒤에 연결 종료를 예약하고 완료 대기."""

        def close_db() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

        if self._closing is None:
            # 호출자가 ACK 대기를 중단해도 진행 중 쓰기 이후 전용 스레드에서 연결 종료.
            self._closing = asyncio.get_running_loop().run_in_executor(self._executor, close_db)
            self._closing.add_done_callback(
                lambda future: future.exception() if not future.cancelled() else None
            )
            self._executor.shutdown(wait=False)
        await asyncio.wait((self._closing,))
        self._closing.result()
