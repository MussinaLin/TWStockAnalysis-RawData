"""Unit tests: www.twse.com.tw 請求 pacing（無網路）。

根因：www.twse.com.tw 對同 IP 高頻請求會回 HTTP 200 + stat=「很抱歉，沒有符合條件的資料!」
的空殼（與真休市同字串），導致被誤判成沒資料。對該 host 加最小請求間隔即可從源頭避免觸發。
"""

from __future__ import annotations

import pytest

from tw_stock_rawdata import sources


def test_min_interval_adapter_sleeps_to_enforce_interval(monkeypatch):
    adapter = sources._MinIntervalAdapter(min_interval=1.0)

    clock = {"t": 100.0}
    sleeps: list[float] = []

    monkeypatch.setattr(sources.time, "monotonic", lambda: clock["t"])

    def fake_sleep(d):
        sleeps.append(d)
        clock["t"] += d

    monkeypatch.setattr(sources.time, "sleep", fake_sleep)
    # stub 父類別實際送請求，避免網路
    monkeypatch.setattr(sources.HTTPAdapter, "send", lambda self, *a, **k: "resp")

    # 第一次：沒有前次紀錄 → 不需等待
    adapter.send(object())
    # 第二次：時間未前進 → 必須 sleep 滿一個 min_interval
    adapter.send(object())

    assert sleeps == [pytest.approx(1.0)]


def test_build_session_paces_only_twse_www():
    session = sources.build_session(min_interval=0.5)

    paced = session.get_adapter("https://www.twse.com.tw/fund/T86")
    assert isinstance(paced, sources._MinIntervalAdapter)
    assert paced._min_interval == 0.5

    # openapi / 其他 host 不應被 pacing
    other = session.get_adapter("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    assert not isinstance(other, sources._MinIntervalAdapter)

    assert "tw-stock-rawdata" in session.headers["User-Agent"]
