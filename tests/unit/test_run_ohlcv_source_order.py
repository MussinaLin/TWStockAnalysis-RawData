"""Unit tests for _fetch_ohlcv_with_fallback 的來源順序與市場別 gating。

不變量：逐檔 HTTP（TWSE STOCK_DAY 月表）是最後手段。
- 全市場批次來源（STOCK_DAY_ALL / MI_INDEX / TPEX quotes）都排在它前面，
  任何一個補滿 OHLCV 就不該再逐檔打 API。
- 上櫃股（market_type == "tpex"）永遠不打 TWSE 月表 —— 那支 API 沒有上櫃資料，
  打了必定拿到「很抱歉，沒有符合條件的資料!」，只是白白消耗 TWSE 的限流配額。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run

DATE = dt.date(2026, 8, 19)
MONTH = dt.date(2026, 8, 1)


@pytest.fixture
def stock_day_spy(monkeypatch):
    """攔截 run.fetch_twse_stock_day，記錄被呼叫的 symbol。"""
    calls: list[str] = []

    def _fake(session, stock_no, date):  # noqa: ANN001 - 測試替身
        calls.append(stock_no)
        return pd.DataFrame()

    monkeypatch.setattr(run, "fetch_twse_stock_day", _fake)
    return calls


def _full_row(symbol: str, name: str) -> dict:
    return {
        "symbol": symbol, "name": name, "open": 100.0, "close": 105.0,
        "high": 106.0, "low": 99.0, "volume": 1234000, "change": 5.0,
    }


def _empty_quotes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
    )


def test_tpex_symbol_never_calls_stock_day(stock_day_spy) -> None:
    """上櫃股由 TPEX quotes 供應，不得觸發 TWSE 月表逐檔 HTTP。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="3105",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=pd.DataFrame([_full_row("3105", "穩懋")]),
        twse_month_cache={},
        market_type="tpex",
    )
    assert stock_day_spy == []
    assert result.close == 105.0
    assert result.volume == 1234000


def test_tpex_symbol_skips_stock_day_even_without_tpex_quotes(stock_day_spy) -> None:
    """TPEX quotes 當日失敗時，上櫃股仍不該去問 TWSE 月表（那裡永遠沒有上櫃資料）。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="3105",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        twse_month_cache={},
        market_type="tpex",
    )
    assert stock_day_spy == []
    assert result.close is None


def test_twse_symbol_served_by_mi_index_without_stock_day(stock_day_spy) -> None:
    """STOCK_DAY_ALL 不可用時改由 MI_INDEX 補滿，不得退化成逐檔 HTTP。

    這正是 2026-08-19 的情境：STOCK_DAY_ALL 無法解析日期 → twse_day_all=None，
    舊順序會讓 217 檔全部各打一次 TWSE 月表。
    """
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=None,
        twse_mi_index=pd.DataFrame([_full_row("2330", "台積電")]),
        tpex_quotes=_empty_quotes(),
        twse_month_cache={},
        market_type="twse",
    )
    assert stock_day_spy == []
    assert result.open == 100.0
    assert result.close == 105.0
    assert result.high == 106.0
    assert result.low == 99.0
    assert result.volume == 1234000


def test_twse_symbol_falls_back_to_stock_day_as_last_resort(stock_day_spy) -> None:
    """所有批次來源都缺席時，上市股仍保留逐檔月表這條最後手段。"""
    run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        twse_month_cache={},
        market_type="twse",
    )
    assert stock_day_spy == ["2330"]


def test_unknown_market_type_keeps_stock_day_fallback(stock_day_spy) -> None:
    """market_type 未知（如 --backfill-stocks 直接給代號）時維持既有行為，不誤殺。"""
    run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        twse_month_cache={},
        market_type=None,
    )
    assert stock_day_spy == ["2330"]


def test_stock_day_all_alone_is_enough(stock_day_spy) -> None:
    """STOCK_DAY_ALL 正常供應五欄時，不需要任何逐檔 HTTP。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=pd.DataFrame([_full_row("2330", "台積電")]),
        twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        twse_month_cache={},
        market_type="twse",
    )
    assert stock_day_spy == []
    assert result.close == 105.0


def test_build_daily_rows_passes_market_type_through(stock_day_spy) -> None:
    """接線測試：_build_daily_rows 要把 holdings 的 market_type 傳進 fallback。

    上櫃檔（3105）不得觸發逐檔月表；上市檔（2330）在批次來源全缺時仍會觸發。
    """
    holdings = pd.DataFrame([
        {"symbol": "3105", "name": "穩懋", "market_type": "tpex"},
        {"symbol": "2330", "name": "台積電", "market_type": "twse"},
    ])
    insti = pd.DataFrame(
        [{"symbol": "3105", "foreign_net": 0, "trust_net": 0, "dealer_net": 0}]
    )

    run._build_daily_rows(
        session=None, date=DATE, holdings=holdings,
        twse_3insti=insti,
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        tpex_3insti=insti,
        twse_month_cache={},
    )

    assert stock_day_spy == ["2330"]


def test_build_daily_rows_without_market_type_column(stock_day_spy) -> None:
    """holdings 沒有 market_type 欄（--backfill-stocks 舊格式）時不得炸掉。"""
    holdings = pd.DataFrame([{"symbol": "2330", "name": "台積電"}])
    insti = pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"])

    run._build_daily_rows(
        session=None, date=DATE, holdings=holdings,
        twse_3insti=insti,
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=_empty_quotes(),
        tpex_3insti=insti,
        twse_month_cache={},
    )

    assert stock_day_spy == ["2330"]
