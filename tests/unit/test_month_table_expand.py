"""Unit tests: 個股月表 → 逐日 OHLCV/change 展開。

兩個關鍵不變量：
1. TWSE 在除權息日的漲跌價差是 "X0.00"（顯式標記），必須展開成 change=None，
   絕不可變成 0.0 —— 那會算出「參考價 = 收盤」的錯誤漲跌停。
2. volume 契約單位是「股」。TPEX 月表給的是「成交張數」，展開時要 × 1000。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata.sources import expand_tpex_stock_day, expand_twse_stock_day


def _twse_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["114/09/15", "24,000,000", "30,000,000", "1250.00", "1260.00",
             "1245.00", "1255.00", "-5.00", "50,000", ""],
            # 除息日：漲跌價差帶 X 標記
            ["114/09/16", "30,000,000", "38,000,000", "1270.00", "1285.00",
             "1265.00", "1280.00", "X0.00", "60,000", ""],
        ],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


def _tpex_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["114/07/15", "1,234", "380,000", "310.00", "312.00",
             "308.00", "310.00", "-1.50", "2,000"],
            # 除息日：TPEX 月表給的是相對除息參考價的正確漲跌
            ["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
             "307.50", "322.50", "18.50", "5,095"],
        ],
        columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                 "收盤", "漲跌", "筆數"],
    )


def test_twse_expand_basic() -> None:
    out = expand_twse_stock_day(_twse_df())

    row = out[dt.date(2025, 9, 15)]
    assert row["open"] == 1250.0
    assert row["high"] == 1260.0
    assert row["low"] == 1245.0
    assert row["close"] == 1255.0
    assert row["volume"] == 24_000_000
    assert row["change"] == -5.0


def test_twse_ex_dividend_change_is_none_not_zero() -> None:
    """X0.00 必須是 None。變成 0.0 會讓參考價 = 收盤，算出假的漲跌停。"""
    out = expand_twse_stock_day(_twse_df())

    row = out[dt.date(2025, 9, 16)]
    assert row["change"] is None
    assert row["close"] == 1280.0  # 其他欄位照常


def test_tpex_expand_converts_lots_to_shares() -> None:
    out = expand_tpex_stock_day(_tpex_df())

    row = out[dt.date(2025, 7, 15)]
    assert row["volume"] == 1_234_000  # 1,234 張 × 1000
    assert row["close"] == 310.0
    assert row["change"] == -1.5


def test_tpex_ex_dividend_change_is_reference_price_delta() -> None:
    """6488 2025-07-16 除息 6 元：322.50 - (310.00 - 6.00) = 18.50。"""
    out = expand_tpex_stock_day(_tpex_df())

    assert out[dt.date(2025, 7, 16)]["change"] == 18.5


def test_expand_skips_unparsable_date_rows() -> None:
    df = pd.DataFrame(
        [["合計", "1", "1", "1", "1", "1", "1", "1", "1"]],
        columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                 "收盤", "漲跌", "筆數"],
    )
    assert expand_tpex_stock_day(df) == {}


def test_twse_expand_accepts_alternate_volume_column_name() -> None:
    """成交量欄位吃「成交股數」與「成交量」兩種名稱。

    `find_twse_ohlcv`（daily 路徑）本來就吃兩種；這裡少了別名的話，TWSE 若改用
    「成交量」，這條路徑會 OHLC 有值而 volume 是 None —— 該列照樣寫進 DB
    （跳過與否只看價格），volume 與 turnover_rate 靜默變 NULL。
    """
    df = pd.DataFrame(
        [["114/09/15", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交量", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )

    assert expand_twse_stock_day(df)[dt.date(2025, 9, 15)]["volume"] == 24_000_000


def test_expand_empty_frame_returns_empty_dict() -> None:
    assert expand_twse_stock_day(pd.DataFrame()) == {}
    assert expand_tpex_stock_day(pd.DataFrame()) == {}


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
