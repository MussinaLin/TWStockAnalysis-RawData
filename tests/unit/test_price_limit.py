"""Unit tests for price_limit — 台股升降單位級距與漲跌停換算。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tw_stock_rawdata.price_limit import calc_limits, is_etf, tick_size


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


class TestIsEtf:
    @pytest.mark.parametrize("symbol", ["00981A", "0050", "006208", "00631L", " 00981A "])
    def test_00_prefix_is_etf(self, symbol: str) -> None:
        assert is_etf(symbol) is True

    @pytest.mark.parametrize("symbol", ["2330", "1101", "6488", "9958", "", None])
    def test_other_symbols_are_not_etf(self, symbol) -> None:
        assert is_etf(symbol) is False


class TestEtfTickSize:
    """ETF 升降單位只有兩級：未滿 50 元 0.01、50 元以上 0.05（TWSE ETF 交易制度）。"""

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.01"),
            ("10", "0.01"),
            ("49.99", "0.01"),
            ("50", "0.05"),
            ("150", "0.05"),
            ("1000", "0.05"),
        ],
    )
    def test_band_boundaries(self, price: str, expected: str) -> None:
        assert tick_size(Decimal(price), etf=True) == Decimal(expected)


class TestEtfCalcLimits:
    def test_uses_etf_ticks_in_10_to_50_band(self) -> None:
        # 20.13 × 1.1 = 22.143 → 0.01 捨去 → 22.14；× 0.9 = 18.117 → 0.01 進位 → 18.12。
        # 普通股在這個價位用 0.05，會算成 (22.10, 18.15)。
        assert calc_limits(Decimal("20.13"), etf=True) == (
            Decimal("22.14"), Decimal("18.12"),
        )
        assert calc_limits(Decimal("20.13")) == (Decimal("22.10"), Decimal("18.15"))

    def test_crosses_50_band_upward(self) -> None:
        # 45.6 × 1.1 = 50.16，落在 50 元以上（0.05）→ 50.15；× 0.9 = 41.04（0.01）。
        assert calc_limits(Decimal("45.6"), etf=True) == (
            Decimal("50.15"), Decimal("41.04"),
        )

    def test_none_ref_returns_none_pair(self) -> None:
        assert calc_limits(None, etf=True) == (None, None)
