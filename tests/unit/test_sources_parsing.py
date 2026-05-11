"""Tests for data source parsing utilities (sources.py internal functions)."""

from __future__ import annotations

import datetime as dt

import pytest

from tw_stock_rawdata.sources import (
    DataUnavailableError,
    _clean_int,
    _clean_number,
    _date_to_roc,
    _extract_first_date,
    _extract_twse_table,
    _parse_date_any,
    _parse_market_margin_payload,
    _parse_roc_date,
    _read_tpex_csv,
    _roc_to_date,
)


# ---------------------------------------------------------------------------
# _parse_roc_date
# ---------------------------------------------------------------------------

class TestParseRocDate:
    def test_standard_format(self):
        assert _parse_roc_date("114/03/18") == dt.date(2025, 3, 18)

    def test_two_digit_year(self):
        assert _parse_roc_date("89/01/01") == dt.date(2000, 1, 1)

    def test_dash_separator(self):
        assert _parse_roc_date("114-03-18") == dt.date(2025, 3, 18)

    def test_invalid_format(self):
        assert _parse_roc_date("2025-03-18") is None

    def test_invalid_date(self):
        assert _parse_roc_date("114/13/01") is None  # month 13

    def test_empty_string(self):
        assert _parse_roc_date("") is None

    def test_whitespace(self):
        assert _parse_roc_date("  114/03/18  ") == dt.date(2025, 3, 18)


# ---------------------------------------------------------------------------
# _parse_date_any
# ---------------------------------------------------------------------------

class TestParseDateAny:
    def test_yyyymmdd(self):
        assert _parse_date_any("20250318") == dt.date(2025, 3, 18)

    def test_yyyy_mm_dd_dash(self):
        assert _parse_date_any("2025-03-18") == dt.date(2025, 3, 18)

    def test_yyyy_mm_dd_slash(self):
        assert _parse_date_any("2025/03/18") == dt.date(2025, 3, 18)

    def test_roc_format(self):
        assert _parse_date_any("114/03/18") == dt.date(2025, 3, 18)

    def test_empty(self):
        assert _parse_date_any("") is None

    def test_whitespace_only(self):
        assert _parse_date_any("   ") is None

    def test_invalid_date_yyyymmdd(self):
        assert _parse_date_any("20251301") is None  # month 13

    def test_single_digit_month_day(self):
        assert _parse_date_any("2025/3/8") == dt.date(2025, 3, 8)


# ---------------------------------------------------------------------------
# _extract_first_date
# ---------------------------------------------------------------------------

class TestExtractFirstDate:
    def test_iso_date_in_text(self):
        assert _extract_first_date("資料日期：2025-03-18 台灣") == dt.date(2025, 3, 18)

    def test_roc_date_in_text(self):
        assert _extract_first_date("民國 114/03/18 收盤行情") == dt.date(2025, 3, 18)

    def test_compact_date(self):
        assert _extract_first_date("日期20250318") == dt.date(2025, 3, 18)

    def test_no_date(self):
        assert _extract_first_date("沒有日期資訊") is None

    def test_multiple_dates_returns_first(self):
        result = _extract_first_date("從 2025-01-01 到 2025-03-18")
        assert result == dt.date(2025, 1, 1)


# ---------------------------------------------------------------------------
# _clean_number
# ---------------------------------------------------------------------------

class TestCleanNumber:
    def test_int(self):
        assert _clean_number(42) == 42.0

    def test_float(self):
        assert _clean_number(3.14) == 3.14

    def test_string_with_commas(self):
        assert _clean_number("1,234,567") == 1234567.0

    def test_negative_string(self):
        assert _clean_number("-1,234") == -1234.0

    def test_dash(self):
        assert _clean_number("--") is None

    def test_triple_dash(self):
        assert _clean_number("---") is None

    def test_empty_string(self):
        assert _clean_number("") is None

    def test_none(self):
        assert _clean_number(None) is None

    def test_nan_float(self):
        assert _clean_number(float("nan")) is None

    def test_string_none(self):
        assert _clean_number("None") is None

    def test_invalid_text(self):
        assert _clean_number("abc") is None

    def test_zero(self):
        assert _clean_number(0) == 0.0

    def test_string_zero(self):
        assert _clean_number("0") == 0.0


# ---------------------------------------------------------------------------
# _clean_int
# ---------------------------------------------------------------------------

class TestCleanInt:
    def test_int(self):
        assert _clean_int(42) == 42

    def test_float_rounds(self):
        assert _clean_int(42.6) == 43

    def test_string_with_commas(self):
        assert _clean_int("1,234") == 1234

    def test_none(self):
        assert _clean_int(None) is None

    def test_nan(self):
        assert _clean_int(float("nan")) is None


