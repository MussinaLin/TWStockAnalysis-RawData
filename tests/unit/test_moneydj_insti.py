"""Unit tests: MoneyDJ zcl 頁面內的三大法人買賣超。

背景：repo 為了 holding_pct 本來就在打 zcl.djhtm，該頁 col 1-4 就是三大法人
買賣超（外資/投信/自營商/單日合計）。改用它供應三大法人 = 零額外 HTTP 請求。

單位：MoneyDJ 給「張」且四捨五入；prepare 一律 × 1000 還原成「股」以符合
provider 契約。與 T86 的差異僅來自 floor vs round，最多 1 張，已確認接受。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from tw_stock_rawdata.prepare import prepare_moneydj_holding_pct, prepare_moneydj_insti
from tests.conftest import FakeSession
from tw_stock_rawdata.sources import DataUnavailableError, fetch_moneydj_holding_pct

# 真實擷取的 zcl 頁面（2330、2025-07-01 ~ 2025-07-31，cp950/big5 編碼）。
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "moneydj_zcl_2330_202507.html"


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


# ---------------------------------------------------------------------------
# 真實頁面 fixture：位置取欄 + 表頭結構守衛
# ---------------------------------------------------------------------------


def _fixture_html() -> str:
    return _FIXTURE.read_bytes().decode("cp950")


def _fetch(html: str) -> pd.DataFrame:
    return fetch_moneydj_holding_pct(
        FakeSession(text=html), "2330", dt.date(2025, 7, 1), dt.date(2025, 7, 31)
    )


def test_real_page_maps_columns_1_2_3_to_foreign_trust_dealer() -> None:
    """用真實頁面釘住位置對映。

    zcl 全靠位置取欄，而 col 1-3 是純整數：MoneyDJ 若插入/移除一欄，錯位後會把
    投信的數字寫進 foreign_net——數量級合理、不會拋例外、也不會變 NULL，等於永久
    寫錯資料。這個測試是唯一會在改版時吵出來的地方。
    """
    raw = _fetch(_fixture_html())

    first = raw.iloc[0]
    assert first["date"] == "114/07/31"
    assert first["foreign_net_lots"] == "9040"
    assert first["trust_net_lots"] == "-1293"
    assert first["dealer_net_lots"] == "1612"
    assert first["foreign_holding_pct"] == "73.54%"
    assert first["insti_holding_pct"] == "76.79%"

    # 一路接到 prepare：張 × 1000 = 股
    row = prepare_moneydj_insti(raw).set_index("date").loc[dt.date(2025, 7, 31)]
    assert row["foreign_net"] == 9_040_000
    assert row["trust_net"] == -1_293_000
    assert row["dealer_net"] == 1_612_000


def test_real_page_returns_the_whole_range_in_one_request() -> None:
    """整段一發就把整個區間拿回來（2025-07 共 23 個交易日），這是省請求數的前提。"""
    raw = _fetch(_fixture_html())
    assert len(raw) == 23
    assert raw["date"].iloc[0] == "114/07/31"
    assert raw["date"].iloc[-1] == "114/07/01"


def test_swapped_header_groups_raise_even_though_sub_headers_look_right() -> None:
    """分組表頭（row 5）被換掉時要擋下來。

    這是守衛存在的理由：「外資」在子表頭出現兩次（買賣超一次、估計持股一次），
    只看子表頭完全分不出 col 1 是哪一組。這裡把兩組對調、子表頭一字未動 ——
    沒有 row 5 的檢查就會靜靜把「估計持股」的數字寫進 foreign_net。
    """
    html = _fixture_html().replace(
        "colspan=4 nowrap>買賣超</td>\r\n<td class=\"t2\" colspan=4 nowrap>估計持股",
        "colspan=4 nowrap>估計持股</td>\r\n<td class=\"t2\" colspan=4 nowrap>買賣超",
        1,
    )

    with pytest.raises(DataUnavailableError, match="買賣超"):
        _fetch(html)


def test_shifted_sub_header_raises() -> None:
    """買賣超組多插一欄（例如「外資自營商」）→ col 1-3 整組位移，必須擋下來。"""
    html = _fixture_html().replace(
        "<td class=\"t2\" nowrap>外資</td>\r\n<td class=\"t2\" nowrap>投信</td>",
        "<td class=\"t2\" nowrap>外資自營商</td>\r\n<td class=\"t2\" nowrap>外資</td>",
        1,
    )

    with pytest.raises(DataUnavailableError, match="外資／投信／自營商"):
        _fetch(html)


def test_renamed_holding_pct_group_raises() -> None:
    """col 9-10 的分組同樣要驗：持股比重錯位會寫進錯的百分比欄。"""
    html = _fixture_html().replace("colspan=2 nowrap>持股比重", "colspan=2 nowrap>持股比率", 1)

    with pytest.raises(DataUnavailableError, match="持股比重"):
        _fetch(html)


def _replace_nth(text: str, old: str, new: str, n: int) -> str:
    """只換第 n 個（1 起算）：子表頭的「投信」「單日合計」各出現兩次，要能只動估計持股那組。"""
    idx = -1
    for _ in range(n):
        idx = text.index(old, idx + 1)
    return text[:idx] + new + text[idx + len(old):]


def test_real_page_maps_columns_6_8_to_trust_and_total_estimated_holding() -> None:
    """投信持股比例由 col 6（投信估計持股）與 col 8（單日合計估計持股）反推，位置同樣要釘住。"""
    raw = _fetch(_fixture_html())

    first = raw.iloc[0]
    assert first["trust_holding_lots"] == "618021"
    assert first["insti_holding_lots"] == "19916237"

    # 一路接到 prepare：76.79% × 618,021 ÷ 19,916,237 → 0.0238
    row = prepare_moneydj_holding_pct(raw).set_index("date").loc[dt.date(2025, 7, 31)]
    assert row["trust_holding_pct"] == 0.0238


def test_renamed_estimated_holding_group_raises() -> None:
    html = _fixture_html().replace("colspan=4 nowrap>估計持股", "colspan=4 nowrap>庫存估計", 1)

    with pytest.raises(DataUnavailableError, match="估計持股"):
        _fetch(html)


def test_swapped_estimated_holding_sub_headers_raise() -> None:
    """估計持股組內投信／自營商對調 → col 6 變成自營商，會把自營商持股當成投信。"""
    html = _replace_nth(
        _fixture_html(),
        'nowrap>投信</td>\r\n<td class="t2" nowrap>自營商</td>',
        'nowrap>自營商</td>\r\n<td class="t2" nowrap>投信</td>',
        2,
    )

    with pytest.raises(DataUnavailableError, match="外資／投信／自營商／單日合計"):
        _fetch(html)


def test_renamed_estimated_holding_total_raises() -> None:
    """col 8 是反推分母用的合計，換成別的欄位要擋下來。"""
    html = _replace_nth(_fixture_html(), "nowrap>單日合計</td>", "nowrap>三大法人合計</td>", 2)

    with pytest.raises(DataUnavailableError, match="外資／投信／自營商／單日合計"):
        _fetch(html)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
