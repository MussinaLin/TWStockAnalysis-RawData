"""Tests for _expected_prev_trade_date and _refresh_prev_day_margin guard."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from tw_stock_rawdata import run


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (dt.date(2026, 5, 18), dt.date(2026, 5, 15)),  # Mon → previous Fri
        (dt.date(2026, 5, 19), dt.date(2026, 5, 18)),  # Tue → Mon
        (dt.date(2026, 5, 20), dt.date(2026, 5, 19)),  # Wed → Tue
        (dt.date(2026, 5, 21), dt.date(2026, 5, 20)),  # Thu → Wed
        (dt.date(2026, 5, 22), dt.date(2026, 5, 21)),  # Fri → Thu
        (dt.date(2026, 5, 23), dt.date(2026, 5, 22)),  # Sat → Fri (defensive)
        (dt.date(2026, 5, 24), dt.date(2026, 5, 22)),  # Sun → Fri (defensive)
    ],
)
def test_expected_prev_trade_date(current: dt.date, expected: dt.date) -> None:
    assert run._expected_prev_trade_date(current) == expected


def _config() -> SimpleNamespace:
    return SimpleNamespace(database_url="postgres://x")


def _holdings(symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "name": ""} for s in symbols])


def test_refresh_prev_day_margin_skips_when_db_prev_mismatches_calendar(monkeypatch, capsys):
    """User's bug: --date 2026-05-20 with DB only up to 2026-05-13 must skip."""
    current = dt.date(2026, 5, 20)
    db_prev = dt.date(2026, 5, 13)

    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev
    )

    fetch_calls: list[tuple] = []
    monkeypatch.setattr(
        run, "fetch_moneydj_margin",
        lambda *a, **kw: fetch_calls.append((a, kw)) or pd.DataFrame(),
    )
    update_calls: list[list] = []
    monkeypatch.setattr(
        run, "update_prev_day_margin_batch",
        lambda _url, updates: update_calls.append(updates) or 0,
    )

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330", "2317"]),
        current_date=current,
        config=_config(),
    )

    out = capsys.readouterr().out
    assert "略過融資融券修正" in out
    assert "2026-05-13" in out
    assert "2026-05-19" in out  # expected D-1
    assert fetch_calls == []  # no MoneyDJ requests
    assert update_calls == []  # no DB writes


def test_refresh_prev_day_margin_proceeds_when_db_prev_matches_calendar(monkeypatch):
    """Normal daily run: DB prev == calendar D-1 → MoneyDJ correction runs."""
    current = dt.date(2026, 5, 20)
    db_prev = dt.date(2026, 5, 19)

    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev
    )

    fetch_calls: list[str] = []

    def fake_fetch(_session, symbol, _start, _end):
        fetch_calls.append(symbol)
        return pd.DataFrame()

    monkeypatch.setattr(run, "fetch_moneydj_margin", fake_fetch)
    monkeypatch.setattr(
        run, "prepare_moneydj_margin",
        lambda _raw: pd.DataFrame(columns=["date", *run._PREV_MARGIN_FIELDS]),
    )

    captured: dict = {}

    def fake_update(_url, updates):
        captured["updates"] = updates
        return 0

    monkeypatch.setattr(run, "update_prev_day_margin_batch", fake_update)

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330", "2317"]),
        current_date=current,
        config=_config(),
    )

    assert fetch_calls == ["2330", "2317"]
    assert captured["updates"] == []  # empty data → no updates accumulated


def test_refresh_prev_day_margin_proceeds_on_monday_with_friday_prev(monkeypatch):
    """Monday's calendar D-1 is the previous Friday — must NOT skip."""
    current = dt.date(2026, 5, 18)  # Monday
    db_prev = dt.date(2026, 5, 15)  # Friday

    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev
    )

    fetch_calls: list[str] = []
    monkeypatch.setattr(
        run, "fetch_moneydj_margin",
        lambda _s, sym, _a, _b: fetch_calls.append(sym) or pd.DataFrame(),
    )
    monkeypatch.setattr(
        run, "prepare_moneydj_margin",
        lambda _raw: pd.DataFrame(columns=["date", *run._PREV_MARGIN_FIELDS]),
    )
    monkeypatch.setattr(run, "update_prev_day_margin_batch", lambda _u, _x: 0)

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330"]),
        current_date=current,
        config=_config(),
    )

    assert fetch_calls == ["2330"]


def test_refresh_prev_day_margin_skips_when_consensus_none(monkeypatch, capsys):
    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date", lambda _url, _d: None
    )
    fetch_calls: list = []
    monkeypatch.setattr(
        run, "fetch_moneydj_margin",
        lambda *a, **kw: fetch_calls.append(a) or pd.DataFrame(),
    )

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330"]),
        current_date=dt.date(2026, 5, 20),
        config=_config(),
    )

    out = capsys.readouterr().out
    assert "無 D-1 共識交易日" in out
    assert fetch_calls == []


def test_refresh_prev_day_margin_uses_date_range_not_single_day(monkeypatch):
    """Regression: MoneyDJ margin 對 c==d 只回 summary，必須以區間查詢。"""
    current = dt.date(2026, 5, 20)
    prev_date = dt.date(2026, 5, 19)

    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date", lambda _url, _d: prev_date
    )

    captured: list[tuple[dt.date, dt.date]] = []

    def fake_fetch(_session, _symbol, start, end):
        captured.append((start, end))
        return pd.DataFrame()

    monkeypatch.setattr(run, "fetch_moneydj_margin", fake_fetch)
    monkeypatch.setattr(
        run, "prepare_moneydj_margin",
        lambda _raw: pd.DataFrame(columns=["date", *run._PREV_MARGIN_FIELDS]),
    )
    monkeypatch.setattr(run, "update_prev_day_margin_batch", lambda _u, _x: 0)

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330"]),
        current_date=current,
        config=_config(),
    )

    assert len(captured) == 1
    start, end = captured[0]
    assert end == prev_date
    assert start < prev_date, "start 必須早於 prev_date，避免單日查詢"
    assert (prev_date - start).days >= 7, "緩衝範圍至少 7 天以覆蓋連假"


def test_refresh_prev_day_margin_skips_empty_holdings(monkeypatch):
    """Empty holdings: short-circuit before DB call."""
    consensus_calls: list = []
    monkeypatch.setattr(
        run, "find_consensus_prev_trade_date",
        lambda _url, _d: consensus_calls.append(1) or dt.date(2026, 5, 19),
    )

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings([]),
        current_date=dt.date(2026, 5, 20),
        config=_config(),
    )

    assert consensus_calls == []
