"""Unit tests: MoneyDJ zcl 頁面內的三大法人買賣超。

背景：repo 為了 holding_pct 本來就在打 zcl.djhtm，該頁 col 1-4 就是三大法人
買賣超（外資/投信/自營商/單日合計）。改用它供應三大法人 = 零額外 HTTP 請求。

單位：MoneyDJ 給「張」且四捨五入；prepare 一律 × 1000 還原成「股」以符合
provider 契約。與 T86 的差異僅來自 floor vs round，最多 1 張，已確認接受。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata.prepare import prepare_moneydj_insti


def _raw() -> pd.DataFrame:
    """模擬 fetch_moneydj_holding_pct 的輸出（含新增的三欄）。"""
    return pd.DataFrame({
        "date": ["114/07/31", "114/07/30"],
        "foreign_net_lots": ["9040", "12832"],
        "trust_net_lots": ["-1293", "-352"],
        "dealer_net_lots": ["1612", "592"],
        "foreign_holding_pct": ["73.54%", "73.51%"],
        "insti_holding_pct": ["76.79%", "76.76%"],
    })


def test_prepare_insti_parses_dates_and_converts_lots_to_shares() -> None:
    out = prepare_moneydj_insti(_raw())

    by_date = out.set_index("date")
    row = by_date.loc[dt.date(2025, 7, 31)]
    assert row["foreign_net"] == 9_040_000
    assert row["trust_net"] == -1_293_000
    assert row["dealer_net"] == 1_612_000


def test_prepare_insti_matches_t86_within_one_lot() -> None:
    """對照 T86 2330 / 2025-07-31 的實際股數，差異必須 <= 1 張。"""
    out = prepare_moneydj_insti(_raw())
    row = out.set_index("date").loc[dt.date(2025, 7, 31)]

    t86 = {"foreign_net": 9_039_647, "trust_net": -1_292_952, "dealer_net": 1_611_836}
    for col, exact in t86.items():
        assert abs(row[col] - exact) <= 1000, col


def test_prepare_insti_drops_unparsable_dates() -> None:
    df = _raw()
    df.loc[len(df)] = ["合計", "1", "1", "1", "0%", "0%"]

    out = prepare_moneydj_insti(df)
    assert len(out) == 2


def test_prepare_insti_missing_columns_yields_none() -> None:
    df = pd.DataFrame({"date": ["114/07/31"]})

    out = prepare_moneydj_insti(df)
    assert out.iloc[0]["foreign_net"] is None
    assert out.iloc[0]["trust_net"] is None
    assert out.iloc[0]["dealer_net"] is None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
