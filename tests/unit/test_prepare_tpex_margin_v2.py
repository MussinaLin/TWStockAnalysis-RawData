"""Unit tests: TPEX V2 融資融券 normalize（中文欄名）。

docs/refactor-plan.md T6b。原覆蓋率 3%、CC 16。

最容易壞的是欄位歧義修正：`_find_column` 用「包含」比對，而 API 回應裡
`前資餘額(張)` 排在 `資餘額(張)` 之前，所以 `資餘額` 會先比中前者。
函式內有一段專門重找非「前」的那一欄——本檔用真實欄位順序把它釘住。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tw_stock_rawdata.prepare import prepare_tpex_margin_v2
from tw_stock_rawdata.sources import DataUnavailableError


def _df(**overrides) -> pd.DataFrame:
    """欄位順序刻意模擬 API 回應：前餘額欄排在餘額欄之前。"""
    data = {
        "代號": ["6488"],
        "資買(張)": ["100"],
        "資賣(張)": ["40"],
        "現償(張)": ["10"],
        "前資餘額(張)": ["900"],
        "資餘額(張)": ["950"],
        "券賣(張)": ["30"],
        "券買(張)": ["10"],
        "券償(張)": ["5"],
        "前券餘額(張)": ["180"],
        "券餘額(張)": ["200"],
    }
    for k, v in overrides.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return pd.DataFrame(data)


class TestAmbiguousColumnResolution:
    def test_balance_columns_are_not_confused_with_prev(self) -> None:
        """資餘額 / 券餘額 必須取到非「前」的那一欄。"""
        out = prepare_tpex_margin_v2(_df())
        assert out.iloc[0]["margin_balance"] == 950
        assert out.iloc[0]["short_balance"] == 200

    def test_change_computed_from_balance_diff(self) -> None:
        out = prepare_tpex_margin_v2(_df())
        assert out.iloc[0]["margin_change"] == 950 - 900
        assert out.iloc[0]["short_change"] == 200 - 180

    def test_works_when_prev_columns_come_after(self) -> None:
        """欄位順序反過來也要正確——修正邏輯不能依賴特定排列。"""
        df = _df()
        cols = list(df.columns)
        cols[cols.index("前資餘額(張)")], cols[cols.index("資餘額(張)")] = (
            cols[cols.index("資餘額(張)")], cols[cols.index("前資餘額(張)")]
        )
        out = prepare_tpex_margin_v2(df[cols])
        assert out.iloc[0]["margin_balance"] == 950
        assert out.iloc[0]["margin_change"] == 50


class TestFallbackChangeComputation:
    def test_margin_change_falls_back_to_buy_sell_repay(self) -> None:
        """缺前資餘額時改用 買 − 賣 − 現償。"""
        out = prepare_tpex_margin_v2(_df(**{"前資餘額(張)": None}))
        assert out.iloc[0]["margin_change"] == 100 - 40 - 10

    def test_short_change_falls_back_to_sell_buy_repay(self) -> None:
        out = prepare_tpex_margin_v2(_df(**{"前券餘額(張)": None}))
        assert out.iloc[0]["short_change"] == 30 - 10 - 5

    def test_fallback_without_repay_column(self) -> None:
        """連償還欄都沒有時以 0 計，不可變成 NaN。"""
        out = prepare_tpex_margin_v2(_df(**{"前資餘額(張)": None, "現償(張)": None}))
        assert out.iloc[0]["margin_change"] == 100 - 40


class TestRatioAndRequired:
    def test_short_margin_ratio(self) -> None:
        out = prepare_tpex_margin_v2(_df())
        assert out.iloc[0]["short_margin_ratio"] == pytest.approx(200 / 950)

    def test_zero_margin_balance_yields_null_ratio(self) -> None:
        out = prepare_tpex_margin_v2(_df(**{"資餘額(張)": ["0"]}))
        assert pd.isna(out.iloc[0]["short_margin_ratio"])

    @pytest.mark.parametrize(
        "missing", ["代號", "資買(張)", "資賣(張)", "券賣(張)", "券買(張)"]
    )
    def test_missing_required_column_raises(self, missing: str) -> None:
        with pytest.raises(DataUnavailableError, match="TPEX V2 融資融券欄位解析失敗"):
            prepare_tpex_margin_v2(_df(**{missing: None}))

    def test_symbol_is_stripped(self) -> None:
        out = prepare_tpex_margin_v2(_df(代號=["  6488 "]))
        assert out.iloc[0]["symbol"] == "6488"

    def test_unparseable_values_become_na(self) -> None:
        out = prepare_tpex_margin_v2(_df(**{"資買(張)": ["--"]}))
        assert pd.isna(out.iloc[0]["margin_buy"])
