"""Unit tests: PerSymbolRangeProvider（無網路）。

契約重點：
- ohlcv().volume 與 insti() 一律回「股」（per-symbol 來源是張，provider 內 ×1000）
- insti_ok 只問「這檔的 MoneyDJ 有沒有抓到」，與市場別無關（MoneyDJ 不分市場）
- MoneyDJ 整段取得失敗 → insti_ok 為 False → 該檔每一天都跳過不寫，月表也不必抓
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

START = dt.date(2025, 7, 1)
END = dt.date(2025, 7, 31)
D = dt.date(2025, 7, 31)


def _moneydj_raw() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["114/07/31"],
        "foreign_net_lots": ["9040"],
        "trust_net_lots": ["-1293"],
        "dealer_net_lots": ["1612"],
        "foreign_holding_pct": ["73.54%"],
        "insti_holding_pct": ["76.79%"],
    })


def _twse_month_df() -> pd.DataFrame:
    return pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct", lambda *a, **k: _moneydj_raw()
    )


def test_ohlcv_returns_shares_not_lots(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    result = provider.ohlcv("2330", D, "twse")
    assert result.close == 1255.0
    assert result.volume == 24_000_000
    assert result.change == -5.0


def test_insti_returns_shares(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.insti("2330", D) == (9_040_000, -1_293_000, 1_612_000)


def test_missing_date_yields_empty_ohlcv(wired) -> None:
    """該日不在月表裡（沒交易 / 停牌）→ 全 None，_build_daily_rows 會跳過該列。"""
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    result = provider.ohlcv("2330", dt.date(2025, 7, 1), "twse")
    assert result.open is None
    assert result.close is None
    assert result.volume is None


def test_insti_ok_ignores_market_type(wired) -> None:
    """per-stock 模式的 gating 只問「這檔的 MoneyDJ 有沒有抓到」，
    沒有市場別這個中間概念（MoneyDJ 不分市場）。"""
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.insti_ok("2330", "twse") is True
    assert provider.insti_ok("2330", "tpex") is True
    assert provider.insti_ok("2330", None) is True
    # 沒預取過的個股一律 False（不可預設放行）
    assert provider.insti_ok("6488", "tpex") is False


def test_insti_ok_false_when_moneydj_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("down")),
    )

    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.insti_ok("2330", "twse") is False
    assert provider.insti("2330", D) == (None, None, None)


def test_moneydj_failure_skips_month_tables_entirely(monkeypatch) -> None:
    """MoneyDJ 失敗的個股不再抓月表。

    兩個理由：那些價格一列都寫不進去（每天都過不了 insti_ok gating），而且
    traded_dates 是空的、限流判準整個失效，等於白打幾十發沒有保護的請求。
    """
    month_calls: list[dt.date] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda session, stock_no, date: month_calls.append(date) or _twse_month_df(),
    )
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("down")),
    )

    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=dt.date(2023, 1, 1), end=dt.date(2025, 12, 31),
    )

    assert month_calls == []
    assert provider.ohlcv("2330", D, "twse").close is None


def test_summary_inputs_are_exposed(monkeypatch) -> None:
    """收尾摘要要用的兩份資料：整檔失敗的個股、判定失敗的月份。"""
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct",
        lambda session, symbol, start, end: (
            (_ for _ in ()).throw(DataUnavailableError("down"))
            if symbol == "2317" else _moneydj_raw()
        ),
    )
    # 2330 的 7 月月表回空，但 MoneyDJ 有 7/31 → 判定為取得失敗
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330", "2317"],
        market_types={"2330": "twse", "2317": "twse"},
        start=START, end=END,
    )

    assert provider.insti_failed_symbols == ["2317"]
    assert provider.failed_months_by_symbol == {"2330": [dt.date(2025, 7, 1)]}


def test_resolved_market_types_exposed(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={},
        start=START, end=END,
    )

    assert provider.resolved_market_types["2330"] == "twse"


def test_moneydj_fetched_once_per_symbol(monkeypatch) -> None:
    """整段只打一發 MoneyDJ —— 這是本設計省下請求數的關鍵之一。"""
    calls: list[tuple] = []

    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )

    def fake(session, symbol, start, end):  # noqa: ANN001 - 測試替身
        calls.append((symbol, start, end))
        return _moneydj_raw()

    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", fake)

    run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=dt.date(2023, 1, 1), end=dt.date(2025, 12, 31),
    )

    assert calls == [("2330", dt.date(2023, 1, 1), dt.date(2025, 12, 31))]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
