"""Unit tests for --backfill-limits 的資料收集與批次 UPDATE。"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from tw_stock_rawdata import db_utils, run

DATE = dt.date(2026, 8, 12)


class _FakeCursor:
    def __init__(self, rowcounts: list[int]):
        self._rowcounts = list(rowcounts)
        self.rowcount = 0
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.committed = True


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


class TestUpdatePriceLimitsBatch:
    def test_empty_updates_returns_zero(self) -> None:
        assert db_utils.update_price_limits_batch("postgres://x", []) == 0

    def test_uses_update_not_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """必須是 UPDATE：走 upsert 會 INSERT 出半套 row。"""
        cursor = _FakeCursor([1])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        db_utils.update_price_limits_batch(
            "postgres://x", [("2330", DATE, Decimal("2655"), Decimal("2175"))]
        )

        sql, params = cursor.executed[0]
        assert sql.startswith("UPDATE stock_daily_raw")
        assert "INSERT" not in sql
        assert params == ["2330", DATE, Decimal("2655"), Decimal("2175")]

    def test_single_statement_regardless_of_row_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N 筆更新只能發一次 execute。

        逐列 execute 是一列一次網路往返：實測對遠端 DB 是 96.7ms/次，
        單日 6763 筆要 11 分鐘、回補一年 44.5 小時，等於這個指令沒得用。
        """
        cursor = _FakeCursor([500])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        updates = [
            (f"{i:04d}", DATE, Decimal("11"), Decimal("9")) for i in range(500)
        ]
        db_utils.update_price_limits_batch("postgres://x", updates)

        assert len(cursor.executed) == 1
        sql, params = cursor.executed[0]
        assert len(params) == 500 * 4

    def test_chunks_to_stay_under_param_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超過 chunk 大小要分批：PostgreSQL 單一語句參數上限 65535。"""
        cursor = _FakeCursor([1000, 500])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        n_rows = db_utils._LIMIT_UPDATE_CHUNK + 500
        updates = [
            (f"{i:05d}", DATE, Decimal("11"), Decimal("9")) for i in range(n_rows)
        ]
        db_utils.update_price_limits_batch("postgres://x", updates)

        assert len(cursor.executed) == 2
        assert len(cursor.executed[0][1]) == db_utils._LIMIT_UPDATE_CHUNK * 4
        assert len(cursor.executed[1][1]) == 500 * 4

    def test_returns_total_rowcount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不存在的 (symbol, trade_date) 不計入 —— 由 UPDATE 的 rowcount 反映。"""
        cursor = _FakeCursor([2])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        n = db_utils.update_price_limits_batch(
            "postgres://x",
            [
                ("2330", DATE, Decimal("2655"), Decimal("2175")),
                ("9999", DATE, Decimal("11"), Decimal("9")),
                ("3605", DATE, Decimal("132.0"), Decimal("108.0")),
            ],
        )
        assert n == 2
        assert conn.committed is True


class TestCollectLimitUpdates:
    def _patch_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mi_df: pd.DataFrame,
        tpex_df: pd.DataFrame,
    ) -> None:
        monkeypatch.setattr(run, "fetch_twse_mi_index", lambda s, d: (mi_df, d))
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (tpex_df, d))
        monkeypatch.setattr(run, "prepare_twse_mi_index", lambda df: df)
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

    def test_computes_limits_from_both_markets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # close - change = 2415 → 對齊 test_price_limit.py 的 calc_limits(2415) 案例。
        mi = pd.DataFrame([{"symbol": "2330", "close": 2435.0, "change": 20.0}])
        tpex = pd.DataFrame([{"symbol": "6488", "close": 45.21, "change": 1.41}])
        self._patch_sources(monkeypatch, mi, tpex)

        updates = run._collect_limit_updates(None, DATE)

        by_symbol = {u[0]: u for u in updates}
        assert by_symbol["2330"][1] == DATE
        assert by_symbol["2330"][2] == Decimal("2655")
        assert by_symbol["2330"][3] == Decimal("2175")
        assert "6488" in by_symbol

    def test_skips_rows_without_reference_price(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """除權息日 change 為 NaN → 整檔跳過，不寫入也不覆蓋既有值。"""
        mi = pd.DataFrame(
            [
                {"symbol": "2330", "close": 2415.0, "change": 20.0},
                {"symbol": "3605", "close": 120.0, "change": float("nan")},
            ]
        )
        empty = pd.DataFrame(columns=["symbol", "close", "change"])
        self._patch_sources(monkeypatch, mi, empty)

        symbols = {u[0] for u in run._collect_limit_updates(None, DATE)}
        assert symbols == {"2330"}

    def test_one_market_failing_does_not_lose_the_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tw_stock_rawdata.sources import DataUnavailableError

        def _boom(session, date):
            raise DataUnavailableError("TWSE 掛了")

        tpex = pd.DataFrame([{"symbol": "6488", "close": 45.21, "change": 1.41}])
        monkeypatch.setattr(run, "fetch_twse_mi_index", _boom)
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (tpex, d))
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

        symbols = {u[0] for u in run._collect_limit_updates(None, DATE)}
        assert symbols == {"6488"}

    def test_date_mismatch_is_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """來源回傳的是別天的資料 → 不可寫進本日。"""
        mi = pd.DataFrame([{"symbol": "2330", "close": 2415.0, "change": 20.0}])
        empty = pd.DataFrame(columns=["symbol", "close", "change"])
        monkeypatch.setattr(
            run, "fetch_twse_mi_index", lambda s, d: (mi, dt.date(2026, 8, 11))
        )
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (empty, d))
        monkeypatch.setattr(run, "prepare_twse_mi_index", lambda df: df)
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

        assert run._collect_limit_updates(None, DATE) == []


