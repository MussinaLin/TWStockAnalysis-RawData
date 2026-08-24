"""Unit tests: MoneyDJ 融資融券與持股比例 normalize。

補這組測試是為了讓 prepare_moneydj_margin（原覆蓋率 5%）有回歸保護，
才能安全地把它與 prepare_moneydj_holding_pct 內逐字相同的 _parse_moneydj_date
合併成共用函式（CLAUDE.md 抽象門檻兩次）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata.prepare import (
    prepare_moneydj_holding_pct,
    prepare_moneydj_margin,
)
from tw_stock_rawdata.sources import DataUnavailableError

MARGIN_COLS = ["margin_buy", "margin_sell", "margin_balance", "margin_change",
               "short_sell", "short_buy", "short_balance", "short_change"]


def _margin_df(dates, **overrides) -> pd.DataFrame:
    data = {"date": dates}
    for col in MARGIN_COLS:
        data[col] = overrides.get(col, ["100"] * len(dates))
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
    return pd.DataFrame(data)


class TestPrepareMoneydjMargin:
    def test_roc_dates_parsed(self) -> None:
        out = prepare_moneydj_margin(_margin_df(["115/02/11", "115/02/12"]))
        assert list(out["date"]) == [dt.date(2026, 2, 11), dt.date(2026, 2, 12)]

    def test_missing_date_column_raises(self) -> None:
        with pytest.raises(DataUnavailableError, match="缺少 date"):
            prepare_moneydj_margin(pd.DataFrame({"margin_buy": ["1"]}))

    def test_rows_with_unparseable_date_are_dropped(self) -> None:
        out = prepare_moneydj_margin(_margin_df(["115/02/11", "not-a-date", ""]))
        assert len(out) == 1
        assert out.iloc[0]["date"] == dt.date(2026, 2, 11)

    def test_na_date_is_dropped(self) -> None:
        out = prepare_moneydj_margin(_margin_df(["115/02/11", None]))
        assert len(out) == 1

    def test_missing_optional_column_becomes_none(self) -> None:
        out = prepare_moneydj_margin(_margin_df(["115/02/11"], short_change=None))
        assert out.iloc[0]["short_change"] is None

    def test_short_margin_ratio_computed_from_balances(self) -> None:
        out = prepare_moneydj_margin(
            _margin_df(["115/02/11"], margin_balance=["1000"], short_balance=["250"])
        )
        assert out.iloc[0]["short_margin_ratio"] == pytest.approx(0.25)

    def test_zero_margin_balance_yields_null_ratio(self) -> None:
        """融資餘額為 0 時不做除法，留 NULL 而不是 inf。"""
        out = prepare_moneydj_margin(
            _margin_df(["115/02/11"], margin_balance=["0"], short_balance=["250"])
        )
        assert pd.isna(out.iloc[0]["short_margin_ratio"])


class TestPrepareMoneydjHoldingPct:
    def test_roc_dates_parsed_and_pct_to_decimal(self) -> None:
        df = pd.DataFrame({
            "date": ["115/02/11"],
            "foreign_holding_pct": ["35.03%"],
            "insti_holding_pct": ["40.10%"],
        })
        out = prepare_moneydj_holding_pct(df)
        assert out.iloc[0]["date"] == dt.date(2026, 2, 11)
        assert out.iloc[0]["foreign_holding_pct"] == pytest.approx(0.3503)
        assert out.iloc[0]["insti_holding_pct"] == pytest.approx(0.4010)

    def test_rows_with_unparseable_date_are_dropped(self) -> None:
        df = pd.DataFrame({
            "date": ["115/02/11", "xx", ""],
            "foreign_holding_pct": ["35.03%", "1%", "2%"],
            "insti_holding_pct": ["40.10%", "1%", "2%"],
        })
        out = prepare_moneydj_holding_pct(df)
        assert len(out) == 1


def test_both_preparers_parse_dates_identically() -> None:
    """兩個 normalize 的日期解析必須一致——這是它們可以共用同一個 helper 的前提。"""
    dates = ["115/02/11", "114/12/31", "not-a-date", "", None]
    m = prepare_moneydj_margin(_margin_df(dates))
    h = prepare_moneydj_holding_pct(pd.DataFrame({
        "date": dates,
        "foreign_holding_pct": ["1%"] * len(dates),
        "insti_holding_pct": ["1%"] * len(dates),
    }))
    assert list(m["date"]) == list(h["date"])


# ---------------------------------------------------------------------------
# _is_moneydj_date_row —— 兩個 MoneyDJ fetcher 共用的資料列判準
#
# MoneyDJ 表格靠位置取欄，混進合計列或空列會整列錯位，故這道濾網是正確性關鍵。
# ---------------------------------------------------------------------------


class TestIsMoneydjDateRow:
    @pytest.mark.parametrize("value", ["115/02/11", "99/1/1", "114/12/31", " 115/02/11 "])
    def test_accepts_roc_dates(self, value: str) -> None:
        from tw_stock_rawdata.sources import _is_moneydj_date_row
        assert _is_moneydj_date_row(value) is True

    @pytest.mark.parametrize(
        "value",
        ["合計", "", "   ", "2026/02/11", "115-02-11", "115/02/11 合計", "abc", "1/1/1"],
    )
    def test_rejects_non_data_rows(self, value: str) -> None:
        from tw_stock_rawdata.sources import _is_moneydj_date_row
        assert _is_moneydj_date_row(value) is False

    def test_rejects_na(self) -> None:
        from tw_stock_rawdata.sources import _is_moneydj_date_row
        assert _is_moneydj_date_row(None) is False
        assert _is_moneydj_date_row(float("nan")) is False
