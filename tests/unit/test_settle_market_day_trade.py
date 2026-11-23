"""Unit tests: daily 以往前第 3 個交易日重抓校正上市當沖統計（_settle_market_day_trade）。

TWSE 的 TWTB4U 在 T 日晚間公布初版、T+1／T+2 晚間各修正一次，T+2 為最終值。
T+2 的更新與 22:00 的 daily 幾乎同時，不保證已更新，所以重抓往前第 3 個交易日；
同時當作「D 當晚尚未公布」的備援。只 UPDATE、任何失敗只印 log。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

CFG = SimpleNamespace(database_url="postgres://x")
TODAY = dt.date(2026, 11, 23)
TARGET = dt.date(2026, 11, 18)
DAY_TRADE = {
    "day_trade_shares": 4460672000, "day_trade_shares_pct": 0.261,
    "day_trade_buy_amount": 322561954550, "day_trade_buy_amount_pct": 0.3448,
    "day_trade_sell_amount": 322720245100, "day_trade_sell_amount_pct": 0.345,
}


@pytest.fixture
def settle(monkeypatch):
    state: dict = {"target": TARGET, "prev_queries": [], "fetched": [], "updated": [],
                   "result": dict(DAY_TRADE)}

    def fake_prev(url, date, n):  # noqa: ANN001 - 測試替身
        state["prev_queries"].append((date, n))
        return state["target"]

    def fake_fetch(session, date):  # noqa: ANN001 - 測試替身
        state["fetched"].append(date)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return dict(state["result"])

    def fake_update(url, updates):  # noqa: ANN001 - 測試替身
        state["updated"].append(list(updates))
        if isinstance(state.get("update_error"), Exception):
            raise state["update_error"]
        return len(updates)

    monkeypatch.setattr(run, "find_prev_market_trade_date", fake_prev)
    monkeypatch.setattr(run, "fetch_twse_day_trade", fake_fetch)
    monkeypatch.setattr(run, "update_market_day_trade_batch", fake_update)
    return state


def test_refetches_third_previous_trade_date(settle, capsys) -> None:
    run._settle_market_day_trade(None, CFG, TODAY)
    assert run._DAY_TRADE_SETTLE_LAG == 3
    assert settle["prev_queries"] == [(TODAY, 3)]
    assert settle["fetched"] == [TARGET]
    assert settle["updated"] == [[(TARGET, DAY_TRADE)]]
    assert "2026-11-18" in capsys.readouterr().out


def test_not_enough_history_skips_without_request(settle, capsys) -> None:
    settle["target"] = None
    run._settle_market_day_trade(None, CFG, TODAY)
    assert settle["fetched"] == []
    assert settle["updated"] == []
    assert "略過" in capsys.readouterr().out


@pytest.mark.parametrize(
    "exc",
    [
        DataUnavailableError("TWTB4U：很抱歉，沒有符合條件的資料!"),
        DataUnavailableError("TWTB4U 資料日期 2026-11-17 不是請求日 2026-11-18"),
        requests.RequestException("boom"),
    ],
)
def test_fetch_failure_is_logged_without_update(settle, capsys, exc) -> None:
    """Review Focus 5：限流或回別日資料 → 印 log、不 UPDATE、不拋例外。"""
    settle["result"] = exc
    run._settle_market_day_trade(None, CFG, TODAY)
    assert settle["updated"] == []
    assert str(exc) in capsys.readouterr().out


def test_db_error_is_contained(settle, capsys) -> None:
    settle["update_error"] = Exception("db down")
    run._settle_market_day_trade(None, CFG, TODAY)
    assert "db down" in capsys.readouterr().out
