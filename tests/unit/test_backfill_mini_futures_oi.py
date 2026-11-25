"""Unit tests: 一次性回補 tools/backfill_mini_futures_oi.py 的純函式。

期交所行情端點一次最多查一個月，所以按日曆月切段；小台法人端點只保留近三年，
微台從上市日 2024-07-29 起；只寫 market_daily 已有的日期（不 INSERT）。
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "tools" / "backfill_mini_futures_oi.py"
_spec = importlib.util.spec_from_file_location("backfill_mini_futures_oi", _PATH)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


def test_month_chunks_calendar_months() -> None:
    assert script.month_chunks(date(2026, 9, 15), date(2026, 11, 24)) == [
        (date(2026, 9, 15), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 24)),
    ]


def test_month_chunks_single_day() -> None:
    d = date(2026, 11, 24)
    assert script.month_chunks(d, d) == [(d, d)]


def test_month_chunks_crosses_year() -> None:
    assert script.month_chunks(date(2025, 12, 20), date(2026, 1, 5)) == [
        (date(2025, 12, 20), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 5)),
    ]


def test_month_chunks_february_leap_year() -> None:
    assert script.month_chunks(date(2028, 2, 1), date(2028, 3, 1)) == [
        (date(2028, 2, 1), date(2028, 2, 29)),
        (date(2028, 3, 1), date(2028, 3, 1)),
    ]


def test_floor_for_tmf_is_listing_date() -> None:
    assert script.floor_for("tmf", date(2026, 11, 25)) == date(2024, 7, 29)


def test_floor_for_mtx_is_three_years() -> None:
    assert script.floor_for("mtx", date(2026, 11, 25)) == date(2023, 11, 25)


def test_floor_for_mtx_leap_day() -> None:
    assert script.floor_for("mtx", date(2028, 2, 29)) == date(2025, 2, 28)


def test_merge_updates_only_db_dates_and_reports_extra() -> None:
    d1, d2, d3 = date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24)
    inst = {d1: {"mtx_inst_long_oi": 1, "mtx_inst_short_oi": 2}, d3: {"mtx_inst_long_oi": 9}}
    mkt = {d1: {"mtx_market_oi": 3}, d2: {"mtx_market_oi": 4}}
    updates, extra = script.merge_updates([d1, d2], inst, mkt)
    assert updates == [
        (d1, {"mtx_inst_long_oi": 1, "mtx_inst_short_oi": 2, "mtx_market_oi": 3}),
        (d2, {"mtx_market_oi": 4}),
    ]
    assert extra == [d3]


def test_merge_updates_skips_db_dates_without_data() -> None:
    d1, d2 = date(2026, 11, 20), date(2026, 11, 23)
    updates, extra = script.merge_updates([d1, d2], {d1: {"mtx_market_oi": 3}})
    assert updates == [(d1, {"mtx_market_oi": 3})] and extra == []


def test_main_stops_before_fetching_when_columns_missing(monkeypatch) -> None:
    """還沒跑過新版 init_schema 的 DB（如部署前的 PROD）：模擬不能假綠燈、--apply 不能打完請求才炸。"""
    import pytest

    def boom(*a, **k):
        raise AssertionError("欄位不存在時不該讀 DB 日期或打期交所")

    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example.invalid:5432/db")
    monkeypatch.setattr("sys.argv", ["backfill_mini_futures_oi.py"])
    monkeypatch.setattr(script, "load_missing_mini_columns", lambda url: ["mtx_market_oi", "tmf_market_oi"])
    monkeypatch.setattr(script, "load_market_dates_in_range", boom)
    monkeypatch.setattr(script, "build_session", boom)
    monkeypatch.setattr(script, "close_pool", lambda: None)
    with pytest.raises(SystemExit, match="mtx_market_oi.*tw-stock-rawdata"):
        script.main()
