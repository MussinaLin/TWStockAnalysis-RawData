"""Tests for 大戶持股佔比 (--dahu) parsing and date resolution."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata.prepare import (
    prepare_tdcc_major_ratio,
    prepare_tdcc_retail_ratio,
)
from tw_stock_rawdata.run import _resolve_dahu_dates

# TDCC 集保戶股權分散表欄位（pd.read_html 解出的真實欄名）
_COLUMNS = ["序", "持股/單位數分級", "人數", "股數/單位數", "占集保庫存數比例 (%)"]

# 6147 頎邦 20260529 真實資料（含 400 張以上的 12~15 級 = 2.40+1.88+1.88+69.56）
_SAMPLE_6147 = pd.DataFrame(
    [
        [1, "1-999", 64332, 5027224, 0.67],
        [2, "1,000-5,000", 40057, 68533493, 9.20],
        [3, "5,001-10,000", 2830, 22618628, 3.03],
        [4, "10,001-15,000", 709, 9173812, 1.23],
        [5, "15,001-20,000", 432, 8002460, 1.07],
        [6, "20,001-30,000", 341, 8765521, 1.17],
        [7, "30,001-40,000", 161, 5756041, 0.77],
        [8, "40,001-50,000", 118, 5506477, 0.73],
        [9, "50,001-100,000", 207, 14960123, 2.00],
        [10, "100,001-200,000", 94, 13723801, 1.84],
        [11, "200,001-400,000", 66, 18580392, 2.49],
        [12, "400,001-600,000", 36, 17891297, 2.40],
        [13, "600,001-800,000", 20, 14047305, 1.88],
        [14, "800,001-1,000,000", 15, 14036971, 1.88],
        [15, "1,000,001以上", 102, 517982994, 69.56],
        [16, "差異數調整（說明4）", None, -13000, -0.00],
        [17, "合　計", 109520, 744593539, 100.00],
    ],
    columns=_COLUMNS,
)


class TestPrepareMajorHolderRatio:
    def test_sample_6147_matches_expected(self):
        """2.40 + 1.88 + 1.88 + 69.56 = 75.72% → 0.7572"""
        assert prepare_tdcc_major_ratio(_SAMPLE_6147) == 0.7572

    def test_excludes_below_threshold(self):
        """只計 >= 400,001 股的級距；200,001-400,000 等不算。"""
        df = pd.DataFrame(
            [
                [1, "200,001-400,000", 10, 100, 30.00],  # excluded (上界 400,000)
                [2, "400,001-600,000", 10, 100, 25.00],  # included
                [3, "1,000,001以上", 10, 100, 15.00],  # included
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) == 0.40

    def test_excludes_summary_and_adjustment_rows(self):
        """差異數調整 / 合計 無數字下界，必須排除。"""
        df = pd.DataFrame(
            [
                [1, "400,001-600,000", 10, 100, 10.00],
                [2, "差異數調整（說明4）", None, -1, -0.00],
                [3, "合　計", 100, 1000, 100.00],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) == 0.10

    def test_genuine_zero_ratio_returns_zero(self):
        """400 張以上級距存在但比例皆 0.00 → 真實的 0.0（非 None）。"""
        df = pd.DataFrame(
            [
                [11, "200,001-400,000", 10, 100, 100.00],  # excluded
                [12, "400,001-600,000", 0, 0, 0.00],
                [13, "1,000,001以上", 0, 0, 0.00],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) == 0.0

    def test_unparseable_percentages_return_none(self):
        """欄位改版 / '--' / 空白 導致沒有任何級距解析出比例 → None（不可寫假 0.0）。"""
        df = pd.DataFrame(
            [
                [12, "400,001-600,000", 10, 100, "--"],
                [13, "1,000,001以上", 10, 100, None],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) is None

    def test_no_qualifying_grade_rows_return_none(self):
        """表結構異常（完全沒有 400 張以上級距）→ None。"""
        df = pd.DataFrame(
            [
                [1, "1-999", 10, 100, 50.00],
                [2, "1,000-5,000", 10, 100, 50.00],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) is None

    def test_missing_columns_returns_none(self):
        df = pd.DataFrame([[1, 2, 3]], columns=["a", "b", "c"])
        assert prepare_tdcc_major_ratio(df) is None

    def test_percent_formatted_string_cells(self):
        """若 TDCC 把比例欄渲染成 '2.40%' 字串，仍要正確解析（不可變 0.0）。"""
        df = pd.DataFrame(
            [
                [12, "400,001-600,000", 10, 100, "10.00%"],
                [13, "1,000,001以上", 10, 100, "15.00%"],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_major_ratio(df) == 0.25


class TestPrepareRetailHolderRatio:
    def test_sample_6147_matches_expected(self):
        """1-999..15,001-20,000 = 0.67+9.20+3.03+1.23+1.07 = 15.20% → 0.1520"""
        assert prepare_tdcc_retail_ratio(_SAMPLE_6147) == 0.1520

    def test_excludes_at_or_above_threshold(self):
        """只計下界 <= 20,000 股的級距；20,001-30,000 等不算。"""
        df = pd.DataFrame(
            [
                [1, "15,001-20,000", 10, 100, 12.00],  # included (上界 20,000)
                [2, "20,001-30,000", 10, 100, 30.00],  # excluded (下界 20,001)
                [3, "1-999", 10, 100, 8.00],  # included
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) == 0.20

    def test_excludes_summary_and_adjustment_rows(self):
        """差異數調整 / 合計 無數字下界，必須排除。"""
        df = pd.DataFrame(
            [
                [1, "1-999", 10, 100, 10.00],
                [2, "差異數調整（說明4）", None, -1, -0.00],
                [3, "合　計", 100, 1000, 100.00],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) == 0.10

    def test_genuine_zero_ratio_returns_zero(self):
        """散戶級距存在但比例皆 0.00 → 真實的 0.0（非 None）。"""
        df = pd.DataFrame(
            [
                [1, "1-999", 0, 0, 0.00],
                [2, "20,001-30,000", 10, 100, 100.00],  # excluded
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) == 0.0

    def test_unparseable_percentages_return_none(self):
        """欄位改版 / '--' / 空白 導致沒有任何級距解析出比例 → None（不可寫假 0.0）。"""
        df = pd.DataFrame(
            [
                [1, "1-999", 10, 100, "--"],
                [2, "1,000-5,000", 10, 100, None],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) is None

    def test_no_qualifying_grade_rows_return_none(self):
        """表結構異常（完全沒有 <= 20,000 股級距）→ None。"""
        df = pd.DataFrame(
            [
                [1, "400,001-600,000", 10, 100, 50.00],
                [2, "1,000,001以上", 10, 100, 50.00],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) is None

    def test_missing_columns_returns_none(self):
        df = pd.DataFrame([[1, 2, 3]], columns=["a", "b", "c"])
        assert prepare_tdcc_retail_ratio(df) is None

    def test_percent_formatted_string_cells(self):
        """若 TDCC 把比例欄渲染成 '8.00%' 字串，仍要正確解析（不可變 0.0）。"""
        df = pd.DataFrame(
            [
                [1, "1-999", 10, 100, "8.00%"],
                [2, "1,000-5,000", 10, 100, "12.00%"],
            ],
            columns=_COLUMNS,
        )
        assert prepare_tdcc_retail_ratio(df) == 0.20


class TestResolveDahuDates:
    # 由新到舊（與 fetch_tdcc_token_and_dates 回傳順序一致）
    _DATES = [
        dt.date(2026, 5, 29),
        dt.date(2026, 5, 22),
        dt.date(2026, 5, 15),
        dt.date(2026, 5, 8),
    ]

    def test_no_range_returns_latest_only(self):
        assert _resolve_dahu_dates(self._DATES, None, None) == [dt.date(2026, 5, 29)]

    def test_no_range_uses_max_not_list_order(self):
        """即使 option 順序被打亂，預設仍取真正最新的一週。"""
        scrambled = [
            dt.date(2026, 5, 15),
            dt.date(2026, 5, 29),
            dt.date(2026, 5, 8),
            dt.date(2026, 5, 22),
        ]
        assert _resolve_dahu_dates(scrambled, None, None) == [dt.date(2026, 5, 29)]

    def test_range_filters_and_sorts_ascending(self):
        result = _resolve_dahu_dates(
            self._DATES, dt.date(2026, 5, 10), dt.date(2026, 5, 25)
        )
        assert result == [dt.date(2026, 5, 15), dt.date(2026, 5, 22)]

    def test_range_inclusive_endpoints(self):
        result = _resolve_dahu_dates(
            self._DATES, dt.date(2026, 5, 8), dt.date(2026, 5, 29)
        )
        assert result == [
            dt.date(2026, 5, 8),
            dt.date(2026, 5, 15),
            dt.date(2026, 5, 22),
            dt.date(2026, 5, 29),
        ]

    def test_from_only(self):
        result = _resolve_dahu_dates(self._DATES, dt.date(2026, 5, 20), None)
        assert result == [dt.date(2026, 5, 22), dt.date(2026, 5, 29)]

    def test_to_only(self):
        result = _resolve_dahu_dates(self._DATES, None, dt.date(2026, 5, 16))
        assert result == [dt.date(2026, 5, 8), dt.date(2026, 5, 15)]

    def test_empty_when_no_match(self):
        result = _resolve_dahu_dates(
            self._DATES, dt.date(2027, 1, 1), dt.date(2027, 12, 31)
        )
        assert result == []

    def test_reversed_endpoints_are_normalized(self):
        """from > to 時自動對調，不可吞掉合法區間。"""
        result = _resolve_dahu_dates(
            self._DATES, dt.date(2026, 5, 25), dt.date(2026, 5, 10)
        )
        assert result == [dt.date(2026, 5, 15), dt.date(2026, 5, 22)]
