"""_price_limits：算出的漲跌停區間若無拘束力就寫 NULL。

新上市櫃前五日等標的無漲跌幅限制，交易所照樣給漲跌價差，用 ±10% 算出來的區間
是假的。實際成交價落在區間外就是鐵證 —— 有漲跌幅限制時不可能成交在區間外。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tw_stock_rawdata import run


class TestPriceLimitsBinding:
    def test_normal_row_keeps_limits(self) -> None:
        # 2330 2026-08-13：收 2435、漲跌 +20 → 參考價 2415
        up, down = run._price_limits(2435.0, 20.0, 2445.0, 2425.0)
        assert (up, down) == (Decimal("2655"), Decimal("2175"))

    def test_high_above_limit_up_voids_band(self) -> None:
        """4749 新應材 2025-01-17 掛牌首日：櫃買公告次日漲停 9995（無漲跌幅限制）。

        承銷參考價 480 → 我們會算出漲停 528，但當日最高衝到 691。
        """
        up, down = run._price_limits(688.0, 208.0, 691.0, 657.0)
        assert (up, down) == (None, None)

    def test_low_below_limit_down_voids_band(self) -> None:
        """4749 2025-02-03：參考價 734 → 跌停 661，但當日最低 650。"""
        up, down = run._price_limits(678.0, -56.0, 689.0, 650.0)
        assert (up, down) == (None, None)

    def test_close_outside_band_voids_even_without_high_low(self) -> None:
        """來源沒給最高/最低時，收盤價本身仍能否證。"""
        up, down = run._price_limits(688.0, 208.0, None, None)
        assert (up, down) == (None, None)

    def test_touching_the_limit_is_still_valid(self) -> None:
        """收盤剛好等於漲停價是漲停，不是區間失效 —— 不可誤殺。"""
        up, down = run._price_limits(132.0, 12.0, 132.0, 123.0)
        assert up == Decimal("132.0")
        assert down == Decimal("108.0")

    @pytest.mark.parametrize("missing", [None, float("nan")])
    def test_missing_high_low_is_tolerated(self, missing) -> None:
        up, down = run._price_limits(2435.0, 20.0, missing, missing)
        assert (up, down) == (Decimal("2655"), Decimal("2175"))

    def test_no_reference_price_stays_none(self) -> None:
        assert run._price_limits(120.0, None, 120.0, 111.0) == (None, None)
        assert run._price_limits(None, 1.0, None, None) == (None, None)
