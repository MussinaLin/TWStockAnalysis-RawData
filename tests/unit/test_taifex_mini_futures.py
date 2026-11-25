"""Unit tests: 期交所小台（MTX）／微台（TMF）未平倉的抓取與解析。

兩個來源：
- 三大法人多／空單未平倉：futContractsDateDown（與大台同一支），小台 commodityId=MXF、微台 TMF。
- 全市場未平倉：futDataDown（期貨每日交易行情），commodity_id=MTX / TMF；區間上限一個月，
  休市回只有表頭的 CSV（不是 alert）；每列比表頭多一個尾逗號。

fixture 是 2026-11-25 實抓的 2026-11-24 原檔（cp950）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import requests

import pandas as pd

from tw_stock_rawdata import sources
from tw_stock_rawdata.prepare import prepare_taifex_inst_long_short, prepare_taifex_market_oi
from tw_stock_rawdata.sources import (
    MINI_FUTURES,
    DataUnavailableError,
    fetch_taifex_fut_daily_report,
    fetch_taifex_fut_institutional,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MTX_REPORT = (FIXTURES / "taifex_futdatadown_MTX_20261124.csv").read_bytes()
TMF_REPORT = (FIXTURES / "taifex_futdatadown_TMF_20261124.csv").read_bytes()
REPORT_HEADER_ONLY = MTX_REPORT.split(b"\r\n", 1)[0] + b"\r\n"
D = dt.date(2026, 11, 24)

INST_HEADER = (
    "日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,"
    "空方交易契約金額(千元),多空交易口數淨額,多空交易契約金額淨額(千元),"
    "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),"
    "多空未平倉口數淨額,多空未平倉契約金額淨額(千元)"
)
# 2026-11-24 實際回應
MXF_ROWS = [
    "2026/11/24,小型臺指期貨,自營商,15461,30588069,12492,24530635,2969,6057433,"
    "7578,15283148,5227,10561526,2351,4721622",
    "2026/11/24,小型臺指期貨,投信,0,0,0,0,0,0,90,181458,168,338722,-78,-157264",
    "2026/11/24,小型臺指期貨,外資及陸資,151733,294218326,151862,294370634,-129,-152308,"
    "5010,10112055,3457,6973676,1553,3138379",
]
TMF_ROWS = [
    "2026/11/24,微型臺指期貨,自營商,35782,14191431,32065,12637192,3717,1554239,"
    "2099,847762,34153,13792611,-32054,-12944849",
    "2026/11/24,微型臺指期貨,投信,0,0,0,0,0,0,0,0,0,0,0,0",
    "2026/11/24,微型臺指期貨,外資及陸資,270640,104863442,269446,104393710,1194,469732,"
    "12669,5109097,7476,3014642,5193,2094455",
]


def _inst_csv(rows=MXF_ROWS, header=INST_HEADER) -> bytes:
    return ("\r\n".join([header, *rows]) + "\r\n").encode("cp950")


def _alert_page(message: str) -> bytes:
    return (
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html><head></head>'
        f'<body><script>\n\talert("{message}");\n</script></body></html>\n'
    ).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _PostSession:
    """依序回傳 contents（用完重複最後一個）；元素是例外就丟出。"""

    def __init__(self, *contents) -> None:
        self._contents = list(contents)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.calls.append((url, dict(data or {})))
        item = self._contents[min(len(self.calls), len(self._contents)) - 1]
        if isinstance(item, Exception):
            raise item
        return _Resp(item)


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def test_mini_futures_table() -> None:
    assert [(f.prefix, f.inst_id, f.report_id) for f in MINI_FUTURES] == [
        ("mtx", "MXF", "MTX"), ("tmf", "TMF", "TMF"),
    ]
    assert [(f.label, f.product) for f in MINI_FUTURES] == [
        ("小台", "小型臺指期貨"), ("微台", "微型臺指期貨"),
    ]


def test_institutional_posts_given_commodity() -> None:
    session = _PostSession(_inst_csv())
    df = fetch_taifex_fut_institutional(session, D, D, "MXF")
    assert session.calls == [(
        sources.TAIFEX_FUT_CONTRACTS_DOWN_URL,
        {"queryStartDate": "2026/11/24", "queryEndDate": "2026/11/24", "commodityId": "MXF"},
    )]
    assert df.loc[0, "多方未平倉口數"] == "7578"


def test_institutional_out_of_history_alert_explains_three_year_limit() -> None:
    session = _PostSession(_alert_page("日期時間錯誤 DateTime error"))
    with pytest.raises(DataUnavailableError, match="近三年"):
        fetch_taifex_fut_institutional(session, dt.date(2023, 1, 2), D, "TMF")


def test_report_posts_form() -> None:
    session = _PostSession(MTX_REPORT)
    fetch_taifex_fut_daily_report(session, "MTX", D, D)
    assert session.calls == [(
        sources.TAIFEX_FUT_DAILY_REPORT_URL,
        {"down_type": "1", "commodity_id": "MTX", "commodity_id2": "",
         "queryStartDate": "2026/11/24", "queryEndDate": "2026/11/24"},
    )]


def test_report_columns_not_shifted_by_trailing_comma() -> None:
    """每列比表頭多一個尾逗號：沒設 index_col=False 時 pandas 把第一欄當 index、整列錯位。"""
    df = fetch_taifex_fut_daily_report(_PostSession(MTX_REPORT), "MTX", D, D)
    assert len(df) == 35
    assert df["交易日期"].iloc[0] == "2026/11/24"
    assert df["到期月份(週別)"].iloc[0] == "202611W4"
    assert df["未沖銷契約數"].iloc[0] == "865"
    assert df["交易時段"].iloc[1] == "盤後"


def test_report_header_only_returns_empty_frame() -> None:
    """休市日 futDataDown 不回 alert，回只有表頭的 CSV。"""
    df = fetch_taifex_fut_daily_report(_PostSession(REPORT_HEADER_ONLY), "MTX", D, D)
    assert df.empty
    assert "未沖銷契約數" in df.columns


def test_report_range_alert_explains_one_month_limit() -> None:
    session = _PostSession(_alert_page("日期時間錯誤"))
    with pytest.raises(DataUnavailableError, match="一個月"):
        fetch_taifex_fut_daily_report(session, "MTX", dt.date(2026, 10, 24), dt.date(2026, 11, 25))


def test_report_no_data_alert_raises() -> None:
    with pytest.raises(DataUnavailableError, match="查無資料"):
        fetch_taifex_fut_daily_report(_PostSession(_alert_page("查無資料")), "TMF", D, D)


def test_report_retries_transient_connection_error(monkeypatch) -> None:
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)
    session = _PostSession(requests.ConnectionError("reset"), MTX_REPORT)
    df = fetch_taifex_fut_daily_report(session, "MTX", D, D)
    assert len(session.calls) == 2
    assert len(df) == 35


# ---------------------------------------------------------------------------
# 解析：三大法人多／空單未平倉合計
# ---------------------------------------------------------------------------

TXF_ROWS = [
    "2026/11/24,臺股期貨,自營商,1,1,1,1,0,0,5573,1,4414,1,1159,1",
    "2026/11/24,臺股期貨,投信,1,1,1,1,0,0,95851,1,3445,1,92406,1",
    "2026/11/24,臺股期貨,外資及陸資,1,1,1,1,0,0,12030,1,108817,1,-96787,1",
]


def _inst_df(rows=MXF_ROWS, header=INST_HEADER) -> pd.DataFrame:
    return fetch_taifex_fut_institutional(_PostSession(_inst_csv(rows, header)), D, D, "MXF")


def test_inst_sums_three_identities() -> None:
    out = prepare_taifex_inst_long_short(_inst_df(), "小型臺指期貨", "mtx")
    assert out == {D: {"mtx_inst_long_oi": 12678, "mtx_inst_short_oi": 8852}}


def test_inst_tmf() -> None:
    out = prepare_taifex_inst_long_short(_inst_df(TMF_ROWS), "微型臺指期貨", "tmf")
    assert out == {D: {"tmf_inst_long_oi": 14768, "tmf_inst_short_oi": 41629}}


def test_inst_missing_identity_gives_none() -> None:
    rows = [r for r in MXF_ROWS if ",投信," not in r]
    out = prepare_taifex_inst_long_short(_inst_df(rows), "小型臺指期貨", "mtx")
    assert out == {D: {"mtx_inst_long_oi": None, "mtx_inst_short_oi": None}}


def test_inst_blank_value_gives_none_for_that_column_only() -> None:
    rows = [MXF_ROWS[0], "2026/11/24,小型臺指期貨,投信,0,0,0,0,0,0,,181458,168,338722,-78,-157264", MXF_ROWS[2]]
    out = prepare_taifex_inst_long_short(_inst_df(rows), "小型臺指期貨", "mtx")
    assert out == {D: {"mtx_inst_long_oi": None, "mtx_inst_short_oi": 8852}}


def test_inst_other_products_ignored() -> None:
    out = prepare_taifex_inst_long_short(_inst_df([*TXF_ROWS, *MXF_ROWS]), "小型臺指期貨", "mtx")
    assert out == {D: {"mtx_inst_long_oi": 12678, "mtx_inst_short_oi": 8852}}


def test_inst_no_product_rows_raises() -> None:
    with pytest.raises(DataUnavailableError, match="小型臺指期貨"):
        prepare_taifex_inst_long_short(_inst_df(TXF_ROWS), "小型臺指期貨", "mtx")


def test_inst_unknown_identity_raises() -> None:
    rows = [MXF_ROWS[0].replace("自營商", "自營商(避險)"), *MXF_ROWS[1:]]
    with pytest.raises(DataUnavailableError, match="身份別"):
        prepare_taifex_inst_long_short(_inst_df(rows), "小型臺指期貨", "mtx")


def test_inst_missing_header_raises() -> None:
    df = _inst_df().drop(columns=["空方未平倉口數"])
    with pytest.raises(DataUnavailableError, match="空方未平倉口數"):
        prepare_taifex_inst_long_short(df, "小型臺指期貨", "mtx")


def test_inst_unparseable_date_raises() -> None:
    rows = [MXF_ROWS[0].replace("2026/11/24", "bad"), *MXF_ROWS[1:]]
    with pytest.raises(DataUnavailableError, match="日期"):
        prepare_taifex_inst_long_short(_inst_df(rows), "小型臺指期貨", "mtx")


# ---------------------------------------------------------------------------
# 解析：全市場未平倉
# ---------------------------------------------------------------------------


def _report_df(content: bytes = MTX_REPORT) -> pd.DataFrame:
    return fetch_taifex_fut_daily_report(_PostSession(content), "MTX", D, D)


def test_market_oi_mtx_fixture() -> None:
    assert prepare_taifex_market_oi(_report_df(), "MTX", "mtx") == {D: {"mtx_market_oi": 33055}}


def test_market_oi_tmf_fixture() -> None:
    assert prepare_taifex_market_oi(_report_df(TMF_REPORT), "TMF", "tmf") == {D: {"tmf_market_oi": 75681}}


def test_market_oi_includes_weekly_contracts() -> None:
    df = _report_df()
    weekly = df[(df["到期月份(週別)"].str.strip() == "202611W4") & (df["交易時段"] == "一般")]
    assert prepare_taifex_market_oi(weekly, "MTX", "mtx") == {D: {"mtx_market_oi": 865}}


def test_market_oi_excludes_after_hours_and_spreads() -> None:
    """排除依據是時段與到期月份含「/」，不是靠未沖銷契約數剛好是「-」。"""
    df = _report_df()
    rest = df[(df["交易時段"] == "盤後") | df["到期月份(週別)"].str.contains("/")].copy()
    rest["未沖銷契約數"] = "100"
    assert prepare_taifex_market_oi(rest, "MTX", "mtx") == {}


def test_market_oi_other_commodity_ignored() -> None:
    assert prepare_taifex_market_oi(_report_df(), "TMF", "tmf") == {}


def test_market_oi_header_only_is_empty() -> None:
    assert prepare_taifex_market_oi(_report_df(REPORT_HEADER_ONLY), "MTX", "mtx") == {}


def test_market_oi_multiple_dates_keyed_separately() -> None:
    df = _report_df()
    nxt = df.copy()
    nxt["交易日期"] = "2026/11/25"
    out = prepare_taifex_market_oi(pd.concat([df, nxt]), "MTX", "mtx")
    assert out == {D: {"mtx_market_oi": 33055}, dt.date(2026, 11, 25): {"mtx_market_oi": 33055}}


def test_market_oi_missing_header_raises() -> None:
    with pytest.raises(DataUnavailableError, match="交易時段"):
        prepare_taifex_market_oi(_report_df().drop(columns=["交易時段"]), "MTX", "mtx")


def test_market_oi_unparseable_date_raises() -> None:
    df = _report_df()
    df.loc[0, "交易日期"] = "bad"
    with pytest.raises(DataUnavailableError, match="日期"):
        prepare_taifex_market_oi(df, "MTX", "mtx")
