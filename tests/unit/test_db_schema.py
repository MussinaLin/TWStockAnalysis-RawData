"""Unit tests: db.py 的 schema DDL。

market_daily 是線上已存在的表，新欄位必須同時出現在 CREATE TABLE（全新 DB）與
idempotent 的 ALTER TABLE ... ADD COLUMN IF NOT EXISTS（既有 DB）。
"""

from __future__ import annotations

import re

import pytest

from tw_stock_rawdata.db import _SCHEMA_SQL
from tw_stock_rawdata.db_utils import (
    _DAY_TRADE_COL_TYPES,
    _FUTURES_OI_COLS,
    _MINI_FUTURES_OI_COLS,
    _VIX_COL_TYPES,
)


def _create_table_body(table: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", _SCHEMA_SQL, re.S
    )
    assert match, f"找不到 {table} 的 CREATE TABLE"
    return match.group(1)


@pytest.mark.parametrize("col", _FUTURES_OI_COLS)
def test_futures_oi_column_in_create_table(col: str) -> None:
    assert re.search(rf"^\s*{col}\s+BIGINT,", _create_table_body("market_daily"), re.M)


@pytest.mark.parametrize("col", _FUTURES_OI_COLS)
def test_futures_oi_column_has_online_migration(col: str) -> None:
    assert re.search(
        rf"ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS {col}\s+BIGINT;", _SCHEMA_SQL
    )


def test_vix_column_in_create_table() -> None:
    assert list(_VIX_COL_TYPES) == ["tw_vix_close"]
    assert re.search(
        r"^\s*tw_vix_close\s+NUMERIC\(8,2\),", _create_table_body("market_daily"), re.M
    )


def test_vix_column_has_online_migration() -> None:
    assert re.search(
        r"ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS tw_vix_close\s+NUMERIC\(8,2\);",
        _SCHEMA_SQL,
    )


_DAY_TRADE_DDL = {
    "day_trade_shares": r"BIGINT",
    "day_trade_shares_pct": r"NUMERIC\(6,4\)",
    "day_trade_buy_amount": r"BIGINT",
    "day_trade_buy_amount_pct": r"NUMERIC\(6,4\)",
    "day_trade_sell_amount": r"BIGINT",
    "day_trade_sell_amount_pct": r"NUMERIC\(6,4\)",
}


def test_day_trade_col_types_match_ddl() -> None:
    assert list(_DAY_TRADE_COL_TYPES) == list(_DAY_TRADE_DDL)
    for col, pg_type in _DAY_TRADE_COL_TYPES.items():
        assert pg_type == ("numeric" if col.endswith("_pct") else "bigint")


@pytest.mark.parametrize("col,ddl", _DAY_TRADE_DDL.items())
def test_day_trade_column_in_create_table(col: str, ddl: str) -> None:
    assert re.search(rf"^\s*{col}\s+{ddl},", _create_table_body("market_daily"), re.M)


@pytest.mark.parametrize("col,ddl", _DAY_TRADE_DDL.items())
def test_day_trade_column_has_online_migration(col: str, ddl: str) -> None:
    assert re.search(
        rf"ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS {col}\s+{ddl};", _SCHEMA_SQL
    )


# ---------------------------------------------------------------------------
# etfs / etf_daily（熱門 ETF 規模與受益人數）
# ---------------------------------------------------------------------------

_ETF_DAILY_DDL = {
    "units": r"BIGINT",
    "nav": r"NUMERIC\(12,4\)",
    "aum_100m": r"NUMERIC\(12,2\)",
    "holders": r"INTEGER",
}


@pytest.mark.parametrize("col,typ", _ETF_DAILY_DDL.items())
def test_etf_daily_columns(col: str, typ: str) -> None:
    assert re.search(rf"^\s*{col}\s+{typ},", _create_table_body("etf_daily"), re.M)


def test_etf_daily_primary_key() -> None:
    assert "PRIMARY KEY (symbol, trade_date)" in _create_table_body("etf_daily")


def test_etfs_table() -> None:
    body = _create_table_body("etfs")
    assert re.search(r"^\s*symbol\s+VARCHAR\(10\)\s+PRIMARY KEY,", body, re.M)
    assert re.search(r"^\s*enabled\s+BOOLEAN\s+NOT NULL DEFAULT TRUE,", body, re.M)


@pytest.mark.parametrize(
    "trg,table", [("trg_etfs_updated", "etfs"), ("trg_etf_daily_updated", "etf_daily")]
)
def test_etf_updated_time_triggers(trg: str, table: str) -> None:
    assert re.search(rf"tgname = '{trg}'.*?BEFORE UPDATE ON {table}\s", _SCHEMA_SQL, re.S)


def test_mini_futures_oi_cols_order() -> None:
    assert _MINI_FUTURES_OI_COLS == [
        "mtx_market_oi", "mtx_inst_long_oi", "mtx_inst_short_oi",
        "tmf_market_oi", "tmf_inst_long_oi", "tmf_inst_short_oi",
    ]


@pytest.mark.parametrize("col", _MINI_FUTURES_OI_COLS)
def test_mini_futures_oi_column_in_create_table(col: str) -> None:
    assert re.search(rf"^\s*{col}\s+BIGINT,", _create_table_body("market_daily"), re.M)


@pytest.mark.parametrize("col", _MINI_FUTURES_OI_COLS)
def test_mini_futures_oi_column_has_online_migration(col: str) -> None:
    assert re.search(
        rf"ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS {col}\s+BIGINT;", _SCHEMA_SQL
    )
