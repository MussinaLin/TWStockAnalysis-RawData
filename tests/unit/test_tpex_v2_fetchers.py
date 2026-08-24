"""Unit tests: TPEX V2 三個 fetcher 與共用的表格擷取。

docs/refactor-plan.md X2 的前置作業。三者原覆蓋率 8% / 5% / 42%，
而它們的 10 行本體逐行同構，要合併就得先有回歸保護。

三者的差異只在：URL、額外 params、錯誤訊息、表格關鍵字，
以及 fetch_tpex_3insti_v2 多一段位置改名（欄名重複，只能靠位置區分）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tests.conftest import FakeSession
from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import (
    DataUnavailableError,
    _extract_tpex_v2_table,
    fetch_tpex_3insti_v2,
    fetch_tpex_daily_quotes_v2,
    fetch_tpex_margin_v2,
)

DATE = dt.date(2026, 8, 21)


def _payload(title="上櫃股票", fields=None, data=None, stat="ok", date_str="115/08/21", **extra):
    p = {
        "stat": stat,
        "date": date_str,
        "tables": [{
            "title": f"114年08月 {title} 日成交資訊",
            "fields": fields if fields is not None else ["代號", "名稱", "收盤"],
            "data": data if data is not None else [["6488", "環球晶", "500"]],
        }],
    }
    if stat is None:
        p.pop("stat")
    p.update(extra)
    return p


class TestExtractTable:
    def test_picks_table_by_title_keyword(self) -> None:
        payload = {"tables": [
            {"title": "其他表", "fields": ["a"], "data": [["1"]]},
            {"title": "上櫃股票日成交", "fields": ["代號"], "data": [["6488"]]},
        ]}
        df = _extract_tpex_v2_table(payload, "上櫃股票")
        assert list(df.columns) == ["代號"]

    def test_missing_keyword_raises(self) -> None:
        with pytest.raises(DataUnavailableError, match="找不到包含「三大法人」"):
            _extract_tpex_v2_table(_payload(), "三大法人")

    def test_table_with_empty_data_is_skipped(self) -> None:
        payload = {"tables": [{"title": "上櫃股票", "fields": ["代號"], "data": []}]}
        with pytest.raises(DataUnavailableError):
            _extract_tpex_v2_table(payload, "上櫃股票")

    def test_non_dict_entries_are_skipped(self) -> None:
        payload = {"tables": ["垃圾", {"title": "上櫃股票", "fields": ["代號"], "data": [["6488"]]}]}
        assert len(_extract_tpex_v2_table(payload, "上櫃股票")) == 1


class TestCommonBehaviour:
    """三個 fetcher 共有的行為——合併後必須維持一致。"""

    @pytest.mark.parametrize("fn,url_attr", [
        (fetch_tpex_daily_quotes_v2, "TPEX_DAILY_QUOTES_V2_URL"),
        (fetch_tpex_margin_v2, "TPEX_MARGIN_V2_URL"),
    ])
    def test_requests_expected_url_with_roc_date(self, fn, url_attr) -> None:
        session = FakeSession(payload=_payload())
        fn(session, DATE)
        url, params = session.calls[0]
        assert url == getattr(sources, url_attr)
        assert params["date"] == "115/08/21"
        assert params["response"] == "json"

    @pytest.mark.parametrize("fn", [
        fetch_tpex_daily_quotes_v2, fetch_tpex_margin_v2, fetch_tpex_3insti_v2,
    ])
    def test_missing_stat_key_is_accepted(self, fn) -> None:
        """TPEX 端點缺 stat 視為正常——與 TWSE 的嚴格判準刻意不同。"""
        title = "三大法人" if fn is fetch_tpex_3insti_v2 else "上櫃股票"
        _df, data_date = fn(FakeSession(payload=_payload(title=title, stat=None)), DATE)
        assert data_date == DATE

    @pytest.mark.parametrize("fn", [
        fetch_tpex_daily_quotes_v2, fetch_tpex_margin_v2, fetch_tpex_3insti_v2,
    ])
    @pytest.mark.parametrize("stat", ["ok", "OK"])
    def test_both_stat_cases_accepted(self, fn, stat) -> None:
        title = "三大法人" if fn is fetch_tpex_3insti_v2 else "上櫃股票"
        _df, _ = fn(FakeSession(payload=_payload(title=title, stat=stat)), DATE)

    @pytest.mark.parametrize("fn,msg", [
        (fetch_tpex_daily_quotes_v2, "TPEX V2 行情回傳異常"),
        (fetch_tpex_3insti_v2, "TPEX V2 三大法人回傳異常"),
        (fetch_tpex_margin_v2, "TPEX V2 融資融券回傳異常"),
    ])
    def test_bad_stat_raises_with_own_message(self, fn, msg, monkeypatch) -> None:
        """三者的錯誤訊息各自不同，合併後不可混用。"""
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        session = FakeSession(payload=_payload(stat="參數輸入錯誤"))
        with pytest.raises(DataUnavailableError, match="參數輸入錯誤"):
            fn(session, DATE)

    @pytest.mark.parametrize("fn,msg", [
        (fetch_tpex_daily_quotes_v2, "TPEX V2 行情回傳異常"),
        (fetch_tpex_3insti_v2, "TPEX V2 三大法人回傳異常"),
        (fetch_tpex_margin_v2, "TPEX V2 融資融券回傳異常"),
    ])
    def test_empty_stat_falls_back_to_own_message(self, fn, msg, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        session = FakeSession(payload=_payload(stat=""))
        with pytest.raises(DataUnavailableError, match=msg):
            fn(session, DATE)

    def test_unparseable_payload_date_yields_none(self) -> None:
        _df, data_date = fetch_tpex_margin_v2(
            FakeSession(payload=_payload(date_str="")), DATE
        )
        assert data_date is None


class TestPerFetcherDifferences:
    def test_3insti_sends_extra_params(self) -> None:
        session = FakeSession(payload=_payload(title="三大法人"))
        fetch_tpex_3insti_v2(session, DATE)
        _url, params = session.calls[0]
        assert params["type"] == "Daily"
        assert params["se"] == "EW"

    def test_quotes_and_margin_send_no_extra_params(self) -> None:
        for fn in (fetch_tpex_daily_quotes_v2, fetch_tpex_margin_v2):
            session = FakeSession(payload=_payload())
            fn(session, DATE)
            _url, params = session.calls[0]
            assert set(params) == {"date", "response"}

    def test_3insti_renames_duplicate_columns_by_position(self) -> None:
        """欄名重複（每個法人類別都有買進/賣出/買賣超），只能靠位置區分。"""
        fields = [f"c{i}" for i in range(24)]
        data = [[str(i) for i in range(24)]]
        session = FakeSession(payload=_payload(title="三大法人", fields=fields, data=data))
        df, _ = fetch_tpex_3insti_v2(session, DATE)
        cols = list(df.columns)
        assert cols[4] == "外資及陸資買賣超股數"
        assert cols[10] == "外資合計買賣超股數"
        assert cols[13] == "投信買賣超股數"
        assert cols[22] == "自營商合計買賣超股數"
        assert cols[23] == "三大法人買賣超股數合計"

    def test_3insti_skips_rename_when_too_few_columns(self) -> None:
        """欄數不足 24 時不改名——否則會 IndexError 或改到錯的欄。"""
        session = FakeSession(payload=_payload(title="三大法人"))
        df, _ = fetch_tpex_3insti_v2(session, DATE)
        assert list(df.columns) == ["代號", "名稱", "收盤"]

    def test_3insti_uses_its_own_table_keyword(self, monkeypatch) -> None:
        monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
        session = FakeSession(payload=_payload(title="上櫃股票"))
        with pytest.raises(DataUnavailableError, match="三大法人"):
            fetch_tpex_3insti_v2(session, DATE)
