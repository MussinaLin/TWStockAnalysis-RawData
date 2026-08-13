"""Unit tests for 漲跌價差（change）欄位的 normalize。

三個來源格式不同：STOCK_DAY_ALL 自帶符號、TPEX 除權息日給中文字串、
MI_INDEX 符號在獨立 HTML 欄位且以 X 標記除權息。
"""

from __future__ import annotations

import pandas as pd

from tw_stock_rawdata.prepare import (
    _merge_change_sign,
    prepare_tpex_quotes,
    prepare_twse_mi_index,
)


class TestMergeChangeSign:
    def test_positive_sign(self) -> None:
        assert _merge_change_sign("<p style= color:red>+</p>", "20.00") == 20.0

    def test_negative_sign(self) -> None:
        assert _merge_change_sign("<p style= color:green>-</p>", "5.00") == -5.0

    def test_ex_rights_marker_returns_none(self) -> None:
        """X = 該檔當日除權息，交易所未提供相對前日的漲跌 → 不推測。"""
        assert _merge_change_sign("<p>X</p>", "0.00") is None

    def test_empty_sign_is_flat(self) -> None:
        assert _merge_change_sign("<p></p>", "0.00") == 0.0

    def test_unparseable_magnitude_returns_none(self) -> None:
        assert _merge_change_sign("<p></p>", "--") is None


class TestPrepareMiIndexChange:
    def test_merges_sign_column(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "證券代號": "2330", "證券名稱": "台積電",
                    "開盤價": "2,405.00", "最高價": "2,415.00",
                    "最低價": "2,390.00", "收盤價": "2,415.00",
                    "漲跌(+/-)": "<p style= color:red>+</p>", "漲跌價差": "20.00",
                    "成交股數": "19,448,153",
                },
                {
                    "證券代號": "3605", "證券名稱": "宏致",
                    "開盤價": "111.00", "最高價": "120.00",
                    "最低價": "111.00", "收盤價": "120.00",
                    "漲跌(+/-)": "<p>X</p>", "漲跌價差": "0.00",
                    "成交股數": "8,422,797",
                },
            ]
        )
        out = prepare_twse_mi_index(df)
        assert out.loc[out["symbol"] == "2330", "change"].iloc[0] == 20.0
        assert out.loc[out["symbol"] == "3605", "change"].isna().iloc[0]

    def test_sign_column_is_dropped(self) -> None:
        df = pd.DataFrame(
            [{
                "證券代號": "2330", "證券名稱": "台積電",
                "開盤價": "2,405.00", "最高價": "2,415.00",
                "最低價": "2,390.00", "收盤價": "2,415.00",
                "漲跌(+/-)": "<p style= color:red>+</p>", "漲跌價差": "20.00",
                "成交股數": "19,448,153",
            }]
        )
        assert "change_sign" not in prepare_twse_mi_index(df).columns


class TestPrepareTpexQuotesChange:
    def test_signed_change(self) -> None:
        df = pd.DataFrame(
            [{
                "代號": "6488", "名稱": "環球晶", "收盤": "45.21", "漲跌": "+1.41",
                "開盤": "44.20", "最高": "45.26", "最低": "44.20", "成交股數": "508,551",
            }]
        )
        assert prepare_tpex_quotes(df)["change"].iloc[0] == 1.41

    def test_ex_dividend_chinese_marker_returns_na(self) -> None:
        """TPEX 除權息日的漲跌欄直接放中文字串，不是數字。"""
        df = pd.DataFrame(
            [{
                "代號": "5903", "名稱": "全家", "收盤": "184.00", "漲跌": "除息 ",
                "開盤": "183.00", "最高": "185.00", "最低": "182.00", "成交股數": "100,000",
            }]
        )
        assert prepare_tpex_quotes(df)["change"].isna().iloc[0]
