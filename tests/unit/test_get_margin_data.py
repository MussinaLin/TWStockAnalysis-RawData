"""Unit tests: 逐檔融資融券查表（_get_margin_data）。

docs/refactor-plan.md T4。原覆蓋率 21%、CC 15、fan-in 1。

關鍵語意是「TWSE 命中即返回」：只要該檔出現在 TWSE 整批裡就直接回傳，
即使其中有欄位是 NaN 也不會再去 TPEX 補——上市股不該拿上櫃資料補洞。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tw_stock_rawdata.run import _get_margin_data

KEYS = ["margin_buy", "margin_sell", "margin_balance", "margin_change",
        "short_sell", "short_buy", "short_balance", "short_change",
        "short_margin_ratio"]


def _frame(symbol: str, **vals) -> pd.DataFrame:
    row = {"symbol": symbol}
    for k in KEYS:
        row[k] = vals.get(k, 100)
    row["short_margin_ratio"] = vals.get("short_margin_ratio", 0.25)
    return pd.DataFrame([row])


def test_returns_all_none_when_both_sources_missing() -> None:
    out = _get_margin_data("2330", None, None)
    assert out == {k: None for k in KEYS}


def test_returns_all_none_when_symbol_absent() -> None:
    out = _get_margin_data("9999", _frame("2330"), _frame("6488"))
    assert out == {k: None for k in KEYS}


def test_twse_hit_returns_values() -> None:
    out = _get_margin_data("2330", _frame("2330", margin_buy=500), None)
    assert out["margin_buy"] == 500
    assert out["short_margin_ratio"] == pytest.approx(0.25)


def test_tpex_used_when_twse_has_no_such_symbol() -> None:
    out = _get_margin_data("6488", _frame("2330"), _frame("6488", margin_buy=77))
    assert out["margin_buy"] == 77


def test_empty_dataframes_are_skipped() -> None:
    empty = pd.DataFrame(columns=["symbol", *KEYS])
    out = _get_margin_data("6488", empty, _frame("6488", margin_buy=42))
    assert out["margin_buy"] == 42


def test_twse_hit_short_circuits_tpex() -> None:
    """TWSE 命中即返回：即使該列有 NaN，也不拿 TPEX 的值來補。"""
    twse = _frame("2330", margin_buy=np.nan)
    tpex = _frame("2330", margin_buy=999)
    out = _get_margin_data("2330", twse, tpex)
    assert out["margin_buy"] is None


def test_nan_values_become_none() -> None:
    out = _get_margin_data("2330", _frame("2330", short_balance=np.nan), None)
    assert out["short_balance"] is None


def test_missing_column_becomes_none() -> None:
    df = _frame("2330").drop(columns=["short_change"])
    out = _get_margin_data("2330", df, None)
    assert out["short_change"] is None


def test_int_and_float_typing() -> None:
    """short_margin_ratio 轉 float，其餘轉 int——DB 欄位型別不同。"""
    out = _get_margin_data("2330", _frame("2330", margin_buy=500.0), None)
    assert isinstance(out["margin_buy"], int)
    assert isinstance(out["short_margin_ratio"], float)


def test_twse_present_but_symbol_only_in_tpex() -> None:
    out = _get_margin_data("6488", _frame("2330", margin_buy=1), _frame("6488", margin_buy=2))
    assert out["margin_buy"] == 2


def test_tpex_none_after_twse_miss_returns_nulls() -> None:
    out = _get_margin_data("6488", _frame("2330"), None)
    assert out == {k: None for k in KEYS}
