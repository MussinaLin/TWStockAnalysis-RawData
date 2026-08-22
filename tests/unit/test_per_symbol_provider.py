"""Unit tests: PerSymbolRangeProvider（無網路）。

契約重點：
- ohlcv().volume 與 insti() 一律回「股」（per-symbol 來源是張，provider 內 ×1000）
- is_tpex 直接回 stocks.market_type —— per-stock 模式下市場別是已知事實，
  不需要 batch 模式那個「靠當日 tpex_quotes 推市場別」的 workaround
- MoneyDJ 整段取得失敗 → insti_ok 為 False → 該檔每一天都跳過不寫
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


def test_is_tpex_uses_market_type_directly(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.is_tpex("2330", "twse") is False
    assert provider.is_tpex("6488", "tpex") is True


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
