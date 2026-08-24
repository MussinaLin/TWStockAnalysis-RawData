"""Unit tests: --dahu（TDCC 集保戶股權分散表）子命令。

docs/refactor-plan.md T3。此命令原本覆蓋率 2%（72 行只有 1 行被執行），
CC 17，且是唯一寫 stock_daily_raw.foreign/insti holding percent 的路徑之一。
本檔涵蓋標的選取、日期解析、TDCC 失敗處理與重試、以及寫入筆數統計。

不碰網路與 DB：session 與 db_utils / sources 的呼叫全部 monkeypatch。
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.config import AppConfig
from tw_stock_rawdata.sources import DataUnavailableError

CONFIG = AppConfig(database_url="postgres://x", use_db=True)
D1 = dt.date(2026, 8, 7)
D2 = dt.date(2026, 8, 14)


def _args(**kw) -> argparse.Namespace:
    base = {"stocks": None, "from_date": None, "to_date": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _dist() -> pd.DataFrame:
    return pd.DataFrame({"grade": ["15"], "pct": ["50.0"]})


@pytest.fixture
def wiring(monkeypatch):
    """把所有外部相依換成可觀測的假物件，回傳呼叫紀錄。"""
    calls = {"upserts": [], "fetches": [], "tokens": 0}

    monkeypatch.setattr(run, "load_stock_names", lambda url: {"2330": "台積電"})
    monkeypatch.setattr(run, "get_enabled_stocks", lambda url: [("2330", "台積電"), ("2317", "鴻海")])

    def fake_token(session):
        calls["tokens"] += 1
        return "tok", [D1, D2]

    monkeypatch.setattr(run, "fetch_tdcc_token_and_dates", fake_token)

    def fake_dist(session, token, symbol, date):
        calls["fetches"].append((symbol, date))
        return _dist(), "tok2"

    monkeypatch.setattr(run, "fetch_tdcc_distribution", fake_dist)
    monkeypatch.setattr(run, "prepare_tdcc_major_ratio", lambda d: 0.55)
    monkeypatch.setattr(run, "prepare_tdcc_retail_ratio", lambda d: 0.20)

    def fake_upsert(url, date, rows):
        calls["upserts"].append((date, list(rows)))
        return len(rows)

    monkeypatch.setattr(run, "upsert_holder_percent", fake_upsert)
    monkeypatch.setattr(run.time, "sleep", lambda _s: None)
    return calls


class TestTargetSelection:
    def test_explicit_stocks_uses_name_map(self, wiring) -> None:
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        date, rows = wiring["upserts"][0]
        assert rows == [("2330", "台積電", 0.55, 0.20)]

    def test_explicit_stocks_strips_and_skips_blanks(self, wiring) -> None:
        run._dahu_command(None, CONFIG, _args(stocks=" 2330 , ,2317 "), D2)
        _, rows = wiring["upserts"][0]
        assert [r[0] for r in rows] == ["2330", "2317"]

    def test_blank_stocks_arg_aborts(self, wiring) -> None:
        run._dahu_command(None, CONFIG, _args(stocks=" , "), D2)
        assert wiring["upserts"] == []
        assert wiring["tokens"] == 0

    def test_no_stocks_uses_enabled_list(self, wiring) -> None:
        run._dahu_command(None, CONFIG, _args(), D2)
        _, rows = wiring["upserts"][0]
        assert [r[0] for r in rows] == ["2330", "2317"]

    def test_empty_enabled_list_aborts(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(run, "get_enabled_stocks", lambda url: [])
        run._dahu_command(None, CONFIG, _args(), D2)
        assert wiring["upserts"] == []
        assert wiring["tokens"] == 0


class TestDateResolution:
    def test_no_range_uses_latest_date_only(self, wiring) -> None:
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        assert [d for d, _ in wiring["upserts"]] == [D2]

    def test_range_selects_all_dates_in_window(self, wiring) -> None:
        run._dahu_command(
            None, CONFIG, _args(stocks="2330", from_date="2026-08-01", to_date="2026-08-31"), D2
        )
        assert [d for d, _ in wiring["upserts"]] == [D1, D2]

    def test_range_outside_available_writes_nothing(self, wiring) -> None:
        run._dahu_command(
            None, CONFIG, _args(stocks="2330", from_date="2026-09-01", to_date="2026-09-30"), D2
        )
        assert wiring["upserts"] == []


class TestFailureHandling:
    def test_token_fetch_failure_aborts_before_any_write(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(
            run, "fetch_tdcc_token_and_dates",
            lambda s: (_ for _ in ()).throw(DataUnavailableError("TDCC 頁面異常")),
        )
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        assert wiring["upserts"] == []

    def test_distribution_failure_retries_then_skips_symbol(self, monkeypatch, wiring) -> None:
        """單檔連續失敗 _TDCC_MAX_ATTEMPTS 次後跳過該檔，不中斷其他檔。"""
        def flaky(session, token, symbol, date):
            wiring["fetches"].append((symbol, date))
            if symbol == "2330":
                raise requests.RequestException("boom")
            return _dist(), "tok2"

        monkeypatch.setattr(run, "fetch_tdcc_distribution", flaky)
        run._dahu_command(None, CONFIG, _args(), D2)

        attempts_2330 = [c for c in wiring["fetches"] if c[0] == "2330"]
        assert len(attempts_2330) == run._TDCC_MAX_ATTEMPTS
        _, rows = wiring["upserts"][0]
        assert [r[0] for r in rows] == ["2317"]

    def test_unparseable_ratio_skips_symbol(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(run, "prepare_tdcc_major_ratio", lambda d: None)
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        _, rows = wiring["upserts"][0]
        assert rows == []

    def test_retail_none_still_writes_row(self, monkeypatch, wiring) -> None:
        """散戶比例偶發 None 不擋寫——COALESCE 會保護歷史值。"""
        monkeypatch.setattr(run, "prepare_tdcc_retail_ratio", lambda d: None)
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        _, rows = wiring["upserts"][0]
        assert rows == [("2330", "台積電", 0.55, None)]

    def test_empty_name_becomes_none(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(run, "load_stock_names", lambda url: {})
        run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)
        _, rows = wiring["upserts"][0]
        assert rows[0][1] is None


def test_token_refresh_failure_does_not_abort_retry_loop(monkeypatch, wiring) -> None:
    """取分散表失敗後、換 token 也失敗時仍要把重試跑完，不讓例外逸出。

    token 為單次有效，失敗時手上的 token 狀態未知，故每次失敗都嘗試換新的；
    但換 token 本身也可能失敗，那時只能沿用舊 token 再試。
    """
    monkeypatch.setattr(
        run, "fetch_tdcc_distribution",
        lambda s, t, sym, d: (_ for _ in ()).throw(requests.RequestException("boom")),
    )
    calls = {"n": 0}

    def token_then_fail(session):
        calls["n"] += 1
        if calls["n"] == 1:
            return "tok", [D1, D2]
        raise DataUnavailableError("TDCC 頁面暫時異常")

    monkeypatch.setattr(run, "fetch_tdcc_token_and_dates", token_then_fail)

    run._dahu_command(None, CONFIG, _args(stocks="2330"), D2)

    # 首次取 token + 每次失敗後各換一次
    assert calls["n"] == 1 + run._TDCC_MAX_ATTEMPTS
    _, rows = wiring["upserts"][0]
    assert rows == []
