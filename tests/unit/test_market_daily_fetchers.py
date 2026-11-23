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
    TWSE_DAY_TRADE_URL,
    DataUnavailableError,
    fetch_twse_day_trade,
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


DAY_TRADE_DATE = dt.date(2026, 11, 20)
DAY_TRADE_FIELDS = [
    "當日沖銷交易總成交股數",
    "當日沖銷交易總成交股數占市場比重%",
    "當日沖銷交易總買進成交金額",
    "當日沖銷交易總買進成交金額占市場比重%",
    "當日沖銷交易總賣出成交金額",
    "當日沖銷交易總賣出成交金額占市場比重%",
]
DAY_TRADE_ROW = [
    "4,460,672,000", "26.1", "322,561,954,550", "34.48", "322,720,245,100", "34.50",
]


def _day_trade_payload(fields=None, data=None, date="20261120", title=None) -> dict:
    """TWTB4U 2026-11-20 實測回應（個股明細表只留一列）。"""
    return {
        "stat": "OK",
        "date": date,
        "selectType": "All",
        "tables": [
            {
                "title": title or "115年11月20日 當日沖銷交易統計資訊",
                "fields": fields or list(DAY_TRADE_FIELDS),
                "data": data if data is not None else [list(DAY_TRADE_ROW)],
            },
            {
                "title": "115年11月20日 當日沖銷交易標的及成交量值",
                "fields": ["證券代號", "證券名稱", "暫停現股賣出後現款買進當沖註記",
                           "當日沖銷交易成交股數", "當日沖銷交易買進成交金額",
                           "當日沖銷交易賣出成交金額"],
                "data": [["00400A", "主動國泰動能高息", "", "16,731,000",
                          "167,109,870", "166,300,040"]],
            },
        ],
    }


class TestFetchTwseDayTrade:
    def test_returns_six_fields(self) -> None:
        session = FakeSession(payload=_day_trade_payload())
        out = fetch_twse_day_trade(session, DAY_TRADE_DATE)
        assert out == {
            "day_trade_shares": 4460672000,
            "day_trade_shares_pct": pytest.approx(0.261),
            "day_trade_buy_amount": 322561954550,
            "day_trade_buy_amount_pct": pytest.approx(0.3448),
            "day_trade_sell_amount": 322720245100,
            "day_trade_sell_amount_pct": pytest.approx(0.3450),
        }
        assert session.calls == [(
            TWSE_DAY_TRADE_URL,
            {"date": "20261120", "selectType": "All", "response": "json"},
        )]

    def test_stat_not_ok_raises_with_stat(self) -> None:
        with pytest.raises(DataUnavailableError, match="很抱歉"):
            fetch_twse_day_trade(FakeSession(payload=RATE_LIMITED), DAY_TRADE_DATE)

    def test_date_mismatch_raises(self) -> None:
        session = FakeSession(payload=_day_trade_payload(date="20261119"))
        with pytest.raises(DataUnavailableError, match="2026-11-19"):
            fetch_twse_day_trade(session, DAY_TRADE_DATE)

    def test_summary_table_missing_raises(self) -> None:
        payload = _day_trade_payload(title="115年11月20日 其他表")
        with pytest.raises(DataUnavailableError):
            fetch_twse_day_trade(FakeSession(payload=payload), DAY_TRADE_DATE)

    def test_header_change_raises(self) -> None:
        fields = list(DAY_TRADE_FIELDS)
        fields[1] = "當日沖銷交易總成交股數占比%"
        payload = _day_trade_payload(fields=fields)
        with pytest.raises(DataUnavailableError, match="表頭"):
            fetch_twse_day_trade(FakeSession(payload=payload), DAY_TRADE_DATE)

    @pytest.mark.parametrize("rows", [[], [DAY_TRADE_ROW, DAY_TRADE_ROW]])
    def test_row_count_not_one_raises(self, rows) -> None:
        payload = _day_trade_payload(data=[list(r) for r in rows])
        with pytest.raises(DataUnavailableError):
            fetch_twse_day_trade(FakeSession(payload=payload), DAY_TRADE_DATE)

    def test_unparseable_value_becomes_none(self) -> None:
        """Review Focus 3：單一數值是 -- 或空字串 → 該欄 None，其餘照寫。"""
        row = list(DAY_TRADE_ROW)
        row[0] = "--"
        row[3] = ""
        payload = _day_trade_payload(data=[row])
        out = fetch_twse_day_trade(FakeSession(payload=payload), DAY_TRADE_DATE)
        assert out["day_trade_shares"] is None
        assert out["day_trade_buy_amount_pct"] is None
        assert out["day_trade_shares_pct"] == pytest.approx(0.261)
        assert out["day_trade_buy_amount"] == 322561954550
        assert out["day_trade_sell_amount"] == 322720245100
        assert out["day_trade_sell_amount_pct"] == pytest.approx(0.3450)
