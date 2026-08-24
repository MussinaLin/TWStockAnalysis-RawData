"""Unit tests: db_utils 的唯讀查詢與兩個尚無覆蓋的寫入函式。

docs/refactor-plan.md X3 的前置作業。這七個函式原本覆蓋率都在 20% 以下
（只有 def 那行被執行），而 X3 要動的正是它們共有的連線樣板，
沒有測試就沒辦法證明重構前後行為一致。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils

URL = "postgres://x"


def _pool(monkeypatch, **kw):
    cur = FakeCursor(**kw)
    conn = install_fake_pool(monkeypatch, db_utils, cur)
    return cur, conn


class TestGetConfigValue:
    def test_returns_value(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch, fetch_results=[("42",)])
        assert db_utils.get_config_value(URL, "k") == "42"
        sql, params = cur.executed[0]
        assert "FROM config WHERE key" in sql
        assert params == ("k",)

    def test_missing_key_returns_none(self, monkeypatch) -> None:
        _pool(monkeypatch, fetch_results=[])
        assert db_utils.get_config_value(URL, "nope") is None


class TestLoadStockNames:
    def test_builds_mapping(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch, fetchall_rows=[("2330", "台積電"), ("2317", "鴻海")])
        assert db_utils.load_stock_names(URL) == {"2330": "台積電", "2317": "鴻海"}
        assert "name != ''" in cur.executed[0][0]

    def test_empty_returns_empty_dict(self, monkeypatch) -> None:
        _pool(monkeypatch, fetchall_rows=[])
        assert db_utils.load_stock_names(URL) == {}


class TestLoadStockShares:
    def test_casts_to_int(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch, fetchall_rows=[("2330", "25930380458")])
        assert db_utils.load_stock_shares(URL) == {"2330": 25930380458}
        assert "issued_shares IS NOT NULL" in cur.executed[0][0]


class TestGetEnabledStocks:
    def test_returns_tuples_with_normalized_market_type(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch, fetchall_rows=[
            ("2330", "台積電", 24, "半導體", " TWSE "),
            ("6488", "環球晶", 24, "半導體", None),
        ])
        out = db_utils.get_enabled_stocks(URL)
        assert out[0] == ("2330", "台積電", 24, "半導體", "twse")
        assert out[1][4] is None
        assert "enabled = TRUE" in cur.executed[0][0]

    def test_blank_market_type_becomes_none(self, monkeypatch) -> None:
        _pool(monkeypatch, fetchall_rows=[("2330", "台積電", 24, "半導體", "   ")])
        assert db_utils.get_enabled_stocks(URL)[0][4] is None


class TestLoadMarketTypes:
    def test_normalizes_and_drops_blanks(self, monkeypatch) -> None:
        _pool(monkeypatch, fetchall_rows=[
            ("2330", "TWSE"), ("6488", " tpex "), ("9999", "   "),
        ])
        assert db_utils.load_market_types(URL) == {"2330": "twse", "6488": "tpex"}


class TestUpsertStockShares:
    def test_empty_df_skips_db(self, monkeypatch) -> None:
        cur, conn = _pool(monkeypatch)
        db_utils.upsert_stock_shares(URL, pd.DataFrame())
        assert cur.executed_many == []
        assert not conn.committed

    def test_blank_symbols_filtered(self, monkeypatch) -> None:
        cur, conn = _pool(monkeypatch)
        df = pd.DataFrame([
            {"symbol": " 2330 ", "name": "台積電", "issued_shares": 100},
            {"symbol": "", "name": "x", "issued_shares": 1},
        ])
        db_utils.upsert_stock_shares(URL, df)
        _sql, params = cur.executed_many[0]
        assert len(params) == 1
        assert params[0][0] == "2330"
        assert conn.committed

    def test_nan_name_becomes_empty_string(self, monkeypatch) -> None:
        """名稱為 NaN 時寫空字串——SQL 的 CASE 靠它保留 DB 既有股名。"""
        cur, _ = _pool(monkeypatch)
        df = pd.DataFrame([{"symbol": "2330", "name": float("nan"), "issued_shares": 100}])
        db_utils.upsert_stock_shares(URL, df)
        assert cur.executed_many[0][1][0][1] == ""

    def test_conflict_keeps_existing_name_when_new_is_blank(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch)
        df = pd.DataFrame([{"symbol": "2330", "name": "台積電", "issued_shares": 100}])
        db_utils.upsert_stock_shares(URL, df)
        sql, _ = cur.executed_many[0]
        assert "ELSE stocks.name END" in sql
        assert "issued_shares = EXCLUDED.issued_shares" in sql


class TestUpsertMarketDaily:
    def test_all_columns_present_even_when_data_partial(self, monkeypatch) -> None:
        """缺的欄位補 None——搭配 COALESCE 才不會覆寫舊值。"""
        cur, conn = _pool(monkeypatch)
        db_utils.upsert_market_daily(URL, dt.date(2026, 8, 21), {"taiex_close": 24100.0})
        sql, params = cur.executed[0]
        assert params["taiex_close"] == 24100.0
        assert params["total_volume"] is None
        assert params["trade_date"] == dt.date(2026, 8, 21)
        assert conn.committed

    def test_every_non_key_column_uses_coalesce(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch)
        db_utils.upsert_market_daily(URL, dt.date(2026, 8, 21), {})
        sql, _ = cur.executed[0]
        for col in ["taiex_open", "taiex_high", "taiex_low", "taiex_close",
                    "total_volume", "margin_balance", "margin_balance_change",
                    "foreign_net"]:
            assert f"{col} = COALESCE(EXCLUDED.{col}, market_daily.{col})" in sql
        assert "trade_date = COALESCE" not in sql

    def test_unknown_keys_are_dropped(self, monkeypatch) -> None:
        cur, _ = _pool(monkeypatch)
        db_utils.upsert_market_daily(URL, dt.date(2026, 8, 21), {"bogus": 1})
        _sql, params = cur.executed[0]
        assert "bogus" not in params