# ---------------------------------------------------------------------------
# _roc_to_date / _date_to_roc
# ---------------------------------------------------------------------------

class TestRocDateConversion:
    def test_roc_to_date(self):
        assert _roc_to_date("114/03/18") == dt.date(2025, 3, 18)

    def test_roc_to_date_invalid(self):
        assert _roc_to_date("abc") is None

    def test_date_to_roc(self):
        assert _date_to_roc(dt.date(2025, 3, 18)) == "114/03/18"

    def test_roundtrip(self):
        d = dt.date(2025, 6, 1)
        assert _roc_to_date(_date_to_roc(d)) == d


# ---------------------------------------------------------------------------
# _extract_twse_table
# ---------------------------------------------------------------------------

class TestExtractTwseTable:
    def test_tables_format(self):
        """Standard TWSE MI_INDEX payload with 'tables' array."""
        payload = {
            "tables": [
                {
                    "fields": ["證券代號", "證券名稱", "開盤", "最高", "最低", "收盤"],
                    "data": [
                        ["2330", "台積電", "580", "600", "575", "595"],
                    ],
                }
            ]
        }
        df = _extract_twse_table(payload)
        assert len(df) == 1
        assert "證券代號" in df.columns

    def test_fieldsN_dataN_format(self):
        """Fallback format with fields9/data9 keys."""
        payload = {
            "fields9": ["證券代號", "證券名稱", "開盤", "最高", "最低", "收盤"],
            "data9": [
                ["2330", "台積電", "580", "600", "575", "595"],
            ],
        }
        df = _extract_twse_table(payload)
        assert len(df) == 1

    def test_no_matching_table_raises(self):
        """No table with 代號+開盤+收盤 -> raises DataUnavailableError."""
        payload = {"tables": [{"fields": ["foo"], "data": [["bar"]]}]}
        with pytest.raises(DataUnavailableError):
            _extract_twse_table(payload)

    def test_empty_payload_raises(self):
        with pytest.raises(DataUnavailableError):
            _extract_twse_table({})


# ---------------------------------------------------------------------------
# _read_tpex_csv
# ---------------------------------------------------------------------------

class TestReadTpexCsv:
    def test_basic_csv(self):
        csv_text = (
            "日期: 114/03/18\n"
            "代號,名稱,收盤,漲跌,成交量\n"
            "6488,環球晶,450,+5,1000\n"
        )
        df = _read_tpex_csv(csv_text)
        assert len(df) >= 1
        assert "代號" in df.columns

    def test_empty_raises(self):
        with pytest.raises(DataUnavailableError):
            _read_tpex_csv("")

    def test_html_response_raises(self):
        with pytest.raises(DataUnavailableError):
            _read_tpex_csv("<html><body>Error</body></html>")

    def test_no_data_raises(self):
        with pytest.raises(DataUnavailableError):
            _read_tpex_csv("查無資料")


# ---------------------------------------------------------------------------
# _parse_market_margin_payload
# ---------------------------------------------------------------------------

def _make_margin_payload(today_bal: str | None, yesterday_bal: str | None) -> dict:
    return {
        "stat": "OK",
        "tables": [
            {
                "data": [
                    [
                        "融資金額(仟元)",
                        "1,000",
                        "500",
                        "0",
                        yesterday_bal,
                        today_bal,
                    ],
                ],
            },
        ],
    }


class TestParseMarketMarginPayload:
    def test_basic_returns_three_fields(self):
        payload = _make_margin_payload(today_bal="475648471", yesterday_bal="475145292")
        result = _parse_market_margin_payload(payload)
        assert result == {
            "margin_balance": 475_648_471_000,
            "margin_balance_change": 503_179_000,
            "prev_margin_balance": 475_145_292_000,
        }

    def test_yesterday_balance_none_returns_none_change_and_prev(self):
        payload = _make_margin_payload(today_bal="475648471", yesterday_bal=None)
        result = _parse_market_margin_payload(payload)
        assert result == {
            "margin_balance": 475_648_471_000,
            "margin_balance_change": None,
            "prev_margin_balance": None,
        }

    def test_today_balance_none_returns_none(self):
        payload = _make_margin_payload(today_bal=None, yesterday_bal="475145292")
        assert _parse_market_margin_payload(payload) is None

    def test_stat_not_ok_returns_none(self):
        payload = _make_margin_payload(today_bal="100", yesterday_bal="50")
        payload["stat"] = "ERROR"
        assert _parse_market_margin_payload(payload) is None

    def test_no_matching_row_returns_none(self):
        payload = {"stat": "OK", "tables": [{"data": [["其他項目", "1", "2", "3", "4", "5"]]}]}
        assert _parse_market_margin_payload(payload) is None
