"""Unit tests: RowSourceProvider 介面與 BatchSourceProvider。

BatchSourceProvider 只是把現行的自由函式包起來，必須零行為變更 —— 這裡逐一
比對 provider 的回傳與直接呼叫自由函式的結果。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata import run

DATE = dt.date(2026, 8, 19)

_EMPTY_WITH_SYMBOL = pd.DataFrame(columns=["symbol"])
_EMPTY_TPEX_QUOTES = pd.DataFrame(
    columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
)


def _mi_index_row(symbol: str) -> dict:
    return {
        "symbol": symbol, "name": "台積電", "open": 100.0, "close": 105.0,
        "high": 106.0, "low": 99.0, "volume": 1_234_000, "change": 5.0,
    }


def _provider(**kwargs) -> run.BatchSourceProvider:
    defaults = dict(
        session=None,
        twse_3insti=_EMPTY_WITH_SYMBOL,
        twse_day_all=None,
        twse_mi_index=None,
        tpex_quotes=_EMPTY_TPEX_QUOTES,
        tpex_3insti=_EMPTY_WITH_SYMBOL,
        twse_month_cache={},
        twse_insti_ok=True,
        tpex_insti_ok=True,
    )
    defaults.update(kwargs)
    return run.BatchSourceProvider(**defaults)


def test_batch_provider_ohlcv_matches_free_function() -> None:
    mi = pd.DataFrame([_mi_index_row("2330")])
    provider = _provider(twse_mi_index=mi)

    got = provider.ohlcv("2330", DATE, "twse")
    expected = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=None, twse_mi_index=mi,
        tpex_quotes=_EMPTY_TPEX_QUOTES, twse_month_cache={},
        market_type="twse",
    )
    assert got == expected


def test_batch_provider_insti_matches_free_function() -> None:
    twse_3insti = pd.DataFrame([{
        "symbol": "2330", "foreign_net": 9_039_647,
        "trust_net": -1_292_952, "dealer_net": 1_611_836,
    }])
    provider = _provider(twse_3insti=twse_3insti)

    assert provider.insti("2330", DATE) == run._get_institutional_data(
        "2330", twse_3insti, _EMPTY_WITH_SYMBOL
    )


def test_batch_provider_is_tpex_uses_todays_tpex_quotes() -> None:
    """batch 模式的 is_tpex 反映「今天價格誰供應的」，來源是當日 tpex_quotes，
    刻意不看 stocks.market_type —— 這是既有設計，不可改。"""
    quotes = pd.DataFrame([{
        "symbol": "3105", "name": "穩懋", "open": 1.0, "close": 1.0,
        "high": 1.0, "low": 1.0, "volume": 1, "change": 0.0,
    }])
    provider = _provider(tpex_quotes=quotes)

    assert provider.is_tpex("3105", market_type=None) is True
    assert provider.is_tpex("2330", market_type="tpex") is False


def test_batch_provider_insti_ok_follows_market() -> None:
    provider = _provider(twse_insti_ok=False, tpex_insti_ok=True)
    quotes = pd.DataFrame([{
        "symbol": "3105", "name": "穩懋", "open": 1.0, "close": 1.0,
        "high": 1.0, "low": 1.0, "volume": 1, "change": 0.0,
    }])
    provider_tpex = _provider(
        twse_insti_ok=False, tpex_insti_ok=True, tpex_quotes=quotes
    )

    assert provider.insti_ok("2330", "twse") is False
    assert provider_tpex.insti_ok("3105", "tpex") is True


def test_build_daily_rows_accepts_provider() -> None:
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])
    mi = pd.DataFrame([_mi_index_row("2330")])

    result = run._build_daily_rows(
        date=DATE,
        holdings=holdings,
        provider=_provider(twse_mi_index=mi),
    )

    assert len(result) == 1
    assert result.iloc[0]["close"] == 105.0
    assert result.iloc[0]["volume"] == 1234  # 股 // 1000


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
