"""Unit tests: MoneyDJ zcl 持股佔比 → 逐檔 cache 的轉換。

三條抓取路徑（daily 逐檔、區間回補預取、--backfill-stocks provider）都把
`prepare_moneydj_holding_pct` 的結果轉成 {date: {...}}，由 `_holding_pct_by_date`
統一處理；三者的差別只在「抓取失敗時該檔要不要留空 dict」。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

JUL31 = dt.date(2025, 7, 31)


def test_holding_pct_by_date_keys_on_date_and_skips_non_date_rows() -> None:
    """合計列之類的非日期列混進來，cache 的鍵就不再全是日期。"""
    df = pd.DataFrame([
        {"date": JUL31, "foreign_holding_pct": 0.7354, "insti_holding_pct": 0.7679,
         "trust_holding_pct": 0.0238},
        {"date": "合計", "foreign_holding_pct": 0.1, "insti_holding_pct": 0.2,
         "trust_holding_pct": 0.05},
    ])

    assert run._holding_pct_by_date(df) == {
        JUL31: {"foreign_holding_pct": 0.7354, "insti_holding_pct": 0.7679,
                "trust_holding_pct": 0.0238},
    }


def test_prefetch_holding_pct_cache_keeps_failed_symbol_as_empty(monkeypatch) -> None:
    """區間回補預取：抓取失敗的個股仍留一個空 dict（與 daily 逐檔路徑不同）。"""

    def fake_fetch(session, symbol, start, end):  # noqa: ANN001 - 測試替身
        if symbol == "9999":
            raise DataUnavailableError("MoneyDJ 找不到法人持股表格")
        return pd.DataFrame({
            "date": ["114/07/31"],
            "foreign_holding_pct": ["73.54%"],
            "insti_holding_pct": ["76.79%"],
            "trust_holding_lots": ["618021"],
            "insti_holding_lots": ["19916237"],
        })

    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", fake_fetch)
    holdings = pd.DataFrame([{"symbol": "2330"}, {"symbol": "9999"}])

    cache = run._prefetch_holding_pct_cache(
        None, holdings, dt.date(2025, 7, 1), JUL31
    )

    assert cache == {
        "2330": {JUL31: {"foreign_holding_pct": 0.7354, "insti_holding_pct": 0.7679,
                         "trust_holding_pct": 0.0238}},
        "9999": {},
    }
