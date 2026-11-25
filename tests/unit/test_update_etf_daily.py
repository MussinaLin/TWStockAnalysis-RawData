"""Unit tests: daily 收尾寫入 ETF 規模（_update_etf_daily）。

MIS all_etf.txt 只有當下快照，所以只在 daily（target_date == today）跑；清單來自
etfs 表；資料日期不是今天的不寫；任何失敗只印 log，不讓 daily 失敗。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

CFG = SimpleNamespace(database_url="postgres://x")
TODAY = dt.date(2026, 11, 25)
ENTRY_0050 = {"a": "0050", "c": 26433000000, "h": "94.8800", "i": "20261125"}


@pytest.fixture
def etf(monkeypatch):
    state: dict = {"etfs": [("0050", "元大台灣50")], "entries": [ENTRY_0050],
                   "fetched": 0, "upserts": [], "upsert_error": None}

    def fake_fetch(session):  # noqa: ANN001 - 測試替身
        state["fetched"] += 1
        if isinstance(state["entries"], Exception):
            raise state["entries"]
        return state["entries"]

    def fake_upsert(url, rows):  # noqa: ANN001 - 測試替身
        if state["upsert_error"] is not None:
            raise state["upsert_error"]
        state["upserts"].append(list(rows))
        return len(rows)

    monkeypatch.setattr(run, "get_enabled_etfs", lambda url: state["etfs"])
    monkeypatch.setattr(run, "fetch_mis_etf_snapshot", fake_fetch)
    monkeypatch.setattr(run, "upsert_etf_daily", fake_upsert)
    return state


def test_writes_rows_for_enabled_etfs(etf) -> None:
    run._update_etf_daily(None, CFG, TODAY)
    assert etf["fetched"] == 1
    [rows] = etf["upserts"]
    assert rows == [{"symbol": "0050", "trade_date": TODAY, "units": 26433000000,
                     "nav": Decimal("94.8800"), "aum_100m": Decimal("25079.63")}]


def test_no_enabled_etf_skips_request(etf) -> None:
    etf["etfs"] = []
    run._update_etf_daily(None, CFG, TODAY)
    assert etf["fetched"] == 0 and etf["upserts"] == []


@pytest.mark.parametrize("exc", [DataUnavailableError("x"), requests.ConnectionError("x")])
def test_fetch_failure_only_logs(etf, capsys, exc) -> None:
    etf["entries"] = exc
    run._update_etf_daily(None, CFG, TODAY)
    assert etf["upserts"] == []
    assert "失敗" in capsys.readouterr().out


def test_db_failure_only_logs(etf, capsys) -> None:
    etf["upsert_error"] = RuntimeError("db down")
    run._update_etf_daily(None, CFG, TODAY)
    assert "失敗" in capsys.readouterr().out


def test_stale_date_not_written(etf, capsys) -> None:
    etf["entries"] = [dict(ENTRY_0050, i="20261124")]
    run._update_etf_daily(None, CFG, TODAY)
    assert etf["upserts"] == []
    out = capsys.readouterr().out
    assert "⚠" in out and "0050" in out
