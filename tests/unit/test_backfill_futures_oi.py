"""Unit tests: --backfill-futures-oi（只回補 market_daily 的台指期三大法人未平倉欄）。

不變量：
- 整段只打一發期交所（端點吃區間，見 memory/taifex-futures-oi-range-download）。
- 只 UPDATE market_daily 已有的列，不 INSERT（見 update_market_futures_oi_batch）。
"""

from __future__ import annotations

import argparse
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

CFG = SimpleNamespace(database_url="postgres://x")
NOV13 = dt.date(2026, 11, 13)
NOV16 = dt.date(2026, 11, 16)
NOV17 = dt.date(2026, 11, 17)


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date=None, backfill_start="2026-11-01", backfill_end="2026-11-30",
        backfill_stocks=None, backfill_futures_oi=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _raw(*dates: dt.date, foreign: int = -96787) -> pd.DataFrame:
    """fetch_taifex_txf_institutional 的原始字串表，每天 3 個身份別。"""
    rows = []
    for d in dates:
        for who, lots in (("自營商", "1159"), ("投信", "92406"), ("外資及陸資", str(foreign))):
            rows.append({"日期": d.strftime("%Y/%m/%d"), "商品名稱": "臺股期貨", "身份別": who,
                         "多空未平倉口數淨額": lots, "多空未平倉契約金額淨額(千元)": "10"})
    return pd.DataFrame(rows)


@pytest.fixture
def backfill(monkeypatch):
    state: dict = {"db": {NOV13, NOV16}, "taifex": _raw(NOV13, NOV16), "fetched": [],
                   "written": [], "db_queries": []}

    def fake_dates(url, start, end):  # noqa: ANN001 - 測試替身
        state["db_queries"].append((start, end))
        return {d for d in state["db"] if start <= d <= end}

    def fake_fetch(session, start, end):  # noqa: ANN001 - 測試替身
        state["fetched"].append((start, end))
        if isinstance(state["taifex"], Exception):
            raise state["taifex"]
        return state["taifex"]

    def fake_update(url, updates):  # noqa: ANN001 - 測試替身
        state["written"].append(list(updates))
        return len(updates)

    monkeypatch.setattr(run, "load_market_dates_in_range", fake_dates)
    monkeypatch.setattr(run, "fetch_taifex_txf_institutional", fake_fetch)
    monkeypatch.setattr(run, "update_market_futures_oi_batch", fake_update)
    return state


def test_single_request_spans_db_dates_in_range(backfill) -> None:
    run._backfill_futures_oi_command(None, CFG, _args())
    assert backfill["db_queries"] == [(dt.date(2026, 11, 1), dt.date(2026, 11, 30))]
    assert backfill["fetched"] == [(NOV13, NOV16)]


def test_writes_all_eight_columns_for_each_db_date(backfill) -> None:
    run._backfill_futures_oi_command(None, CFG, _args())
    (written,) = backfill["written"]
    assert [d for d, _ in written] == [NOV13, NOV16]
    data = dict(written)[NOV16]
    assert data["txf_foreign_oi_net"] == -96787
    assert data["txf_total_oi_net"] == 1159 + 92406 - 96787
    assert data["txf_total_oi_net_amount"] == 30_000
    assert len(data) == 8


def test_taifex_dates_missing_from_db_are_not_written_and_reported(backfill, capsys) -> None:
    """期交所有、DB 沒有的日子不寫：只有期貨欄的列會打亂 D-1 共識判斷。

    抓取範圍是 DB 已有的最早～最晚日，所以偵測得到的是中間缺的日子。
    """
    backfill["db"] = {NOV13, NOV17}
    backfill["taifex"] = _raw(NOV13, NOV16, NOV17)
    run._backfill_futures_oi_command(None, CFG, _args())
    assert backfill["fetched"] == [(NOV13, NOV17)]
    assert [d for d, _ in backfill["written"][0]] == [NOV13, NOV17]
    out = capsys.readouterr().out
    assert "2026-11-16" in out and "market_daily 沒有" in out


def test_db_dates_missing_from_taifex_are_reported(backfill, capsys) -> None:
    backfill["db"] = {NOV13, NOV16, NOV17}
    backfill["taifex"] = _raw(NOV13, NOV16)
    run._backfill_futures_oi_command(None, CFG, _args())
    assert [d for d, _ in backfill["written"][0]] == [NOV13, NOV16]
    out = capsys.readouterr().out
    assert "2026-11-17" in out and "期交所沒有" in out


def test_no_db_rows_in_range_skips_fetch(backfill, capsys) -> None:
    backfill["db"] = set()
    run._backfill_futures_oi_command(None, CFG, _args())
    assert backfill["fetched"] == []
    assert backfill["written"] == []
    assert "market_daily 無資料" in capsys.readouterr().out


@pytest.mark.parametrize("exc", [DataUnavailableError("日期時間錯誤"),
                                 requests.RequestException("timeout")])
def test_fetch_failure_writes_nothing(backfill, capsys, exc) -> None:
    backfill["taifex"] = exc
    run._backfill_futures_oi_command(None, CFG, _args())
    assert backfill["written"] == []
    assert "取得失敗" in capsys.readouterr().out


def test_format_change_writes_nothing(backfill, capsys) -> None:
    backfill["taifex"] = _raw(NOV13).rename(columns={"身份別": "身分別"})
    run._backfill_futures_oi_command(None, CFG, _args())
    assert backfill["written"] == []
    assert "取得失敗" in capsys.readouterr().out


def test_missing_range_is_an_error(backfill, capsys) -> None:
    run._backfill_futures_oi_command(None, CFG, _args(backfill_end=None))
    assert backfill["fetched"] == []
    assert "需搭配 --backfill-start 和 --backfill-end" in capsys.readouterr().out


def test_reversed_range_is_normalized(backfill) -> None:
    run._backfill_futures_oi_command(
        None, CFG, _args(backfill_start="2026-11-30", backfill_end="2026-11-01")
    )
    assert backfill["db_queries"] == [(dt.date(2026, 11, 1), dt.date(2026, 11, 30))]


def test_backfill_stocks_and_date_are_ignored_with_warning(backfill, capsys) -> None:
    """大盤層級的資料，逐檔限縮沒有意義。"""
    run._backfill_futures_oi_command(
        None, CFG, _args(backfill_stocks="2330", date="2026-11-16")
    )
    out = capsys.readouterr().out
    assert "--backfill-stocks" in out and "--date" in out and "忽略" in out
    assert len(backfill["written"][0]) == 2
