"""Unit tests for `_build_daily_rows` → `db_utils._build_raw_rows` 接縫。

`_build_daily_rows` 在此之前從未被任何測試呼叫過：limit_up / limit_down 兩個
row dict key 若拼錯，不會拋錯，只會靜默寫 NULL，而測試永遠不會紅
（因為 `db_utils._safe` 會把 KeyError 造成的 `.get()` None 值原樣接受）。
這裡鎖住兩件事：
1. `_build_daily_rows` 真的會把算出的 limit_up/limit_down 放進輸出 DataFrame。
2. 這個 DataFrame 餵進 `_build_raw_rows` 後，這兩欄落在參數列表裡正確的位置
   （用 `_RAW_COLUMNS.index(...)` 動態取得，不寫死索引）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from tw_stock_rawdata import db_utils, run

DATE = dt.date(2026, 8, 12)

_EMPTY_WITH_SYMBOL = pd.DataFrame(columns=["symbol"])
_EMPTY_TPEX_QUOTES = pd.DataFrame(
    columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
)


def _day_all_row(symbol: str, close: float) -> dict:
    return {
        "symbol": symbol, "name": "測試股", "open": close, "close": close,
        "high": close, "low": close, "volume": 1000,
    }


def test_build_daily_rows_then_build_raw_rows_places_limits_correctly() -> None:
    # 2330：day_all 給收盤 2435、MI_INDEX 給 change 20 → 參考價 2415，
    # 對齊 test_price_limit.py 的 calc_limits(2415) == (2655, 2175)。
    # 3605：day_all 給收盤，MI_INDEX 沒有這檔（缺 change）→ 算不出參考價，limit 應為 None。
    holdings = pd.DataFrame([{"symbol": "2330"}, {"symbol": "3605"}])
    twse_day_all = pd.DataFrame(
        [_day_all_row("2330", 2435.0), _day_all_row("3605", 120.0)]
    )
    twse_mi_index = pd.DataFrame(
        [{
            "symbol": "2330", "name": "台積電", "open": 2405.0, "close": 2435.0,
            "high": 2440.0, "low": 2400.0, "volume": 1000, "change": 20.0,
        }]
    )

    result = run._build_daily_rows(
        session=None,
        date=DATE,
        holdings=holdings,
        twse_3insti=_EMPTY_WITH_SYMBOL,
        twse_day_all=twse_day_all,
        twse_mi_index=twse_mi_index,
        tpex_quotes=_EMPTY_TPEX_QUOTES,
        tpex_3insti=_EMPTY_WITH_SYMBOL,
        twse_month_cache={},
    )

    assert set(result["symbol"]) == {"2330", "3605"}
    by_symbol = result.set_index("symbol")
    assert by_symbol.loc["2330", "limit_up"] == Decimal("2655")
    assert by_symbol.loc["2330", "limit_down"] == Decimal("2175")
    assert by_symbol.loc["3605", "limit_up"] is None
    assert by_symbol.loc["3605", "limit_down"] is None

    rows = db_utils._build_raw_rows(DATE, result)
    up_idx = db_utils._RAW_COLUMNS.index("limit_up")
    down_idx = db_utils._RAW_COLUMNS.index("limit_down")

    by_symbol_row = {r[0]: r for r in rows}
    assert isinstance(by_symbol_row["2330"][up_idx], Decimal)
    assert by_symbol_row["2330"][up_idx] == Decimal("2655")
    assert isinstance(by_symbol_row["2330"][down_idx], Decimal)
    assert by_symbol_row["2330"][down_idx] == Decimal("2175")
    assert by_symbol_row["3605"][up_idx] is None
    assert by_symbol_row["3605"][down_idx] is None
