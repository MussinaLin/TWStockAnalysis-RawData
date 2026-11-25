"""Unit tests: etfs / etf_daily 的讀寫（get_enabled_etfs、upsert_etf_daily）。

etf_daily 一列由多個來源各寫一部分欄位（daily 寫規模、--dahu 寫受益人數、
月規模回補只寫 aum_100m），所以每個值欄都必須 COALESCE，缺鍵視為 NULL 不覆寫。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils

URL = "postgres://x"
D = dt.date(2026, 11, 20)


def _run(monkeypatch, rows):
    cur = FakeCursor()
    conn = install_fake_pool(monkeypatch, db_utils, cur)
    n = db_utils.upsert_etf_daily(URL, rows)
    return n, cur, conn


def test_upsert_coalesces_every_value_column(monkeypatch) -> None:
    n, cur, conn = _run(monkeypatch, [{"symbol": "0050", "trade_date": D, "holders": 4038137}])
    sql, params = cur.executed_many[0]
    for col in ("units", "nav", "aum_100m", "holders"):
        assert f"{col} = COALESCE(EXCLUDED.{col}, etf_daily.{col})" in sql
    assert "= EXCLUDED." not in sql.replace("COALESCE(EXCLUDED.", "")
    assert params == [("0050", D, None, None, None, 4038137)]
    assert n == 1 and conn.committed


def test_upsert_passes_all_value_columns_in_order(monkeypatch) -> None:
    row = {"symbol": " 0050 ", "trade_date": D, "units": 26433000000,
           "nav": Decimal("94.8800"), "aum_100m": Decimal("25079.63")}
    _, cur, _ = _run(monkeypatch, [row])
    _, params = cur.executed_many[0]
    assert params == [("0050", D, 26433000000, Decimal("94.8800"), Decimal("25079.63"), None)]


def test_upsert_skips_empty_and_blank_symbols(monkeypatch) -> None:
    assert _run(monkeypatch, [])[0] == 0
    n, cur, conn = _run(monkeypatch, [{"symbol": "  ", "trade_date": D}])
    assert n == 0 and cur.executed_many == [] and not conn.committed


def test_get_enabled_etfs(monkeypatch) -> None:
    cur = FakeCursor(fetchall_rows=[("0050", "元大台灣50")])
    install_fake_pool(monkeypatch, db_utils, cur)
    assert db_utils.get_enabled_etfs(URL) == [("0050", "元大台灣50")]
    assert "WHERE enabled = TRUE" in cur.executed[0][0]
