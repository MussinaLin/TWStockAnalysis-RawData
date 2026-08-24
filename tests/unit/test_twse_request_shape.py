"""Unit tests: TWSE 系列 fetcher 的請求形狀與錯誤傳遞。

docs/refactor-plan.md X1（TWSE 那一群）的前置作業。這八個 fetcher 共用同一段
HTTP 前置樣板（disable_warnings → get(timeout=30, verify=False) →
raise_for_status → json），要把它抽成共用函式，就得先釘住每個呼叫端實際送出
什麼、以及 raise_for_status 的例外會不會被吞掉。

不涵蓋各自的解析邏輯（那些另有測試或屬其他項目），只涵蓋「請求怎麼送、
回應怎麼進入解析」這一層。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

from tests.conftest import FakeResponse, FakeSession
from tw_stock_rawdata import sources

DATE = dt.date(2026, 8, 21)


def _ok(fields, data, **extra):
    p = {"stat": "OK", "fields": fields, "data": data}
    p.update(extra)
    return p


def _session(payload):
    """FakeSession 同時給 text——fetch_twse_stock_day 在 json() 之前另檢查
    response.text 是否空白（限流時 TWSE 會回 200 + 空 body）。"""
    return FakeSession(text=json.dumps(payload, ensure_ascii=False), payload=payload)


# (名稱, 呼叫, 預期 URL 常數, 預期 params 子集, 可用 payload)
CASES = [
    (
        "stock_day",
        lambda s: sources.fetch_twse_stock_day(s, "2330", DATE),
        "TWSE_STOCK_DAY_URL",
        {"response": "json", "stockNo": "2330", "date": "20260801"},
        _ok(["日期", "收盤價"], [["115/08/21", "1000"]]),
    ),
    (
        "t86",
        lambda s: sources.fetch_twse_t86(s, DATE),
        "TWSE_T86_URL",
        {"response": "json", "date": "20260821", "selectType": "ALL"},
        _ok(["證券代號", "買進股數"], [["2330", "100"]]),
    ),
    (
        "mi_index",
        lambda s: sources.fetch_twse_mi_index(s, DATE),
        "TWSE_MI_INDEX_URL",
        {"response": "json", "date": "20260821", "type": "ALLBUT0999"},
        {"stat": "OK", "date": "20260821", "tables": [
            {"fields": ["證券代號", "開盤價", "收盤價"], "data": [["2330", "1", "2"]]}
        ]},
    ),
    (
        "taiex_ohlc",
        lambda s: sources.fetch_twse_taiex_ohlc(s, DATE),
        "TWSE_TAIEX_OHLC_URL",
        {"response": "json", "date": "20260801"},
        _ok(None, [["115/08/21", "1", "2", "3", "4"]]),
    ),
    (
        "market_volume",
        lambda s: sources.fetch_twse_market_volume(s, DATE),
        "TWSE_MARKET_VOLUME_URL",
        {"response": "json", "date": "20260801"},
        _ok(None, [["115/08/21", "1", "2", "3"]]),
    ),
    (
        "foreign_net",
        lambda s: sources.fetch_twse_foreign_net(s, DATE),
        "TWSE_FOREIGN_NET_URL",
        {"response": "json", "dayDate": "20260821", "type": "day"},
        _ok(None, [["外資及陸資(不含外資自營商)", "1", "2", "300"]]),
    ),
    (
        "market_margin",
        lambda s: sources.fetch_twse_market_margin(s, DATE),
        "TWSE_MARKET_MARGIN_URL",
        {"response": "json", "date": "20260821", "selectType": "MS"},
        {"stat": "OK", "tables": [{"data": [["融資金額(仟元)", "1", "2", "3", "4", "5"]]}]},
    ),
]
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("name,call,url_attr,expected_params,payload", CASES, ids=IDS)
class TestRequestShape:
    def test_hits_expected_url(self, name, call, url_attr, expected_params, payload) -> None:
        session = _session(payload)
        call(session)
        assert session.calls[0][0] == getattr(sources, url_attr)

    def test_sends_expected_params(self, name, call, url_attr, expected_params, payload) -> None:
        session = _session(payload)
        call(session)
        assert session.calls[0][1] == expected_params

    def test_http_error_propagates(self, name, call, url_attr, expected_params, payload, monkeypatch) -> None:
        """raise_for_status 的例外不可被吞——它要往上給 retry 判斷。"""
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        session = FakeSession(responses=[
            FakeResponse(text=json.dumps(payload, ensure_ascii=False), payload=payload,
                         status_error=requests.HTTPError("503"))
        ])
        with pytest.raises((requests.HTTPError, sources.DataUnavailableError)):
            call(session)


class TestStockDayAllHasNoParams:
    """STOCK_DAY_ALL 走 openapi 主機且不帶任何查詢參數。"""

    def test_no_params_sent(self) -> None:
        session = _session([{"Code": "2330", "Date": "20260821",
                             "OpeningPrice": "1", "ClosingPrice": "2"}])
        sources.fetch_twse_stock_day_all(session)
        url, params = session.calls[0]
        assert url == sources.TWSE_STOCK_DAY_ALL_URL
        assert params == {}

    def test_non_list_payload_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        with pytest.raises(sources.DataUnavailableError, match="回傳格式異常"):
            sources.fetch_twse_stock_day_all(_session({"stat": "OK"}))

    def test_empty_list_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        with pytest.raises(sources.DataUnavailableError, match="無資料"):
            sources.fetch_twse_stock_day_all(_session([]))
