"""Unit tests: CLI 總分派（_main_inner）。

docs/refactor-plan.md T9。CC 32、覆蓋率 41%、212 行，fan-in 只有 1，
但它決定所有子命令的走向——改壞會同時影響每一條路徑。

本檔只測「分派」與「前置閘門」：哪個旗標走哪個分支、走了之後有沒有停下來、
以及休市開關的 fail-open 行為。各子命令自身的邏輯另有專屬測試檔。
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd
import psycopg
import pytest

from tw_stock_rawdata import run
from tw_stock_rawdata.config import AppConfig

CONFIG = AppConfig(database_url="postgres://x", use_db=True)
TODAY = dt.date(2026, 8, 21)


def _args(**kw) -> argparse.Namespace:
    base = {
        "date": None, "backfill_start": None, "backfill_end": None,
        "backfill_stocks": None, "backfill_limits": False,
        "backfill_disposition": False, "update_shares": False, "dahu": False,
        "force": False, "stocks": None, "from_date": None, "to_date": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def spies(monkeypatch):
    """把每個分支的入口換成計數器，並隔絕所有 I/O。"""
    calls: dict[str, int] = {}

    def spy(name, retval=None):
        def _fn(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            return retval
        return _fn

    monkeypatch.setattr(run, "build_session", spy("build_session"))
    monkeypatch.setattr(run, "_update_shares_command", spy("update_shares"))
    monkeypatch.setattr(run, "_dahu_command", spy("dahu"))
    monkeypatch.setattr(run, "_backfill_limits_command", spy("backfill_limits"))
    monkeypatch.setattr(run, "_backfill_disposition_command", spy("backfill_disposition"))
    monkeypatch.setattr(run, "_run_for_date", spy("run_for_date", retval=True))
    monkeypatch.setattr(run, "_refresh_prev_day_margin", spy("refresh_prev_margin"))
    monkeypatch.setattr(run, "_prefetch_margin_cache", spy("prefetch_margin", retval={}))
    monkeypatch.setattr(run, "_prefetch_holding_pct_cache", spy("prefetch_holding", retval={}))
    monkeypatch.setattr(run, "_fetch_disposition", spy("fetch_disposition", retval=None))
    monkeypatch.setattr(run, "_get_issued_shares", spy("issued_shares", retval={}))
    monkeypatch.setattr(run, "_print_backfill_stocks_summary", spy("summary"))
    monkeypatch.setattr(run, "load_stock_names", spy("load_names", retval={}))
    monkeypatch.setattr(run, "load_market_types", spy("load_market_types", retval={}))
    monkeypatch.setattr(
        run, "get_enabled_stocks",
        spy("enabled", retval=[("2330", "台積電", 24, "半導體", "twse")]),
    )
    monkeypatch.setattr(run, "get_config_value", spy("config_value", retval="true"))

    class _Provider:
        holding_pct_cache: dict = {}
        resolved_market_types: dict = {}

    monkeypatch.setattr(
        run.PerSymbolRangeProvider, "build",
        staticmethod(lambda **k: (calls.__setitem__("provider_build", 1), _Provider())[1]),
    )
    return calls


class TestSubcommandRouting:
    """每個旗標各自走自己的分支，且走完就停——不可再跑到後面的模式。"""

    @pytest.mark.parametrize("flag,expected", [
        ("update_shares", "update_shares"),
        ("dahu", "dahu"),
        ("backfill_limits", "backfill_limits"),
        ("backfill_disposition", "backfill_disposition"),
    ])
    def test_flag_routes_and_returns(self, spies, flag, expected) -> None:
        run._main_inner(CONFIG, _args(**{flag: True}), TODAY, TODAY)
        assert spies.get(expected) == 1
        assert "run_for_date" not in spies
        assert "enabled" not in spies

    def test_precedence_update_shares_wins(self, spies) -> None:
        """多個旗標同時給時的優先序：update_shares 最前面。"""
        run._main_inner(
            CONFIG, _args(update_shares=True, dahu=True, backfill_limits=True), TODAY, TODAY
        )
        assert spies.get("update_shares") == 1
        assert "dahu" not in spies
        assert "backfill_limits" not in spies

    def test_precedence_dahu_before_backfill_limits(self, spies) -> None:
        run._main_inner(CONFIG, _args(dahu=True, backfill_limits=True), TODAY, TODAY)
        assert spies.get("dahu") == 1
        assert "backfill_limits" not in spies


class TestTradingDayGate:
    """休市開關只擋純 daily 模式，且讀不到一律 fail-open。"""

    def test_daily_mode_stops_when_not_trading_day(self, spies, monkeypatch) -> None:
        monkeypatch.setattr(run, "get_config_value", lambda url, key: "false")
        run._main_inner(CONFIG, _args(), TODAY, TODAY)
        assert "build_session" not in spies
        assert "run_for_date" not in spies

    def test_daily_mode_proceeds_when_trading_day(self, spies) -> None:
        run._main_inner(CONFIG, _args(), TODAY, TODAY)
        assert spies.get("run_for_date") == 1

    def test_db_error_fails_open(self, spies, monkeypatch) -> None:
        """讀不到 is_trading_day 時視為交易日照常執行——排程不可因 DB 抖動整天不跑。"""
        def boom(url, key):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(run, "get_config_value", boom)
        run._main_inner(CONFIG, _args(), TODAY, TODAY)
        assert spies.get("run_for_date") == 1

    @pytest.mark.parametrize("kw", [
        {"date": "2026-08-21"}, {"backfill_start": "2026-08-01"},
        {"backfill_stocks": "2330"}, {"update_shares": True}, {"dahu": True},
    ])
    def test_non_daily_modes_skip_the_gate(self, spies, monkeypatch, kw) -> None:
        """手動操作不受休市開關影響，隨時可跑。"""
        monkeypatch.setattr(run, "get_config_value", lambda url, key: "false")
        run._main_inner(CONFIG, _args(**kw), TODAY, TODAY)
        assert "config_value" not in spies


class TestBackfillStocksGuards:
    def test_requires_start_and_end(self, spies) -> None:
        run._main_inner(CONFIG, _args(backfill_stocks="2330"), TODAY, TODAY)
        assert "run_for_date" not in spies
        assert "provider_build" not in spies

    def test_blank_stock_list_aborts(self, spies) -> None:
        run._main_inner(
            CONFIG,
            _args(backfill_stocks=" , ", backfill_start="2026-08-01", backfill_end="2026-08-02"),
            TODAY, TODAY,
        )
        assert "provider_build" not in spies

    def test_does_not_write_market_daily(self, spies, monkeypatch) -> None:
        """--backfill-stocks 不可動共用的 market_daily（CLAUDE.md 不變量）。"""
        seen = {}

        def capture(*a, **k):
            seen.update(k)
            return True

        monkeypatch.setattr(run, "_run_for_date", capture)
        run._main_inner(
            CONFIG,
            _args(backfill_stocks="2330", backfill_start="2026-08-01", backfill_end="2026-08-01"),
            TODAY, TODAY,
        )
        assert seen["write_market_daily"] is False

    def test_reversed_range_is_normalized(self, spies, monkeypatch) -> None:
        """起訖顛倒時要正規化——否則 _month_starts 回空 list，整段靜默 no-op。"""
        seen = {}
        monkeypatch.setattr(
            run.PerSymbolRangeProvider, "build",
            staticmethod(lambda **k: seen.update(k) or type("P", (), {
                "holding_pct_cache": {}, "resolved_market_types": {}})()),
        )
        run._main_inner(
            CONFIG,
            _args(backfill_stocks="2330", backfill_start="2026-08-05", backfill_end="2026-08-01"),
            TODAY, TODAY,
        )
        assert seen["start"] == dt.date(2026, 8, 1)
        assert seen["end"] == dt.date(2026, 8, 5)


class TestGeneralBackfillAndSingleDate:
    def test_no_enabled_stocks_aborts(self, spies, monkeypatch) -> None:
        monkeypatch.setattr(run, "get_enabled_stocks", lambda url: [])
        run._main_inner(CONFIG, _args(date="2026-08-21"), TODAY, TODAY)
        assert "run_for_date" not in spies

    def test_backfill_range_runs_each_date(self, spies) -> None:
        run._main_inner(
            CONFIG, _args(backfill_start="2026-08-01", backfill_end="2026-08-03"), TODAY, TODAY
        )
        assert spies.get("run_for_date") == 3

    def test_backfill_start_only_uses_target_date_as_end(self, spies) -> None:
        run._main_inner(
            CONFIG, _args(backfill_start="2026-08-19"), TODAY, dt.date(2026, 8, 21)
        )
        assert spies.get("run_for_date") == 3

    def test_force_disables_skip_existing(self, spies, monkeypatch) -> None:
        seen = {}
        monkeypatch.setattr(run, "_run_for_date", lambda *a, **k: seen.update(k) or True)
        run._main_inner(
            CONFIG, _args(backfill_start="2026-08-01", backfill_end="2026-08-01", force=True),
            TODAY, TODAY,
        )
        assert seen["skip_existing"] is False

    def test_without_force_skips_existing(self, spies, monkeypatch) -> None:
        seen = {}
        monkeypatch.setattr(run, "_run_for_date", lambda *a, **k: seen.update(k) or True)
        run._main_inner(
            CONFIG, _args(backfill_start="2026-08-01", backfill_end="2026-08-01"), TODAY, TODAY
        )
        assert seen["skip_existing"] is True

    def test_prev_margin_refresh_only_when_something_written(self, spies, monkeypatch) -> None:
        monkeypatch.setattr(run, "_run_for_date", lambda *a, **k: False)
        run._main_inner(
            CONFIG, _args(backfill_start="2026-08-01", backfill_end="2026-08-02"), TODAY, TODAY
        )
        assert "refresh_prev_margin" not in spies

    def test_single_date_mode_runs_once(self, spies) -> None:
        run._main_inner(CONFIG, _args(date="2026-08-21"), TODAY, dt.date(2026, 8, 21))
        assert spies.get("run_for_date") == 1
        assert "prefetch_margin" not in spies


def test_backfill_end_only_uses_target_date_as_start(spies) -> None:
    """只給 --backfill-end 時，起始日用 target_date（今天或 --date）。"""
    run._main_inner(
        CONFIG, _args(backfill_end="2026-08-21"), TODAY, dt.date(2026, 8, 19)
    )
    assert spies.get("run_for_date") == 3
