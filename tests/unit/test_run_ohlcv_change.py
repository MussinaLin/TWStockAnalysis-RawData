"""Unit tests for _fetch_ohlcv_with_fallback 的 change 來源規則。

change 只從 MI_INDEX / TPEX quotes 取，永遠不從 STOCK_DAY_ALL 取
（後者在除權息日給 0.0000 且無標記）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata import run

DATE = dt.date(2026, 8, 12)


def _day_all(symbol: str, close: float, change: float) -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "symbol": symbol, "name": "宏致", "open": 111.0, "close": close,
            "high": 120.0, "low": 111.0, "volume": 8422797, "change": change,
        }]
    )


def _mi_index(symbol: str, close: float, change: float | None) -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "symbol": symbol, "name": "宏致", "open": 111.0, "close": close,
            "high": 120.0, "low": 111.0, "volume": 8422797, "change": change,
        }]
    )


def _empty_tpex() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
    )


def test_change_is_never_taken_from_stock_day_all() -> None:
    """day_all 有 change 欄但 MI_INDEX 缺席 → change 必須是 None，close 仍取自 day_all。

    prepare_twse_day_all 目前刻意不吐 change 欄，這裡手建帶 change 的 frame，
    是為了鎖住「即使上游哪天真的給了，這裡也必須忽略」的不變量。
    """
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="3605",
        twse_day_all=_day_all("3605", 120.0, 0.0),
        twse_mi_index=None,
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert result.close == 120.0
    assert result.change is None


def test_change_comes_from_mi_index() -> None:
    """close 取自 day_all、change 取自 MI_INDEX —— 跨來源組合是刻意且安全的。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=_day_all("2330", 2415.0, 0.0),
        twse_mi_index=_mi_index("2330", 2415.0, 20.0),
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert result.close == 2415.0
    assert result.change == 20.0


def test_change_from_tpex_quotes() -> None:
    tpex = pd.DataFrame(
        [{
            "symbol": "6488", "name": "環球晶", "open": 44.2, "close": 45.21,
            "high": 45.26, "low": 44.2, "volume": 508551, "change": 1.41,
        }]
    )
    # day_all 與 mi_index 皆缺 → 會進 STOCK_DAY 逐檔月表區塊。預先塞 cache 讓它
    # 不會用 session=None 發 HTTP（該區塊只 catch DataUnavailableError）。
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="6488",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=tpex,
        twse_month_cache={("6488", dt.date(2026, 8, 1)): pd.DataFrame()},
    )
    assert result.close == 45.21
    assert result.change == 1.41


def test_result_is_named_tuple() -> None:
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=_day_all("2330", 2415.0, 0.0),
        twse_mi_index=_mi_index("2330", 2415.0, 20.0),
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert isinstance(result, run.OhlcvResult)
    assert result.open == 111.0
    assert result.high == 120.0
    assert result.low == 111.0
    assert result.volume == 8422797
