"""Unit tests for _parse_trading_day（is_trading_day 休市開關解析）.

fail-open 原則：讀不到（None）或無法辨識的值一律視為 True 照常執行，
開關只是輔助，缺了不影響原本抓資料流程。
"""

from __future__ import annotations

import pytest

from tw_stock_rawdata import run


@pytest.mark.parametrize(
    "value",
    ["false", "False", "FALSE", " false ", "0", "no", "No"],
)
def test_parse_trading_day_false_values(value: str) -> None:
    assert run._parse_trading_day(value) is False


@pytest.mark.parametrize(
    "value",
    ["true", "True", "TRUE", " true ", "1", "yes", "Yes"],
)
def test_parse_trading_day_true_values(value: str) -> None:
    assert run._parse_trading_day(value) is True


@pytest.mark.parametrize(
    "value",
    [None, "", "  ", "maybe", "off", "on", "2"],
)
def test_parse_trading_day_fail_open(value: str | None) -> None:
    """key 不存在（None）或值無法辨識 → fail-open 視為 True。"""
    assert run._parse_trading_day(value) is True


def _make_args(**overrides) -> object:
    """組出 _parse_args 會回傳的 Namespace，預設為純 daily 模式（無任何參數）。"""
    import argparse

    defaults = dict(
        date=None,
        backfill_start=None,
        backfill_end=None,
        backfill_stocks=None,
        update_shares=False,
        dahu=False,
        stocks=None,
        from_date=None,
        to_date=None,
        force=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_is_daily_mode_no_args() -> None:
    """無任何參數 → 純 daily 模式，需檢查 is_trading_day 開關。"""
    assert run._is_daily_mode(_make_args()) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"date": "2025-10-15"},
        {"backfill_start": "2025-08-01"},
        {"backfill_end": "2025-10-15"},
        {"backfill_stocks": "2330,2317"},
        {"update_shares": True},
        {"dahu": True},
    ],
)
def test_is_daily_mode_manual_args(overrides: dict) -> None:
    """任何手動參數（含 --date）→ 非 daily 模式，不檢查開關。"""
    assert run._is_daily_mode(_make_args(**overrides)) is False
