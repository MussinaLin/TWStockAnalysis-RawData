"""Unit tests: TPEX 系列 fetcher 的請求形狀與解析分歧。

docs/refactor-plan.md X1（TPEX 那一群）的前置作業。這四個 fetcher 的請求段與
TWSE 群逐字相同，但解析各不相同——測試分兩層：請求形狀（抽共用函式後不可變）
與各自的解析（抽取後必須保留）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import requests

from tests.conftest import FakeResponse, FakeSession
from tw_stock_rawdata import sources

DATE = dt.date(2026, 8, 21)
END = dt.date(2026, 8, 31)


def _session(payload):
    return FakeSession(text=json.dumps(payload, ensure_ascii=False), payload=payload)


# date 前 6 碼必須等於請求月份——端點對未知參數會靜默回當月，那道防線見
# fetch_tpex_stock_day 內的「坑 3」註解。
STOCK_DAY = {"stat": "ok", "date": "20260801", "tables": [
    {"title": "個股日成交資訊", "fields": ["日 期", "收盤"], "data": [["115/08/21", "500"]]}
]}
COMPANY = [{"SecuritiesCompanyCode": "6488", "IssueShares": "100"}]
MARGIN = [{"Date": "1150821", "SecuritiesCompanyCode": "6488"}]
DISPOSITION = {"stat": "ok", "tables": [
    {"title": "上櫃處置有價證券資訊", "fields": ["證券代號"], "data": [["6488"]]}
]}

CASES = [
    ("stock_day", lambda s: sources.fetch_tpex_stock_day(s, "6488", DATE),
     "TPEX_STOCK_DAY_URL", STOCK_DAY),
    ("company_basic", lambda s: sources.fetch_tpex_company_basic(s),
     "TPEX_COMPANY_BASIC_URL", COMPANY),
    ("margin", lambda s: sources.fetch_tpex_margin(s),
     "TPEX_MARGIN_URL", MARGIN),
    ("disposition", lambda s: sources.fetch_tpex_disposition(s, DATE, END),
     "TPEX_DISPOSAL_V2_URL", DISPOSITION),
]
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("name,call,url_attr,payload", CASES, ids=IDS)
class TestRequestShape:
    def test_hits_expected_url(self, name, call, url_attr, payload) -> None:
        session = _session(payload)
        call(session)
        assert session.calls[0][0] == getattr(sources, url_attr)

    def test_http_error_propagates(self, name, call, url_attr, payload, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        session = FakeSession(responses=[FakeResponse(
            text=json.dumps(payload, ensure_ascii=False), payload=payload,
            status_error=requests.HTTPError("503"))])
        with pytest.raises((requests.HTTPError, sources.DataUnavailableError)):
            call(session)


class TestParamsPerFetcher:
    def test_stock_day_params_use_western_year_and_no_response_key(self) -> None:
        """三個已知陷阱，程式碼裡標為坑 1/2：

        - 參數名是 `code`（不是 stkno / stockNo）。
        - 日期用**西元** 2026/08/01，不是民國——這支端點與其他 TPEX 端點相反。
        - **不可**加 response=json，加了會回不同格式。
        """
        session = _session(STOCK_DAY)
        sources.fetch_tpex_stock_day(session, "6488", DATE)
        _url, params = session.calls[0]
        assert params == {"code": "6488", "date": "2026/08/01"}

    def test_disposition_uses_slash_date_format(self) -> None:
        """TPEX 處置端點吃 YYYY/MM/DD，與 TWSE 的 YYYYMMDD 不同。"""
        session = _session(DISPOSITION)
        sources.fetch_tpex_disposition(session, DATE, END)
        _url, params = session.calls[0]
        assert params["startDate"] == "2026/08/21"
        assert params["endDate"] == "2026/08/31"

    def test_company_basic_and_margin_send_no_params(self) -> None:
        for fn, payload in ((sources.fetch_tpex_company_basic, COMPANY),
                            (sources.fetch_tpex_margin, MARGIN)):
            session = _session(payload)
            fn(session)
            assert session.calls[0][1] == {}


class TestParsingDifferences:
    """抽共用函式後這些必須原樣保留。"""

    def test_company_basic_requires_list_payload(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        with pytest.raises(sources.DataUnavailableError, match="回傳格式異常"):
            sources.fetch_tpex_company_basic(_session({"stat": "ok"}))

    def test_company_basic_empty_list_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        with pytest.raises(sources.DataUnavailableError, match="無資料"):
            sources.fetch_tpex_company_basic(_session([]))

    def test_margin_parses_compact_roc_date(self) -> None:
        """TPEX 融資融券的日期是緊湊民國格式 1150821，不是 115/08/21。"""
        _df, data_date = sources.fetch_tpex_margin(_session(MARGIN))
        assert data_date == dt.date(2026, 8, 21)

    def test_margin_unparseable_date_yields_none(self) -> None:
        _df, data_date = sources.fetch_tpex_margin(_session([{"Date": "xxx"}]))
        assert data_date is None

    def test_disposition_uses_its_own_table_keyword(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        wrong = {"stat": "ok", "tables": [
            {"title": "上櫃股票", "fields": ["a"], "data": [["1"]]}
        ]}
        with pytest.raises(sources.DataUnavailableError, match="上櫃處置有價證券資訊"):
            sources.fetch_tpex_disposition(_session(wrong), DATE, END)

    def test_disposition_accepts_missing_stat(self) -> None:
        payload = dict(DISPOSITION)
        payload.pop("stat")
        df = sources.fetch_tpex_disposition(_session(payload), DATE, END)
        assert list(df.columns) == ["證券代號"]

    def test_disposition_bad_stat_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        bad = dict(DISPOSITION, stat="參數輸入錯誤")
        with pytest.raises(sources.DataUnavailableError, match="參數輸入錯誤"):
            sources.fetch_tpex_disposition(_session(bad), DATE, END)


def test_stock_day_rejects_month_mismatch(monkeypatch) -> None:
    """端點對未知參數會靜默回當月——回傳月份不符必須當成無效資料。"""
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    wrong = dict(STOCK_DAY, date="20260701")
    with pytest.raises(sources.DataUnavailableError, match="月份不匹配"):
        sources.fetch_tpex_stock_day(_session(wrong), "6488", DATE)


def test_stock_day_non_json_raises(monkeypatch) -> None:
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    session = FakeSession(responses=[FakeResponse(text="<html>維護中</html>", payload=None)])
    session._responses[0].json = lambda: (_ for _ in ()).throw(ValueError("no json"))
    with pytest.raises(sources.DataUnavailableError, match="非 JSON"):
        sources.fetch_tpex_stock_day(session, "6488", DATE)
