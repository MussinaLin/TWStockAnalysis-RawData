"""Unit tests: 大盤外資買賣超（BFI82U）與融資彙總（MI_MARGN）的失敗語意。

兩者原本在 stat 不是 OK / 找不到目標列時回 None，呼叫端靜默略過、不印 log。
MI_MARGN 靜默失敗還會連帶跳過 D-1 校正——2026-06／08 的大盤融資餘額錯誤即源於此
（D 的餘額停在交易所當日速報值）。改成丟 DataUnavailableError 並帶 TWSE 的 stat，
才看得出是不是限流字串（與「沒資料」同一句）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import FakeSession
from tw_stock_rawdata.sources import (
    DataUnavailableError,
    fetch_twse_foreign_net,
    fetch_twse_market_margin,
)

DATE = dt.date(2026, 7, 7)
RATE_LIMITED = {"stat": "很抱歉，沒有符合條件的資料!", "total": 0}


class TestFetchTwseForeignNet:
    def test_returns_foreign_net(self) -> None:
        payload = {"stat": "OK", "data": [
            ["自營商(自行買賣)", "1", "2", "3"],
            ["外資及陸資(不含外資自營商)", "100", "200", "-54,731,070,648"],
        ]}
        assert fetch_twse_foreign_net(FakeSession(payload=payload), DATE) == -54_731_070_648

    def test_stat_not_ok_raises_with_stat(self) -> None:
        with pytest.raises(DataUnavailableError, match="很抱歉，沒有符合條件的資料"):
            fetch_twse_foreign_net(FakeSession(payload=RATE_LIMITED), DATE)

    def test_missing_foreign_row_raises(self) -> None:
        payload = {"stat": "OK", "data": [["自營商(自行買賣)", "1", "2", "3"]]}
        with pytest.raises(DataUnavailableError, match="外資"):
            fetch_twse_foreign_net(FakeSession(payload=payload), DATE)

    def test_unparseable_value_raises(self) -> None:
        payload = {"stat": "OK", "data": [["外資及陸資(不含外資自營商)", "1", "2", "--"]]}
        with pytest.raises(DataUnavailableError, match="外資"):
            fetch_twse_foreign_net(FakeSession(payload=payload), DATE)


class TestFetchTwseMarketMargin:
    def test_returns_parsed_balances(self) -> None:
        payload = {"stat": "OK", "tables": [{"data": [
            ["融資金額(仟元)", "1", "2", "3", "593046092", "605041591"],
        ]}]}
        out = fetch_twse_market_margin(FakeSession(payload=payload), DATE)
        assert out["margin_balance"] == 605_041_591_000
        assert out["prev_margin_balance"] == 593_046_092_000

    def test_stat_not_ok_raises_with_stat(self) -> None:
        with pytest.raises(DataUnavailableError, match="很抱歉，沒有符合條件的資料"):
            fetch_twse_market_margin(FakeSession(payload=RATE_LIMITED), DATE)

    def test_missing_margin_row_raises(self) -> None:
        payload = {"stat": "OK", "tables": [{"data": [["融券(張)", "1", "2", "3", "4", "5"]]}]}
        with pytest.raises(DataUnavailableError, match="融資金額"):
            fetch_twse_market_margin(FakeSession(payload=payload), DATE)
