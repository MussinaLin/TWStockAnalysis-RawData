"""Unit tests: TPEX 個股日成交資訊月表（無網路）。

2026-08-22 實測到的三個坑，這裡逐一鎖住：
1. date 參數是「西元」yyyy/MM/dd，不是民國 —— 與 repo 其他 TPEX v2 端點相反。
2. 不可帶 response=json，帶了端點回「參數輸入錯誤」。
3. 參數名打錯不會報錯，會靜默 fallback 回「當月」資料且 stat=ok。
   ⇒ 必須驗證回應的 date 落在請求月份，否則歷史回補會被寫進當月數字而完全無聲。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tw_stock_rawdata import sources
from tests.conftest import FakeSession
from tw_stock_rawdata.sources import DataUnavailableError, fetch_tpex_stock_day


def _payload(echo_date: str, rows: list[list[str]] | None = None) -> dict:
    return {
        "tables": [{
            "title": "個股日成交資訊",
            "subtitle": "6488 環球晶 114年07月",
            "date": echo_date,
            "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                       "收盤", "漲跌", "筆數"],
            "data": rows if rows is not None else [
                ["114/07/01", "1,985", "605,535", "301.50", "307.50",
                 "300.50", "307.50", "6.00", "2,441"],
            ],
        }],
        "date": echo_date,
        "code": "6488",
        "name": "環球晶",
        "stat": "ok",
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)


def test_date_param_is_gregorian_not_roc() -> None:
    """坑 1：民國格式會被端點拒絕，這裡鎖住送出去的是西元 yyyy/MM/01。"""
    session = FakeSession(payload=_payload("20250701"))
    fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    _url, params = session.calls[0]
    assert params["date"] == "2025/07/01"
    assert params["code"] == "6488"


def test_response_json_param_is_not_sent() -> None:
    """坑 2：帶了 response=json 端點會回「參數輸入錯誤」。"""
    session = FakeSession(payload=_payload("20250701"))
    fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    _url, params = session.calls[0]
    assert "response" not in params


def test_returns_month_table() -> None:
    session = FakeSession(payload=_payload("20250701"))
    df = fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    assert list(df.columns)[:2] == ["日 期", "成交張數"]
    assert df.iloc[0]["收盤"] == "307.50"


def test_month_mismatch_raises() -> None:
    """坑 3：請求 2025/07，端點靜默回當月（2026/08）→ 必須拋錯，不可當成資料。"""
    session = FakeSession(payload=_payload("20260801"))

    with pytest.raises(DataUnavailableError, match="月份不匹配"):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


def test_bad_stat_raises() -> None:
    session = FakeSession(payload={"stat": "參數輸入錯誤"})

    with pytest.raises(DataUnavailableError, match="參數輸入錯誤"):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


def test_empty_month_raises_data_unavailable() -> None:
    """該檔該月無資料（未上市 / 停牌）→ DataUnavailableError，呼叫端據此判斷。"""
    session = FakeSession(payload=_payload("20250701", rows=[]))

    with pytest.raises(DataUnavailableError):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