class TestBackfillLimitsDispatchWarning:
    """finding E：--backfill-limits 先中 dispatch，同傳 --backfill-stocks / --date

    會被靜默忽略；改成印警告。用純週末區間讓 dates 迴圈整段被 skip，
    不必 mock 資料來源或 DB。
    """

    def _args(self, **overrides):
        import argparse

        base = dict(
            date=None, backfill_start="2026-08-15", backfill_end="2026-08-16",
            backfill_stocks=None, backfill_limits=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_warns_when_backfill_stocks_also_set(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = self._args(backfill_stocks="2330,2317")
        run._backfill_limits_command(None, SimpleNamespace(database_url="x"), args)

        out = capsys.readouterr().out
        assert "警告" in out
        assert "--backfill-stocks" in out

    def test_warns_when_date_also_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = self._args(date="2026-08-12")
        run._backfill_limits_command(None, SimpleNamespace(database_url="x"), args)

        out = capsys.readouterr().out
        assert "警告" in out
        assert "--date" in out

    def test_no_warning_when_used_alone(self, capsys: pytest.CaptureFixture[str]) -> None:
        args = self._args()
        run._backfill_limits_command(None, SimpleNamespace(database_url="x"), args)

        out = capsys.readouterr().out
        assert "警告" not in out


def test_backfill_limits_is_not_daily_mode() -> None:
    """--backfill-limits 是手動模式，不該被 config.is_trading_day 休市開關擋住。"""
    import argparse

    args = argparse.Namespace(
        date=None, backfill_start="2025-01-01", backfill_end="2025-01-31",
        backfill_stocks=None, backfill_limits=True, update_shares=False, dahu=False,
    )
    assert run._is_daily_mode(args) is False


class _FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeQueryConn:
    def __init__(self, rows: list[Any]):
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: Any = None):
        self.executed.append((sql, params))
        return _FakeResult(self._rows)


class _FakeQueryPool:
    def __init__(self, conn: _FakeQueryConn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


class TestLoadSymbolsForDate:
    def test_returns_symbol_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeQueryConn([("2330",), ("3605",)])
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakeQueryPool(conn))

        assert db_utils.load_symbols_for_date("postgres://x", DATE) == {"2330", "3605"}
        sql, params = conn.executed[0]
        assert "stock_daily_raw" in sql
        assert params == (DATE,)

    def test_empty_when_date_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeQueryConn([])
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakeQueryPool(conn))

        assert db_utils.load_symbols_for_date("postgres://x", DATE) == set()


class TestBackfillLimitsFiltering:
    """交易所公布全市場約 6763 檔（含權證），本 repo 只存 enabled 的兩百多檔。

    不先過濾就把 6763 筆全送上去，96.8% 命中 0 列，純粹浪費網路往返。
    """

    def _run(self, monkeypatch, existing, collected):
        import argparse

        sent: list[list] = []
        monkeypatch.setattr(run, "load_symbols_for_date", lambda url, d: existing)
        monkeypatch.setattr(run, "_collect_limit_updates", lambda s, d: collected)
        monkeypatch.setattr(
            run, "update_price_limits_batch",
            lambda url, updates: (sent.append(updates), len(updates))[1],
        )
        args = argparse.Namespace(
            backfill_start="2026-08-12", backfill_end="2026-08-12",
            backfill_stocks=None, date=None,
        )
        cfg = SimpleNamespace(database_url="postgres://x")
        run._backfill_limits_command(None, cfg, args)
        return sent

    def test_only_existing_symbols_are_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        collected = [
            ("2330", DATE, Decimal("2655"), Decimal("2175")),
            ("03041P", DATE, Decimal("11"), Decimal("9")),   # 權證，DB 沒有
            ("3605", DATE, Decimal("132.0"), Decimal("108.0")),
        ]
        sent = self._run(monkeypatch, {"2330", "3605"}, collected)

        assert len(sent) == 1
        assert {u[0] for u in sent[0]} == {"2330", "3605"}

    def test_date_with_no_rows_skips_api_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB 該日沒有任何列時，連行情 API 都不該打。"""
        import argparse

        called = []
        monkeypatch.setattr(run, "load_symbols_for_date", lambda url, d: set())
        monkeypatch.setattr(
            run, "_collect_limit_updates",
            lambda s, d: called.append(d) or [],
        )
        args = argparse.Namespace(
            backfill_start="2026-08-12", backfill_end="2026-08-12",
            backfill_stocks=None, date=None,
        )
        run._backfill_limits_command(None, SimpleNamespace(database_url="postgres://x"), args)

        assert called == []
