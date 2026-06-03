"""Unit tests for upsert_daily_raw COALESCE semantics.

回歸測試：upsert_daily_raw 必須以 COALESCE(EXCLUDED.col, stock_daily_raw.col) 寫入，
新值為 NULL 時保留 DB 既有值，避免子來源暫時性失敗把整欄好資料蓋成 NULL。
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Any

import pandas as pd
import pytest

from tw_stock_rawdata import db_utils


class _FakeCursor:
    def __init__(self) -> None:
        self.executed_many: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def executemany(self, sql: str, params: Any) -> None:
        self.executed_many.append((sql, params))


class _FakeConn:
    def __init__(self, cur: _FakeCursor):
        self._cur = cur
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cur

    def commit(self) -> None:
        self.committed = True


class _FakePool:
    def __init__(self, cur: _FakeCursor):
        self._conn = _FakeConn(cur)

    @contextmanager
    def connection(self):
        yield self._conn


def _install(monkeypatch, cur: _FakeCursor) -> _FakePool:
    pool = _FakePool(cur)
    monkeypatch.setattr(db_utils, "get_pool", lambda _url: pool)
    # ensure_partition 觸 DB，unit test 以 no-op 取代
    monkeypatch.setattr(db_utils, "ensure_partition", lambda conn, trade_date: None)
    return pool


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "symbol": "2330",
            "name": "台積電",
            "open": 1000.0,
            "close": 1010.0,
            "high": 1020.0,
            "low": 990.0,
            "volume": 30000,
            "turnover_rate": 0.001,
            # 法人欄全 None：模擬 T86 當日暫時性失敗 → 不可蓋掉舊值
            "foreign_net": None,
            "trust_net": None,
            "dealer_net": None,
            "institutional_investors_net": None,
            "margin_buy": 100,
            "margin_sell": 50,
            "margin_balance": 1000,
            "margin_change": 50,
            "short_sell": 5,
            "short_buy": 2,
            "short_balance": 20,
            "short_change": 3,
            "short_margin_ratio": 0.02,
            "foreign_holding_pct": None,
            "insti_holding_pct": None,
        }
    ])


def test_upsert_uses_coalesce_for_every_update_column(monkeypatch):
    """每個 update 欄位都必須是 COALESCE(EXCLUDED.col, stock_daily_raw.col)。"""
    cur = _FakeCursor()
    pool = _install(monkeypatch, cur)

    db_utils.upsert_daily_raw("postgres://x", dt.date(2026, 5, 13), _sample_df())

    assert len(cur.executed_many) == 1
    sql, _params = cur.executed_many[0]

    update_cols = [c for c in db_utils._RAW_COLUMNS if c not in ("symbol", "trade_date")]
    for col in update_cols:
        assert f"{col} = COALESCE(EXCLUDED.{col}, stock_daily_raw.{col})" in sql
    assert pool._conn.committed


def test_upsert_has_no_bare_excluded_overwrite(monkeypatch):
    """回歸鎖：不得殘留裸 'col = EXCLUDED.col'（會把 NULL 蓋掉好資料）。"""
    cur = _FakeCursor()
    _install(monkeypatch, cur)

    db_utils.upsert_daily_raw("postgres://x", dt.date(2026, 5, 13), _sample_df())

    sql, _params = cur.executed_many[0]
    set_part = sql.split("DO UPDATE SET", 1)[1]
    for col in db_utils._RAW_COLUMNS:
        if col in ("symbol", "trade_date"):
            continue
        # COALESCE(EXCLUDED.col, ...) 內含 'EXCLUDED.col'，但不得出現賦值式 'col = EXCLUDED.col'
        assert f"{col} = EXCLUDED.{col}" not in set_part


def test_upsert_sql_structure_and_none_passthrough(monkeypatch):
    """SQL 結構正確，且含 None 的 row 仍照常進 executemany（NULL 由 DB 端 COALESCE 保護）。"""
    cur = _FakeCursor()
    _install(monkeypatch, cur)

    trade_date = dt.date(2026, 5, 13)
    db_utils.upsert_daily_raw("postgres://x", trade_date, _sample_df())

    sql, params = cur.executed_many[0]
    assert "INSERT INTO stock_daily_raw" in sql
    assert "ON CONFLICT (symbol, trade_date) DO UPDATE SET" in sql

    # executemany 收到 1 筆，欄位順序 = _RAW_COLUMNS（symbol, trade_date 在前）
    assert len(params) == 1
    row = params[0]
    assert len(row) == len(db_utils._RAW_COLUMNS)
    assert row[0] == "2330"
    assert row[1] == trade_date
    # foreign_net 在 _RAW_COLUMNS 的位置應為 None（pass through）
    fn_idx = db_utils._RAW_COLUMNS.index("foreign_net")
    assert row[fn_idx] is None


def test_upsert_empty_df_skips_db(monkeypatch):
    cur = _FakeCursor()
    pool = _install(monkeypatch, cur)

    db_utils.upsert_daily_raw("postgres://x", dt.date(2026, 5, 13), pd.DataFrame())

    assert cur.executed_many == []
    assert not pool._conn.committed


def test_upsert_all_blank_symbols_skips_db(monkeypatch):
    cur = _FakeCursor()
    pool = _install(monkeypatch, cur)

    df = pd.DataFrame([{"symbol": "", "close": 100.0}])
    db_utils.upsert_daily_raw("postgres://x", dt.date(2026, 5, 13), df)

    assert cur.executed_many == []
    assert not pool._conn.committed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
