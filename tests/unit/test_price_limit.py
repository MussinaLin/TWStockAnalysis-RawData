"""Unit tests for price_limit — 台股升降單位級距與漲跌停換算。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tw_stock_rawdata.price_limit import calc_limits, tick_size


class TestTickSize:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.01"),
            ("9.99", "0.01"),
            ("10", "0.05"),
            ("49.95", "0.05"),
            ("50", "0.1"),
            ("99.9", "0.1"),
            ("100", "0.5"),
            ("499.5", "0.5"),
            ("500", "1"),
            ("999", "1"),
            ("1000", "5"),
            ("2656.5", "5"),
        ],
    )
    def test_band_boundaries(self, price: str, expected: str) -> None:
        assert tick_size(Decimal(price)) == Decimal(expected)


class TestCalcLimits:
    def test_limit_up_floors_to_tick(self) -> None:
        # 2415 × 1.1 = 2656.5；1000 元以上檔位 5 → 無條件捨去 → 2655
        up, _ = calc_limits(Decimal("2415"))
        assert up == Decimal("2655")

    def test_limit_down_ceils_to_tick(self) -> None:
        # 2415 × 0.9 = 2173.5；檔位 5 → 無條件進位 → 2175
        _, down = calc_limits(Decimal("2415"))
        assert down == Decimal("2175")

    def test_regression_3605_ex_rights_day(self) -> None:
        """3605 宏致 2026-08-13：除息後參考價 120，當日實際收 132.00 漲停。"""
        up, down = calc_limits(Decimal("120"))
        assert up == Decimal("132.0")
        assert down == Decimal("108.0")

    def test_crosses_band_upward(self) -> None:
        # 9.5 × 1.1 = 10.45，落在 10~50 區間（檔位 0.05）而非參考價所屬的 0.01 檔
        up, _ = calc_limits(Decimal("9.5"))
        assert up == Decimal("10.45")

    def test_none_ref_returns_none_pair(self) -> None:
        assert calc_limits(None) == (None, None)

    def test_returns_decimal_not_float(self) -> None:
        up, down = calc_limits(Decimal("2415"))
        assert isinstance(up, Decimal)
        assert isinstance(down, Decimal)

    @pytest.mark.parametrize(
        "ref", ["9.5", "10", "45.2", "66.1", "120", "499", "500", "999", "2415"]
    )
    def test_limits_land_on_tick_multiples(self, ref: str) -> None:
        up, down = calc_limits(Decimal(ref))
        assert up % tick_size(up) == 0
        assert down % tick_size(down) == 0
