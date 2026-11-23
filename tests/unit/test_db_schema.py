"""Unit tests: db.py 的 schema DDL。

market_daily 是線上已存在的表，新欄位必須同時出現在 CREATE TABLE（全新 DB）與
idempotent 的 ALTER TABLE ... ADD COLUMN IF NOT EXISTS（既有 DB）。
"""

from __future__ import annotations

import re

import pytest

from tw_stock_rawdata.db import _SCHEMA_SQL
from tw_stock_rawdata.db_utils import _FUTURES_OI_COLS, _VIX_COL_TYPES


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
