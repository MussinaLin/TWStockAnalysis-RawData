"""Unit tests: 期交所台指期（大台）三大法人未平倉的抓取與解析。

端點 futContractsDateDown 吃日期區間、回 cp950 CSV；錯誤（休市、超出近三年）
一律是 HTTP 200 + HTML alert，必須從內容分辨（見 memory/taifex-futures-oi-range-download）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import sources
from tw_stock_rawdata.prepare import prepare_taifex_txf_oi
from tw_stock_rawdata.sources import DataUnavailableError, fetch_taifex_txf_institutional

HEADER = (
    "日期,商品名稱,身份別,多方交易口數,多方交易契約金額(千元),空方交易口數,"
    "空方交易契約金額(千元),多空交易口數淨額,多空交易契約金額淨額(千元),"
    "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,空方未平倉契約金額(千元),"
    "多空未平倉口數淨額,多空未平倉契約金額淨額(千元)"
)
# 2026-11-16 實際回應
ROWS = [
    "2026/11/16,臺股期貨,自營商,15054,127197021,11048,92812667,4006,34384354,"
    "5573,45527463,4414,36072336,1159,9455127",
    "2026/11/16,臺股期貨,投信,51979,436552715,59496,497201802,-7517,-60649087,"
    "95851,782883182,3445,28131743,92406,754751439",
    "2026/11/16,臺股期貨,外資及陸資,162613,1374924922,157435,1335157765,5178,39767157,"
    "12030,98165273,108817,888770550,-96787,-790605277",
]
DATE = dt.date(2026, 11, 16)


def _csv_bytes(rows=ROWS, header=HEADER) -> bytes:
    return ("\r\n".join([header, *rows]) + "\r\n").encode("cp950")


def _alert_page(message: str) -> bytes:
    return (
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html><head>'
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"></head>'
        f'<body><script>\n\talert("{message}");\n'
        '\twindow.location.replace("futContractsDateView");\n</script></body></html>\n'
    ).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _PostSession:
    """只記錄 POST 的假 session（conftest 的 FakeSession 只有 GET）。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.calls.append((url, dict(data or {})))
        return _Resp(self._content)


def _raw_df(rows=ROWS, header=HEADER) -> pd.DataFrame:
    return fetch_taifex_txf_institutional(
        _PostSession(_csv_bytes(rows, header)), DATE, DATE
    )


# ---------------------------------------------------------------------------
# fetch_taifex_txf_institutional
# ---------------------------------------------------------------------------


class TestFetch:
    def test_posts_range_and_txf_commodity(self) -> None:
        session = _PostSession(_csv_bytes())
        fetch_taifex_txf_institutional(session, dt.date(2025, 1, 2), dt.date(2026, 11, 16))
        url, data = session.calls[0]
        assert url == sources.TAIFEX_FUT_CONTRACTS_DOWN_URL
        assert data == {
            "queryStartDate": "2025/01/02",
            "queryEndDate": "2026/11/16",
            "commodityId": "TXF",
        }

    def test_decodes_cp950_csv_as_strings(self) -> None:
        df = _raw_df()
        assert len(df) == 3
        assert df.loc[2, "身份別"] == "外資及陸資"
        assert df.loc[2, "多空未平倉口數淨額"] == "-96787"

    def test_no_data_alert_raises(self) -> None:
        session = _PostSession(_alert_page("查無資料"))
        with pytest.raises(DataUnavailableError, match="查無資料"):
            fetch_taifex_txf_institutional(session, DATE, DATE)

    def test_out_of_history_alert_explains_three_year_limit(self) -> None:
        session = _PostSession(_alert_page("日期時間錯誤 DateTime error"))
        with pytest.raises(DataUnavailableError, match="近三年"):
            fetch_taifex_txf_institutional(session, dt.date(2023, 1, 2), DATE)

    def test_html_without_alert_raises(self) -> None:
        session = _PostSession(b"<html><body>maintenance</body></html>")
        with pytest.raises(DataUnavailableError, match="非 CSV"):
            fetch_taifex_txf_institutional(session, DATE, DATE)


# ---------------------------------------------------------------------------
# prepare_taifex_txf_oi
# ---------------------------------------------------------------------------


class TestPrepare:
    def test_maps_three_identities_lots(self) -> None:
        day = prepare_taifex_txf_oi(_raw_df())[DATE]
        assert day["txf_foreign_oi_net"] == -96787
        assert day["txf_trust_oi_net"] == 92406
        assert day["txf_dealer_oi_net"] == 1159

    def test_amounts_are_converted_from_thousands_to_yuan(self) -> None:
        day = prepare_taifex_txf_oi(_raw_df())[DATE]
        assert day["txf_foreign_oi_net_amount"] == -790_605_277_000
        assert day["txf_trust_oi_net_amount"] == 754_751_439_000
        assert day["txf_dealer_oi_net_amount"] == 9_455_127_000

    def test_total_is_sum_of_three_identities(self) -> None:
        day = prepare_taifex_txf_oi(_raw_df())[DATE]
        assert day["txf_total_oi_net"] == 1159 + 92406 - 96787
        assert day["txf_total_oi_net_amount"] == (9455127 + 754751439 - 790605277) * 1000

    def test_multiple_dates_are_keyed_separately(self) -> None:
        nxt = [r.replace("2026/11/16", "2026/11/17") for r in ROWS]
        out = prepare_taifex_txf_oi(_raw_df(ROWS + nxt))
        assert set(out) == {DATE, dt.date(2026, 11, 17)}

    def test_other_products_are_ignored(self) -> None:
        mini = ROWS[2].replace("臺股期貨", "小型臺指").replace("-96787", "-1")
        day = prepare_taifex_txf_oi(_raw_df(ROWS + [mini]))[DATE]
        assert day["txf_foreign_oi_net"] == -96787

    def test_missing_identity_leaves_total_null(self) -> None:
        """少一個身份別時不把缺的當 0 加進合計：寫 NULL，COALESCE 保留舊值。"""
        day = prepare_taifex_txf_oi(_raw_df(ROWS[:2]))[DATE]
        assert day["txf_dealer_oi_net"] == 1159
        assert day["txf_foreign_oi_net"] is None
        assert day["txf_total_oi_net"] is None
        assert day["txf_total_oi_net_amount"] is None

    def test_unknown_identity_raises(self) -> None:
        """身份別改名時直接報錯，不默默少寫一欄。"""
        renamed = ROWS[2].replace("外資及陸資", "外資")
        with pytest.raises(DataUnavailableError, match="身份別"):
            prepare_taifex_txf_oi(_raw_df(ROWS[:2] + [renamed]))

    def test_missing_header_column_raises(self) -> None:
        header = HEADER.replace("多空未平倉口數淨額", "未平倉淨口數")
        with pytest.raises(DataUnavailableError, match="多空未平倉口數淨額"):
            prepare_taifex_txf_oi(_raw_df(header=header))

    def test_no_txf_rows_raises(self) -> None:
        mini = [r.replace("臺股期貨", "小型臺指") for r in ROWS]
        with pytest.raises(DataUnavailableError, match="臺股期貨"):
            prepare_taifex_txf_oi(_raw_df(mini))

    def test_unparseable_date_raises(self) -> None:
        bad = [r.replace("2026/11/16", "合計") for r in ROWS]
        with pytest.raises(DataUnavailableError, match="日期"):
            prepare_taifex_txf_oi(_raw_df(bad))
