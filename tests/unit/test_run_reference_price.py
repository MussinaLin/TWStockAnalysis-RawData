"""Unit tests for _reference_price — 由收盤價與漲跌價差推當日參考價。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from tw_stock_rawdata import run


class TestReferencePrice:
    def test_positive_change(self) -> None:
        # 2330 2026-08-12：收 2415.00、漲跌 +20.00 → 參考價 2395
        assert run._reference_price(2415.0, 20.0) == Decimal("2395")

    def test_negative_change(self) -> None:
        assert run._reference_price(100.0, -5.0) == Decimal("105")

    def test_none_change_returns_none(self) -> None:
        """除權息日：交易所未提供漲跌 → 不以前一交易日收盤推測。"""
        assert run._reference_price(120.0, None) is None

    def test_none_close_returns_none(self) -> None:
        assert run._reference_price(None, 1.0) is None

    def test_nan_is_rejected(self) -> None:
        """pandas 會把含 None 的 float 欄位轉成 NaN，必須擋掉。"""
        nan = float("nan")
        assert run._reference_price(120.0, nan) is None
        assert run._reference_price(nan, 1.0) is None

    def test_returns_decimal_without_float_error(self) -> None:
        result = run._reference_price(0.3, 0.1)
        assert isinstance(result, Decimal)
        assert result == Decimal("0.2")

    def test_accepts_numpy_scalars(self) -> None:
        """pandas 取值回來的是 numpy 純量，不是 Python float。"""
        row = pd.Series({"close": 2415.0, "change": 20.0})
        assert run._reference_price(row["close"], row["change"]) == Decimal("2395")
