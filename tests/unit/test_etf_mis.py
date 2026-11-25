"""Unit tests: TWSE MIS all_etf.txt 的抓取與規模解析。

all_etf.txt 是 1 次請求回全部 ETF 的當下快照：c 已發行受益權單位數、h 前一營業日
單位淨值、i 資料日期。同一份回應裡數值有的是 JSON number、有的是字串，兩種都要吃。
規模口徑同 e添富：c × h（0050：264.33 億單位 × 94.88 = 25,079 億元）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from tests.conftest import FakeSession
from tw_stock_rawdata.prepare import prepare_mis_etf_rows
from tw_stock_rawdata.sources import DataUnavailableError, fetch_mis_etf_snapshot

ENTRIES = [
    {"a": "0050", "c": 26433000000, "h": "94.8800", "i": "20261125"},
    {"a": "00981A", "c": "13167709000", "h": "21.55", "i": "20261125"},
    {"a": "0056", "c": "-", "h": "48.68", "i": "20261125"},
    {"a": "00878", "c": "20205790000", "h": "30.19", "i": "20261124"},
]
D = dt.date(2026, 11, 25)


def test_number_and_string_values() -> None:
    rows, problems = prepare_mis_etf_rows(ENTRIES, ["0050", "00981A"], D)
    assert rows == [
        {"symbol": "0050", "trade_date": D, "units": 26433000000,
         "nav": Decimal("94.8800"), "aum_100m": Decimal("25079.63")},
        {"symbol": "00981A", "trade_date": D, "units": 13167709000,
         "nav": Decimal("21.5500"), "aum_100m": Decimal("2837.64")},
    ]
    assert problems == {}


def test_problems_reported_not_written() -> None:
    rows, problems = prepare_mis_etf_rows(ENTRIES, ["0056", "00878", "00919"], D)
    assert rows == []
    assert set(problems) == {"0056", "00878", "00919"}
    assert "20261124" in problems["00878"]


@pytest.mark.parametrize("bad", [None, "", "0", "-1", 0, "abc"])
def test_non_positive_or_blank_nav_is_problem(bad) -> None:
    entries = [{"a": "0050", "c": 26433000000, "h": bad, "i": "20261125"}]
    rows, problems = prepare_mis_etf_rows(entries, ["0050"], D)
    assert rows == [] and "0050" in problems


def test_fetch_flattens_msg_array() -> None:
    payload = {"a1": [{"msgArray": ENTRIES[:2]}, {"msgArray": ENTRIES[2:]}]}
    assert len(fetch_mis_etf_snapshot(FakeSession(payload=payload))) == 4


@pytest.mark.parametrize("payload", [[], {"a1": "x"}, {"a1": [{"msgArray": []}]}, {"a1": [1]}])
def test_fetch_bad_shape_raises(payload) -> None:
    with pytest.raises(DataUnavailableError):
        fetch_mis_etf_snapshot(FakeSession(payload=payload))
