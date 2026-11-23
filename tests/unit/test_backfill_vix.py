"""Unit tests: --backfill-vix 回補 market_daily 的台指 VIX 收盤值。

- 期交所月檔只保留近 4 個月，每月 1 次請求；按 DB 已有日期涵蓋的月份逐月抓。
- 只 UPDATE market_daily 已有的列，不 INSERT（見 update_market_vix_batch）。
- 某月抓不到（太舊或其他錯誤）印警告、繼續下一個月。
"""

from __future__ import annotations

import argparse
import datetime as dt
from types import SimpleNamespace

import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

CFG = SimpleNamespace(database_url="postgres://x")
OCT30 = dt.date(2026, 10, 30)
NOV02 = dt.date(2026, 11, 2)
NOV20 = dt.date(2026, 11, 20)


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date=None, backfill_start="2026-10-01", backfill_end="2026-11-30",
        backfill_stocks=None, backfill_vix=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def backfill(monkeypatch):
    state: dict = {
        "db": {OCT30, NOV02, NOV20},
        "months": {
            (2026, 10): {OCT30: 30.5},
            (2026, 11): {NOV02: 31.3, NOV20: 52.13},
        },
        "fetched": [], "written": [],
    }

    def fake_dates(url, start, end):  # noqa: ANN001 - 測試替身
        return {d for d in state["db"] if start <= d <= end}

    def fake_month(session, month):  # noqa: ANN001 - 測試替身
        key = (month.year, month.month)
        state["fetched"].append(key)
        value = state["months"].get(key, DataUnavailableError("HTML"))
        if isinstance(value, Exception):
            raise value
        return value

    def fake_update(url, updates):  # noqa: ANN001 - 測試替身
        state["written"].append(list(updates))
        return len(updates)

    monkeypatch.setattr(run, "load_market_dates_in_range", fake_dates)
    monkeypatch.setattr(run, "fetch_taifex_vix_month", fake_month)
    monkeypatch.setattr(run, "update_market_vix_batch", fake_update)
    return state


def _written(state) -> dict:
    assert len(state["written"]) == 1
    return {d: data["tw_vix_close"] for d, data in state["written"][0]}


def test_fetches_once_per_month_covering_db_dates(backfill) -> None:
    run._backfill_vix_command(None, CFG, _args())
    assert backfill["fetched"] == [(2026, 10), (2026, 11)]


def test_updates_only_db_dates(backfill, capsys) -> None:
    backfill["months"][(2026, 11)][dt.date(2026, 11, 23)] = 55.36
    run._backfill_vix_command(None, CFG, _args())
    assert _written(backfill) == {OCT30: 30.5, NOV02: 31.3, NOV20: 52.13}
    out = capsys.readouterr().out
    assert "ℹ" in out and "2026-11-23" in out


def test_db_dates_missing_from_taifex_are_reported(backfill, capsys) -> None:
    del backfill["months"][(2026, 11)][NOV02]
    run._backfill_vix_command(None, CFG, _args())
    assert NOV02 not in _written(backfill)
    out = capsys.readouterr().out
    assert "⚠" in out and "2026-11-02" in out


@pytest.mark.parametrize(
    "exc", [DataUnavailableError("期交所回傳 HTML"), requests.RequestException("boom")]
)
def test_unavailable_month_is_warned_and_others_continue(backfill, capsys, exc) -> None:
    backfill["months"][(2026, 10)] = exc
    run._backfill_vix_command(None, CFG, _args())
    assert _written(backfill) == {NOV02: 31.3, NOV20: 52.13}
    out = capsys.readouterr().out
    assert "2026-10" in out and str(exc) in out


def test_no_db_rows_in_range_skips_fetch(backfill, capsys) -> None:
    backfill["db"] = set()
    run._backfill_vix_command(None, CFG, _args())
    assert backfill["fetched"] == []
    assert backfill["written"] == []
    assert "無資料" in capsys.readouterr().out


def test_missing_range_is_an_error(backfill, capsys) -> None:
    run._backfill_vix_command(None, CFG, _args(backfill_end=None))
    assert backfill["fetched"] == []
    assert "需搭配 --backfill-start 和 --backfill-end" in capsys.readouterr().out


def test_backfill_stocks_and_date_are_ignored_with_warning(backfill, capsys) -> None:
    run._backfill_vix_command(None, CFG, _args(backfill_stocks="2330", date="2026-11-16"))
    out = capsys.readouterr().out
    assert "--backfill-stocks" in out and "--date" in out and "忽略" in out
    assert len(backfill["written"][0]) == 3


def test_is_not_daily_mode() -> None:
    args = argparse.Namespace(
        date=None, backfill_start=None, backfill_end=None, backfill_stocks=None,
        backfill_limits=False, backfill_disposition=False, backfill_trust_holding=False,
        backfill_margin=False, backfill_futures_oi=False, backfill_vix=True,
        update_shares=False, dahu=False,
    )
    assert run._is_daily_mode(args) is False
