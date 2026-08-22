"""Unit tests: --backfill-stocks 走 per-stock provider 時不打全市場批次來源。

這是整個改造的驗收點：回補 1 檔 N 天，T86 / MI_INDEX / TPEX quotes / TPEX 3insti
的呼叫次數必須是 0（改造前是每天各 1 次）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run


@pytest.fixture
def batch_spies(monkeypatch):
    """把四個全市場批次 fetch 換成計數器：被呼叫就代表改造沒生效。"""
    calls = {"t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0}

    monkeypatch.setattr(
        run, "_fetch_twse_3insti",
        lambda *a, **k: calls.__setitem__("t86", calls["t86"] + 1)
        or pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"]),
    )
    monkeypatch.setattr(
        run, "fetch_twse_mi_index",
        lambda *a, **k: calls.__setitem__("mi_index", calls["mi_index"] + 1)
        or (pd.DataFrame(), None),
    )
    monkeypatch.setattr(
        run, "_fetch_tpex_sources",
        lambda *a, **k: calls.__setitem__("tpex_quotes", calls["tpex_quotes"] + 1)
        or (None, None, None, None),
    )
    return calls


class _StubProvider:
    """最小 provider：固定回一天有價格、三大法人 OK。"""

    def ohlcv(self, symbol, date, market_type):  # noqa: ANN001 - 測試替身
        return run.OhlcvResult(
            open=100.0, close=105.0, high=106.0, low=99.0,
            volume=1_234_000, change=5.0,
        )

    def insti(self, symbol, date):  # noqa: ANN001 - 測試替身
        return (9_040_000, -1_293_000, 1_612_000)

    def insti_ok(self, symbol, market_type):  # noqa: ANN001 - 測試替身
        return True

    def is_tpex(self, symbol, market_type):  # noqa: ANN001 - 測試替身
        return False


def test_run_for_date_with_provider_skips_batch_fetches(
    batch_spies, monkeypatch
) -> None:
    written: list = []
    monkeypatch.setattr(
        run, "upsert_daily_raw",
        lambda url, date, df: written.append((date, len(df))),
    )

    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is True
    assert written == [(dt.date(2025, 7, 31), 1)]
    assert batch_spies == {
        "t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0
    }


def test_run_for_date_without_provider_still_fetches_batches(
    batch_spies, monkeypatch
) -> None:
    """不傳 provider 時維持現行行為（daily / 全市場 backfill 不受影響）。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
    )

    assert batch_spies["mi_index"] >= 1


def test_run_for_date_with_provider_skips_weekend(batch_spies) -> None:
    """週末照舊直接跳過，provider 不改變這個判斷。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 8, 2),  # 週六
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is False


def test_per_symbol_and_batch_agree_on_same_row(monkeypatch) -> None:
    """同一天同一檔，兩種 provider 組出來的列必須一致（三大法人容許 ±1 張）。

    這是「單一組列路徑、不產生雙軌」的驗收點：差異只該來自來源精度，
    不該來自組列邏輯。
    """
    date = dt.date(2025, 7, 31)
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    # batch 來源：交易所精確股數
    mi_index = pd.DataFrame([{
        "symbol": "2330", "name": "台積電", "open": 1250.0, "close": 1255.0,
        "high": 1260.0, "low": 1245.0, "volume": 24_000_000, "change": -5.0,
    }])
    twse_3insti = pd.DataFrame([{
        "symbol": "2330", "foreign_net": 9_039_647,
        "trust_net": -1_292_952, "dealer_net": 1_611_836,
    }])
    batch_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.BatchSourceProvider(
            session=None,
            twse_3insti=twse_3insti,
            twse_day_all=None,
            twse_mi_index=mi_index,
            tpex_quotes=pd.DataFrame(
                columns=["symbol", "name", "open", "close", "high", "low",
                         "volume", "change"]
            ),
            tpex_3insti=pd.DataFrame(columns=["symbol"]),
            twse_month_cache={},
        ),
    ).iloc[0]

    # per-symbol 來源：月表 + MoneyDJ（張級）
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    ))
    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", lambda *a, **k: pd.DataFrame({
        "date": ["114/07/31"],
        "foreign_net_lots": ["9040"], "trust_net_lots": ["-1293"],
        "dealer_net_lots": ["1612"],
        "foreign_holding_pct": ["73.54%"], "insti_holding_pct": ["76.79%"],
    }))
    per_symbol_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.PerSymbolRangeProvider.build(
            session=None, symbols=["2330"], market_types={"2330": "twse"},
            start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        ),
    ).iloc[0]

    # 價格類完全相同
    for col in ("open", "high", "low", "close", "volume", "limit_up", "limit_down"):
        assert batch_row[col] == per_symbol_row[col], col

    # 三大法人容許 ±1 張（floor vs round）
    for col in ("foreign_net", "trust_net", "dealer_net"):
        assert abs(batch_row[col] - per_symbol_row[col]) <= 1, col


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
