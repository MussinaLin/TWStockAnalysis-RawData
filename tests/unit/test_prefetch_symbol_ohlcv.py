"""Unit tests: 單檔區間 OHLCV 預取（無網路）。

核心風險：www.twse.com.tw 被限流時回 HTTP 200 + stat「很抱歉，沒有符合條件的資料!」，
與「該月真的沒資料」是同一個字串，無法從回應區分（見
memory/twse-rate-limit-ambiguous-response.md）。

唯一可用的外部訊號是 MoneyDJ zcl：它整段只打一發、不經 TWSE，若它證明該月有交易
而交易所月表回空，就判定為限流／取得失敗，該月不寫並警告 —— 而不是靜默當成沒交易。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

START = dt.date(2025, 6, 10)
END = dt.date(2025, 8, 5)


def test_month_starts_covers_partial_edges() -> None:
    assert run._month_starts(START, END) == [
        dt.date(2025, 6, 1), dt.date(2025, 7, 1), dt.date(2025, 8, 1),
    ]


def test_month_starts_single_month() -> None:
    assert run._month_starts(dt.date(2025, 7, 3), dt.date(2025, 7, 20)) == [
        dt.date(2025, 7, 1)
    ]


def test_month_starts_crosses_year_boundary() -> None:
    assert run._month_starts(dt.date(2025, 12, 20), dt.date(2026, 1, 5)) == [
        dt.date(2025, 12, 1), dt.date(2026, 1, 1),
    ]


def _twse_month_df(day: int) -> pd.DataFrame:
    return pd.DataFrame(
        [["114/07/%02d" % day, "24,000,000", "30,000,000", "1250.00",
          "1260.00", "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


def test_twse_symbol_fetches_month_tables(monkeypatch) -> None:
    calls: list[dt.date] = []

    def fake(session, stock_no, date):  # noqa: ANN001 - 測試替身
        calls.append(date)
        return _twse_month_df(15)

    monkeypatch.setattr(run, "fetch_twse_stock_day", fake)

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert calls == [dt.date(2025, 7, 1)]
    assert out.market_type == "twse"
    assert out.failed_months == []
    assert out.by_date[dt.date(2025, 7, 15)].close == 1255.0


def test_tpex_symbol_never_calls_twse(monkeypatch) -> None:
    """上櫃股不可打 TWSE 月表 —— 那支 API 沒有上櫃資料，只是白耗限流配額。"""
    twse_calls: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: twse_calls.append("x") or pd.DataFrame(),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type="tpex",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert twse_calls == []
    assert out.by_date[dt.date(2025, 7, 16)].volume == 3_709_000
    assert out.by_date[dt.date(2025, 7, 16)].change == 18.5


def test_empty_month_without_moneydj_evidence_is_treated_as_no_trading(
    monkeypatch,
) -> None:
    """MoneyDJ 也沒有該月資料 → 該檔那個月本來就沒交易，不算失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert out.by_date == {}
    assert out.failed_months == []


def test_empty_month_with_moneydj_evidence_is_flagged_as_failure(
    monkeypatch,
) -> None:
    """MoneyDJ 證明該月有交易，交易所月表卻回空 → 判定限流／取得失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 7, 15)},
    )

    assert out.by_date == {}
    assert out.failed_months == [dt.date(2025, 7, 1)]


def test_unknown_market_type_probes_twse_first(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse") or _twse_month_df(15),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: order.append("tpex") or pd.DataFrame(),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse"]
    assert out.market_type == "twse"


def test_unknown_market_type_falls_back_to_tpex(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse")
        or (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: order.append("tpex") or pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse", "tpex"]
    assert out.market_type == "tpex"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
