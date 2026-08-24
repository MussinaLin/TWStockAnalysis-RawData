"""Unit tests: MoneyDJ 融資融券抓取（fetch_moneydj_margin）。

docs/refactor-plan.md T7。原覆蓋率 26%、CC 15。

孿生的 fetch_moneydj_holding_pct 已有 88% 覆蓋（test_moneydj_insti.py），
這一邊卻幾乎沒有——同一組邏輯一邊有保護一邊沒有。

表格是**靠位置取欄**的（0:日期 1:資買 2:資賣 4:資餘額 5:資增減 8:券賣
9:券買 11:券餘額 12:券增減），所以「挑中哪張表」與「濾掉哪些列」是正確性關鍵：
挑錯表或混進合計列都會整批錯位，而且不會有任何錯誤訊號。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tests.conftest import FakeSession
from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import DataUnavailableError, fetch_moneydj_margin

START = dt.date(2026, 2, 1)
END = dt.date(2026, 2, 28)

SUB_HEADERS = ["日期", "買進", "賣出", "現償", "餘額", "增減", "限額", "使用率",
               "賣出", "買進", "券償", "餘額", "增減", "券資比", "相抵"]


def _table_html(data_rows: list[list[str]], sub_headers: list[str] | None = None,
                n_filler: int = 5) -> str:
    """組出 MoneyDJ 的表格結構：0-4 填充列、5 上層表頭、6 子表頭、7+ 資料列。"""
    heads = sub_headers if sub_headers is not None else SUB_HEADERS
    n_cols = len(heads)
    rows = []
    for _ in range(n_filler):
        rows.append(["" for _ in range(n_cols)])
    rows.append(["融資"] * 8 + ["融券"] * (n_cols - 8))
    rows.append(heads)
    rows.extend(data_rows)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<html><body><table>{body}</table></body></html>"


def _data_row(date: str, margin_buy: str = "100") -> list[str]:
    return [date, margin_buy, "40", "10", "950", "50", "9999", "5%",
            "30", "10", "5", "200", "20", "21%", "0"]


def _fetch(html: str) -> pd.DataFrame:
    return fetch_moneydj_margin(FakeSession(text=html), "2330", START, END)


class TestTableSelection:
    def test_picks_table_with_date_and_buy_sell_headers(self) -> None:
        out = _fetch(_table_html([_data_row("115/02/11"), _data_row("115/02/12")]))
        assert list(out["date"]) == ["115/02/11", "115/02/12"]
        assert list(out["margin_buy"]) == ["100", "100"]

    def test_column_positions_are_mapped_correctly(self) -> None:
        """位置取欄：4→資餘額、11→券餘額、12→券增減。挑錯位置不會有錯誤訊號。"""
        out = _fetch(_table_html([_data_row("115/02/11")]))
        row = out.iloc[0]
        assert row["margin_balance"] == "950"
        assert row["margin_change"] == "50"
        assert row["short_balance"] == "200"
        assert row["short_change"] == "20"

    def test_table_too_narrow_is_rejected(self) -> None:
        """欄數不足 12 的表不可誤選——那是頁面上的其他小表。"""
        narrow = _table_html([["115/02/11"] + ["1"] * 4], sub_headers=["日期", "買進", "賣出", "a", "b"])
        with pytest.raises(DataUnavailableError, match="找不到融資融券表格"):
            _fetch(narrow)

    def test_table_without_date_header_is_rejected(self) -> None:
        heads = ["代號"] + SUB_HEADERS[1:]
        with pytest.raises(DataUnavailableError, match="找不到融資融券表格"):
            _fetch(_table_html([_data_row("115/02/11")], sub_headers=heads))

    def test_no_table_at_all_retries_then_raises(self, monkeypatch) -> None:
        """read_html 的 ValueError 刻意不在此攔截——讓 _retry_on_transient 重試
        （MoneyDJ 偶發回傳壞 HTML），用盡後 decorator 轉成 DataUnavailableError。

        必須把 sleep 停掉，否則這個測試會真的等完整個 backoff。
        """
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        with pytest.raises(DataUnavailableError):
            _fetch("<html><body><p>無資料</p></body></html>")


class TestRowFiltering:
    def test_summary_rows_are_dropped(self) -> None:
        """合計列混進來會讓後續位置取欄整批錯位。"""
        rows = [_data_row("115/02/11"), _data_row("合計"), _data_row("115/02/12")]
        out = _fetch(_table_html(rows))
        assert list(out["date"]) == ["115/02/11", "115/02/12"]

    def test_non_date_first_column_dropped(self) -> None:
        rows = [_data_row("115/02/11"), _data_row("2026/02/12")]
        out = _fetch(_table_html(rows))
        assert list(out["date"]) == ["115/02/11"]

    def test_all_rows_invalid_raises(self) -> None:
        with pytest.raises(DataUnavailableError, match="無有效資料"):
            _fetch(_table_html([_data_row("合計"), _data_row("小計")]))


class TestRequestShape:
    def test_date_params_use_unpadded_format(self) -> None:
        """MoneyDJ 吃 YYYY-M-D，補零會查不到。"""
        session = FakeSession(text=_table_html([_data_row("115/02/11")]))
        fetch_moneydj_margin(session, "2330", dt.date(2026, 2, 1), dt.date(2026, 3, 5))
        _url, params = session.calls[0]
        assert params["c"] == "2026-2-1"
        assert params["d"] == "2026-3-5"

    def test_symbol_passed_as_a_param(self) -> None:
        session = FakeSession(text=_table_html([_data_row("115/02/11")]))
        fetch_moneydj_margin(session, "6488", START, END)
        _url, params = session.calls[0]
        assert params["a"] == "6488"
