"""Tests for _refresh_prev_day_margin：D-1 的判定（連假、DB 缺日）與 MoneyDJ 查詢區間。"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd

from tw_stock_rawdata import run


def _config() -> SimpleNamespace:
    return SimpleNamespace(database_url="postgres://x")


def _holdings(symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "name": ""} for s in symbols])


def _mdj(*days: dt.date) -> pd.DataFrame:
    """prepare_moneydj_margin 的輸出：每個交易日一列，margin_balance 以日序編號。"""
    rows = [{"date": d, **{c: i for c in run._PREV_MARGIN_FIELDS}}
            for i, d in enumerate(days, 1)]
    return pd.DataFrame(rows, columns=["date", *run._PREV_MARGIN_FIELDS])


def _fake_moneydj(monkeypatch, frames: dict[str, pd.DataFrame]):
    """fetch 回傳代號、prepare 依代號回該檔的資料；回傳 (fetch 紀錄, update 紀錄)。"""
    fetch_calls: list[tuple[str, dt.date, dt.date]] = []
    update_calls: list[list] = []

    def fake_fetch(_session, symbol, start, end):
        fetch_calls.append((symbol, start, end))
        return symbol

    monkeypatch.setattr(run, "fetch_moneydj_margin", fake_fetch)
    monkeypatch.setattr(run, "prepare_moneydj_margin", lambda symbol: frames[symbol])
    monkeypatch.setattr(
        run, "update_prev_day_margin_batch",
        lambda _url, updates: update_calls.append(updates) or len(updates),
    )
    return fetch_calls, update_calls


def test_refresh_prev_day_margin_corrects_d1_after_holiday(monkeypatch):
    """春節後第一個交易日：D-1 是 9 個日曆日前的 01-22，仍要修正（舊版會誤判跳過）。"""
    current = dt.date(2025, 2, 3)
    db_prev = dt.date(2025, 1, 22)
    monkeypatch.setattr(run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev)
    fetch_calls, update_calls = _fake_moneydj(monkeypatch, {
        "2330": _mdj(dt.date(2025, 1, 20), dt.date(2025, 1, 21), db_prev),
    })

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330"]),
        current_date=current,
        config=_config(),
    )

    assert [(s, end) for s, _start, end in fetch_calls] == [("2330", dt.date(2025, 2, 2))]
    assert len(update_calls) == 1
    [(symbol, trade_date, data)] = update_calls[0]
    assert (symbol, trade_date) == ("2330", db_prev)
    assert data["margin_balance"] == 3


def test_refresh_prev_day_margin_skips_when_db_missing_trade_days(monkeypatch, capsys):
    """部分回補：--date 2026-05-20 但 DB 只到 05-13；MoneyDJ 顯示 05-14 有交易 → 整批不寫。"""
    current = dt.date(2026, 5, 20)
    db_prev = dt.date(2026, 5, 13)
    monkeypatch.setattr(run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev)
    fetch_calls, update_calls = _fake_moneydj(monkeypatch, {
        "2330": _mdj(db_prev, dt.date(2026, 5, 14), dt.date(2026, 5, 15)),
        "2317": _mdj(db_prev, dt.date(2026, 5, 14), dt.date(2026, 5, 15)),
    })

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["2330", "2317"]),
        current_date=current,
        config=_config(),
    )

    out = capsys.readouterr().out
    assert "略過融資融券修正" in out
    assert "2026-05-13" in out
    assert "2026-05-14" in out  # DB 缺的交易日
    assert [s for s, _start, _end in fetch_calls] == ["2330"]  # 第一檔就發現，只多 1 次請求
    assert update_calls == []  # no DB writes


def test_refresh_prev_day_margin_gap_found_later_discards_collected_updates(monkeypatch):
    """前幾檔沒資料或停牌看不出缺日，後面某檔才發現：已取得的也不寫。"""
    current = dt.date(2026, 5, 20)
    db_prev = dt.date(2026, 5, 13)
    monkeypatch.setattr(run, "find_consensus_prev_trade_date", lambda _url, _d: db_prev)
    fetch_calls, update_calls = _fake_moneydj(monkeypatch, {
        "1101": _mdj(dt.date(2026, 5, 12), db_prev),  # 05-14 起停牌
        "9999": _mdj(),  # 區間內完全沒有資料
        "2330": _mdj(db_prev, dt.date(2026, 5, 14)),
        "2317": _mdj(db_prev, dt.date(2026, 5, 14)),
    })

    run._refresh_prev_day_margin(
        session=object(),
        holdings=_holdings(["1101", "9999", "2330", "2317"]),
        current_date=current,
        config=_config(),
    )

    assert [s for s, _start, _end in fetch_calls] == ["1101", "9999", "2330"]
    assert update_calls == []


def test_refresh_prev_day_margin_proceeds_on_normal_day(monkeypatch):
    """Normal daily run: DB prev == previous weekday → MoneyDJ correction runs."""
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
