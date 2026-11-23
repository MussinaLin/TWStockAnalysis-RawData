"""Unit tests: 期交所台指 VIX 當日檔（daily）與月檔（--backfill-vix）。

兩者都是 cp950、tab 分隔、前兩行是表頭與 `----` 分隔線（2026-11-23 實測）。
當日檔是逐 15 秒 tick，收盤取 13:45:00（`13450000`）那一列；月檔每天一列。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tw_stock_rawdata.sources import (
    TAIFEX_VIX_DAILY_URL,
    DataUnavailableError,
    fetch_taifex_vix_close,
    fetch_taifex_vix_month,
)

DATE = dt.date(2026, 11, 20)

DAILY_HEADER = "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數"
DAILY_SEP = "--------\t-------------------\t--------------------"
MONTH_HEADER = DAILY_HEADER + "\t收盤前1分鐘平均指數"
MONTH_SEP = DAILY_SEP + "\t-------------------"

DAILY_ROWS = [
    "20261120\t13443000\t\t\t52.24",
    "20261120\t13450000\t\t\t52.13",
    "20261120\tLast 1 min AVG\t\t\t52.20",
]
MONTH_ROWS = [
    "20261119\t13450000\t\t\t49.29\t\t49.27",
    "20261120\t13450000\t\t\t52.13\t\t52.20",
]
HTML = b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html><body>error</body></html>'


def _file(header: str, sep: str, rows: list[str], eol: str = "\r\n") -> bytes:
    return eol.join([header, sep, *rows]).encode("cp950") + eol.encode()


class _Resp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _BytesSession:
    """記錄 GET 的 (url, params)，回傳固定 bytes（conftest 的 FakeResponse 沒有 .content）。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        return _Resp(self._content)


class TestFetchTaifexVixClose:
    def test_close_is_the_1345_row(self) -> None:
        session = _BytesSession(_file(DAILY_HEADER, DAILY_SEP, DAILY_ROWS))
        assert fetch_taifex_vix_close(session, DATE) == 52.13
        assert session.calls == [(TAIFEX_VIX_DAILY_URL, {"filesname": "20261120"})]

    def test_missing_1345_row_raises(self) -> None:
        session = _BytesSession(_file(DAILY_HEADER, DAILY_SEP, DAILY_ROWS[:1]))
        with pytest.raises(DataUnavailableError, match="13:45"):
            fetch_taifex_vix_close(session, DATE)

    def test_other_date_rows_are_ignored(self) -> None:
        rows = ["20261119\t13450000\t\t\t49.29"]
        session = _BytesSession(_file(DAILY_HEADER, DAILY_SEP, rows))
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_close(session, DATE)

    def test_html_response_raises(self) -> None:
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_close(_BytesSession(HTML), DATE)

    def test_header_mismatch_raises(self) -> None:
        header = "交易日期\t時間(時/分/秒/毫秒)\t波動率"
        session = _BytesSession(_file(header, DAILY_SEP, DAILY_ROWS))
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_close(session, DATE)

    def test_unparseable_close_raises(self) -> None:
        rows = ["20261120\t13450000\t\t\t--"]
        session = _BytesSession(_file(DAILY_HEADER, DAILY_SEP, rows))
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_close(session, DATE)


class TestFetchTaifexVixMonth:
    def test_month_returns_close_by_date(self) -> None:
        session = _BytesSession(_file(MONTH_HEADER, MONTH_SEP, MONTH_ROWS))
        out = fetch_taifex_vix_month(session, dt.date(2026, 11, 1))
        assert out == {dt.date(2026, 11, 19): 49.29, dt.date(2026, 11, 20): 52.13}
        url, _ = session.calls[0]
        assert url.endswith("/202611new.txt")

    def test_month_uses_year_month_of_any_day(self) -> None:
        session = _BytesSession(_file(MONTH_HEADER, MONTH_SEP, MONTH_ROWS))
        fetch_taifex_vix_month(session, dt.date(2026, 11, 20))
        assert session.calls[0][0].endswith("/202611new.txt")

    def test_month_html_raises_with_retention_hint(self) -> None:
        with pytest.raises(DataUnavailableError, match="近 4 個月"):
            fetch_taifex_vix_month(_BytesSession(HTML), dt.date(2026, 7, 1))

    def test_month_header_mismatch_raises(self) -> None:
        session = _BytesSession(_file(DAILY_HEADER, DAILY_SEP, DAILY_ROWS))
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_month(session, dt.date(2026, 11, 1))


class TestReviewFocus:
    def test_mixed_line_endings_and_trailing_blank_lines(self) -> None:
        """實測當日檔就是 CRLF 與 LF 混用（最後一行只有 LF）。"""
        daily = (
            "\r\n".join([DAILY_HEADER, DAILY_SEP, *DAILY_ROWS[:2]])
            + "\n" + DAILY_ROWS[2] + "\n\n"
        ).encode("cp950")
        assert fetch_taifex_vix_close(_BytesSession(daily), DATE) == 52.13

        month = _file(MONTH_HEADER, MONTH_SEP, MONTH_ROWS, eol="\n") + b"\r\n\n"
        out = fetch_taifex_vix_month(_BytesSession(month), dt.date(2026, 11, 1))
        assert out[dt.date(2026, 11, 20)] == 52.13

    def test_undecodable_bytes_raise_data_unavailable(self) -> None:
        garbage = b"\xff\xfe\x00garbage\x81"
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_close(_BytesSession(garbage), DATE)
        with pytest.raises(DataUnavailableError):
            fetch_taifex_vix_month(_BytesSession(garbage), dt.date(2026, 11, 1))
