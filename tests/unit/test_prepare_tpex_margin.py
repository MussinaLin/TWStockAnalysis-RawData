"""Unit tests: TPEX 融資融券 normalize。

補這組測試的直接動機是 docs/refactor-plan.md 的 D1——prepare_tpex_margin 內
三處 `and X in df.columns` 是冗餘檢查（cols 的值取自 df_cols_lower，本來就
必定是 df 的欄位或 None），要移除它們需要先有回歸保護。本檔覆蓋欄位齊全、
選填欄位缺席、必填欄位缺席、大小寫不一致等情境。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tw_stock_rawdata.prepare import prepare_tpex_margin
from tw_stock_rawdata.sources import DataUnavailableError

FULL_COLUMNS = {
    "SecuritiesCompanyCode": ["6488", "3105"],
    "MarginPurchase": ["1,000", "500"],
    "MarginSales": ["300", "200"],
    "CashRedemption": ["100", "50"],
    "MarginPurchaseBalance": ["5,000", "2,000"],
    "ShortSale": ["80", "40"],
    "ShortCovering": ["30", "10"],
    "StockRedemption": ["5", "0"],
    "ShortSaleBalance": ["200", "100"],
}


def _df(**overrides) -> pd.DataFrame:
    data = dict(FULL_COLUMNS)
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return pd.DataFrame(data)


def test_full_columns_parses_all_fields() -> None:
    out = prepare_tpex_margin(_df())
    row = out.iloc[0]
    assert row["symbol"] == "6488"
    assert row["margin_buy"] == 1000
    assert row["margin_sell"] == 300
    assert row["margin_balance"] == 5000
    assert row["short_sell"] == 80
    assert row["short_buy"] == 30
    assert row["short_balance"] == 200
    # margin_change = buy - sell - cash_repay
    assert row["margin_change"] == 1000 - 300 - 100
    # short_change = sell - buy - stock_repay
    assert row["short_change"] == 80 - 30 - 5


def test_missing_cash_redemption_skips_that_subtraction() -> None:
    """選填欄位缺席時 margin_change 不減現償，而不是變成 None。"""
    out = prepare_tpex_margin(_df(CashRedemption=None))
    assert out.iloc[0]["margin_change"] == 1000 - 300


def test_missing_stock_redemption_skips_that_subtraction() -> None:
    out = prepare_tpex_margin(_df(StockRedemption=None))
    assert out.iloc[0]["short_change"] == 80 - 30


def test_missing_margin_buy_column_yields_none() -> None:
    """必填欄位缺席時該欄與其衍生的 margin_change 皆為 None。"""
    out = prepare_tpex_margin(_df(MarginPurchase=None))
    assert out.iloc[0]["margin_buy"] is None
    assert out["margin_change"].isna().all() or out.iloc[0]["margin_change"] is None


def test_missing_short_sell_column_yields_none() -> None:
    out = prepare_tpex_margin(_df(ShortSale=None))
    assert out.iloc[0]["short_sell"] is None
    assert out["short_change"].isna().all() or out.iloc[0]["short_change"] is None


def test_column_matching_is_case_insensitive() -> None:
    """欄位比對走 df_cols_lower，端點改大小寫不應該讓解析失敗。"""
    data = {k.upper(): v for k, v in FULL_COLUMNS.items()}
    out = prepare_tpex_margin(pd.DataFrame(data))
    assert out.iloc[0]["symbol"] == "6488"
    assert out.iloc[0]["margin_buy"] == 1000


def test_missing_symbol_column_raises() -> None:
    with pytest.raises(DataUnavailableError, match="缺少 symbol"):
        prepare_tpex_margin(_df(SecuritiesCompanyCode=None))


def test_symbol_is_stripped_string() -> None:
    out = prepare_tpex_margin(_df(SecuritiesCompanyCode=["  6488 ", "3105"]))
    assert out.iloc[0]["symbol"] == "6488"


def test_unparseable_numbers_become_na() -> None:
    """_clean_int 對無法解析的值回 None，但與同欄的 int 混在一個 Series 裡會被
    pandas 強制成 float NaN——下游 db_utils._safe 會再把 NaN 轉回 None 才寫 DB。"""
    out = prepare_tpex_margin(_df(MarginPurchase=["--", "500"]))
    assert pd.isna(out.iloc[0]["margin_buy"])
    assert out.iloc[1]["margin_buy"] == 500
