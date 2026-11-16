"""Unit tests: 重寫歷史日後，以隔日公布的前日餘額校正大盤融資餘額（_settle_market_margin）。

處理 D 時寫入的是交易所 D 當日的速報餘額，只有處理 D+1 時才會校正 D。所以
`--date D` 與回補區間的最後一天，重寫後一定停在未校正的值——DEV 的 2025-01-07
就是這樣倒退的。收尾時若 DB 已有 D+1，就抓 D+1 的前日餘額校正 D（整個指令 1 次請求）。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

CFG = SimpleNamespace(database_url="postgres://x")
D = dt.date(2026, 6, 22)
N = dt.date(2026, 6, 23)


@pytest.fixture
def settle(monkeypatch):
    state: dict = {"next": N, "consensus": D, "fetched": [], "corrected": [],
                   "margin": {"margin_balance": 593929137000, "prev_margin_balance": 605041986000}}

    def fake_fetch(session, date):  # noqa: ANN001 - 測試替身
        state["fetched"].append(date)
        if isinstance(state["margin"], Exception):
            raise state["margin"]
        return dict(state["margin"])

    def fake_correct(url, date, prev):  # noqa: ANN001 - 測試替身
        state["corrected"].append((date, prev))
        if isinstance(state.get("correct_error"), Exception):
            raise state["correct_error"]
        return (D, 605041591000, prev, 11995499000, 11995894000)

    monkeypatch.setattr(run, "find_next_market_trade_date", lambda url, d: state["next"])
    monkeypatch.setattr(run, "find_consensus_prev_trade_date", lambda url, d: state["consensus"])
    monkeypatch.setattr(run, "fetch_twse_market_margin", fake_fetch)
    monkeypatch.setattr(run, "correct_prev_margin_balance", fake_correct)
    return state


def test_corrects_date_with_next_days_prev_balance(settle, capsys) -> None:
    run._settle_market_margin(None, CFG, D)
    assert settle["fetched"] == [N]
    assert settle["corrected"] == [(N, 605041986000)]
    assert "2026-06-22" in capsys.readouterr().out


def test_no_next_trade_date_skips_without_request(settle, capsys) -> None:
    settle["next"] = None
    run._settle_market_margin(None, CFG, D)
    assert settle["fetched"] == []
    assert "沒有下一個交易日" in capsys.readouterr().out


def test_db_gap_between_date_and_next_skips_without_request(settle, capsys) -> None:
    """N 的兩表共識前一交易日不是 D（DB 缺日）時，N 的前日餘額指的是別天，不可寫進 D。"""
    settle["consensus"] = dt.date(2026, 6, 18)
    run._settle_market_margin(None, CFG, D)
    assert settle["fetched"] == []
    assert settle["corrected"] == []
    assert "缺日" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [DataUnavailableError("MI_MARGN：很抱歉，沒有符合條件的資料!"),
                                 requests.RequestException("timeout")])
def test_fetch_failure_is_logged_and_skips_correction(settle, capsys, exc) -> None:
    settle["margin"] = exc
    run._settle_market_margin(None, CFG, D)
    assert settle["corrected"] == []
    assert "校正失敗" in capsys.readouterr().out


def test_missing_prev_balance_skips_correction(settle, capsys) -> None:
    settle["margin"] = {"margin_balance": 1, "prev_margin_balance": None}
    run._settle_market_margin(None, CFG, D)
    assert settle["corrected"] == []
    assert "前日餘額" in capsys.readouterr().out


def test_correction_error_is_contained(settle, capsys) -> None:
    """收尾校正是附加動作，DB 例外不可讓整個指令以 traceback 結束。"""
    settle["correct_error"] = RuntimeError("DB 連線中斷")
    run._settle_market_margin(None, CFG, D)
    assert "校正失敗" in capsys.readouterr().out
