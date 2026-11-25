"""Unit tests: e添富「月規模」的解析（一次性回補 tools/backfill_etf_month_aum.py 用）。

月規模在 e添富 etfInfo 頁面的 `let chartData = {...};` 裡（totalMonthAv），
12 筆、標籤只有英文月份沒有年份，依時間由舊到新；tooltip 說明是
「每個月最後一個營業日的資產規模」，單位億元。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tests.conftest import FakeSession
from tw_stock_rawdata.prepare import resolve_month_labels
from tw_stock_rawdata.sources import DataUnavailableError, fetch_etf_month_aum

TWELVE = ["Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct"]


def test_resolve_labels_ending_last_month() -> None:
    out = resolve_month_labels(TWELVE, dt.date(2026, 11, 25))
    assert out[0] == (2025, 11) and out[1] == (2025, 12) and out[2] == (2026, 1)
    assert out[-1] == (2026, 10) and len(out) == 12


def test_resolve_labels_including_current_month() -> None:
    assert resolve_month_labels(["Oct", "Nov"], dt.date(2026, 11, 30)) == [(2026, 10), (2026, 11)]


def test_resolve_labels_in_january() -> None:
    assert resolve_month_labels(["Nov", "Dec"], dt.date(2027, 1, 5)) == [(2026, 11), (2026, 12)]


def test_resolve_labels_not_consecutive_raises() -> None:
    with pytest.raises(ValueError):
        resolve_month_labels(["Aug", "Oct"], dt.date(2026, 11, 25))


def test_resolve_labels_unknown_name_raises() -> None:
    with pytest.raises(ValueError):
        resolve_month_labels(["Oct", "11月"], dt.date(2026, 11, 25))


def test_resolve_empty() -> None:
    assert resolve_month_labels([], dt.date(2026, 11, 25)) == []


def test_fetch_month_aum_parses_chart_data() -> None:
    html = (
        '<script>let chartData = {"totalMonthAv":[{"count":9423.45,"date":"Nov"},'
        '{"count":24556.07,"date":"Oct"}],"close1":[]};</script>'
    )
    session = FakeSession(text=html)
    assert fetch_etf_month_aum(session, "0050") == [
        ("Nov", Decimal("9423.45")), ("Oct", Decimal("24556.07"))]
    assert session.calls[0][0] == "https://www.twse.com.tw/zh/ETFortune/etfInfo/0050"


@pytest.mark.parametrize("html", [
    "<html></html>",
    "<script>let chartData = {not json};</script>",
    '<script>let chartData = {"totalMonthAv": "x"};</script>',
])
def test_fetch_month_aum_bad_page_raises(html: str) -> None:
    with pytest.raises(DataUnavailableError):
        fetch_etf_month_aum(FakeSession(text=html), "0050")
