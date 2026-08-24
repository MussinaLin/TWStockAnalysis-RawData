"""Shared fixtures for RawData tests.

psycopg 連線池的假物件集中在此。四個測試檔原本各自寫一份，彼此只差
「有沒有實作到自己用得到的那幾個方法」——這裡放的是聯集版，涵蓋 db_utils
實際用過的三種存取形態：

    with pool.connection() as conn:
        with conn.cursor() as cur: cur.execute(...) / cur.executemany(...)
    with pool.connection() as conn, conn.cursor() as cur: ...
    with pool.connection() as conn: conn.execute(...).fetchall()

conn 層與 cursor 層的 execute 共用同一份 `executed` 紀錄（前者委派給後者），
所以測試不必在意受測程式碼走的是哪一種形態。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest


class FakeCursor:
    """記錄所有 execute / executemany，並依序吐出預先安排的查詢結果。

    Args:
        fetch_results: fetchone() 依序回傳的值，用完回 None。
        rowcounts: 每次 execute 後 rowcount 依序取的值，用完維持最後一次。
        fetchall_rows: fetchall() 回傳的列。
    """

    def __init__(
        self,
        fetch_results: list[Any] | None = None,
        rowcounts: list[int] | None = None,
        fetchall_rows: list[Any] | None = None,
    ) -> None:
        self._fetch_results = list(fetch_results or [])
        self._rowcounts = list(rowcounts or [])
        self._fetchall_rows = list(fetchall_rows or [])
        self.rowcount = 0
        self.executed: list[tuple[str, Any]] = []
        self.executed_many: list[tuple[str, Any]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)
        return self

    def executemany(self, sql: str, params: Any) -> None:
        self.executed_many.append((sql, params))

    def fetchone(self) -> Any:
        if not self._fetch_results:
            return None
        return self._fetch_results.pop(0)

    def fetchall(self) -> list[Any]:
        return list(self._fetchall_rows)


class FakeConn:
    """conn 層的 execute 委派給 cursor，兩者共用同一份 executed 紀錄。"""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    @property
    def executed(self) -> list[tuple[str, Any]]:
        return self._cursor.executed

    @property
    def cursor_obj(self) -> FakeCursor:
        return self._cursor

    def cursor(self) -> FakeCursor:
        return self._cursor

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        return self._cursor.execute(sql, params)

    def commit(self) -> None:
        self.committed = True


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


def install_fake_pool(monkeypatch, module, cursor: FakeCursor | None = None, **kwargs) -> FakeConn:
    """把 `module.get_pool` 換成回傳假連線池，回傳可供斷言的 FakeConn。

    module 是實際呼叫 get_pool 的那個模組（通常是 db_utils）——patch 要打在
    使用端而不是定義端，否則 `from .db import get_pool` 綁定的名稱不會被換掉。
    """
    cur = cursor if cursor is not None else FakeCursor(**kwargs)
    conn = FakeConn(cur)
    monkeypatch.setattr(module, "get_pool", lambda _url: FakePool(conn))
    return conn


@pytest.fixture
def fake_pool(monkeypatch):
    """回傳一個 installer：fake_pool(db_utils, rowcounts=[3]) -> FakeConn。"""
    def _install(module, cursor: FakeCursor | None = None, **kwargs) -> FakeConn:
        return install_fake_pool(monkeypatch, module, cursor, **kwargs)

    return _install
