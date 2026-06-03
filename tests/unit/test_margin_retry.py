"""Unit tests: margin fetches retry on transient errors (no network).

需求：來源失敗時要 retry。融資融券四來源原本沒有 retry，補上後：
- fetch_twse_margin / fetch_tpex_margin / fetch_tpex_margin_v2 → 長窗口（每日一次）
- fetch_moneydj_margin → 短窗口（per-symbol，避免單一上游中斷被乘上數百檔）
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import (
    PER_SYMBOL_RETRY_ATTEMPTS,
    RETRY_ATTEMPTS,
    DataUnavailableError,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)


def _count_get_raising(monkeypatch) -> dict:
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("endpoint down")

    monkeypatch.setattr(sources.requests.Session, "get", boom, raising=False)
    return calls


def test_fetch_twse_margin_retries_long_window(monkeypatch):
    calls = _count_get_raising(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        sources.fetch_twse_margin(requests.Session())
    assert calls["n"] == RETRY_ATTEMPTS


def test_fetch_tpex_margin_retries_long_window(monkeypatch):
    calls = _count_get_raising(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        sources.fetch_tpex_margin(requests.Session())
    assert calls["n"] == RETRY_ATTEMPTS


def test_fetch_tpex_margin_v2_retries_long_window(monkeypatch):
    calls = _count_get_raising(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        sources.fetch_tpex_margin_v2(requests.Session(), dt.date(2026, 6, 2))
    assert calls["n"] == RETRY_ATTEMPTS


def test_fetch_moneydj_margin_uses_short_per_symbol_window(monkeypatch):
    """per-symbol，必須用短窗口而非長窗口，否則單一上游中斷會乘上數百檔 stall。"""
    calls = _count_get_raising(monkeypatch)
    with pytest.raises(requests.ConnectionError):
        sources.fetch_moneydj_margin(
            requests.Session(), "2330", dt.date(2026, 5, 23), dt.date(2026, 6, 2)
        )
    assert calls["n"] == PER_SYMBOL_RETRY_ATTEMPTS
    assert PER_SYMBOL_RETRY_ATTEMPTS < RETRY_ATTEMPTS


def test_fetch_moneydj_margin_retries_on_parse_failure(monkeypatch):
    """暫時性壞/不完整 HTML（read_html → ValueError）也要重試，
    用盡後轉成 DataUnavailableError（呼叫端語意不變）。
    回歸鎖：先前 fetch_moneydj_margin 內部把這個 ValueError 直接吞成
    DataUnavailableError，會讓 decorator 第一次就放棄、完全不重試。"""

    class _Resp:
        text = "<html><body>no parseable table here</body></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        sources.requests.Session, "get", lambda *a, **k: _Resp(), raising=False
    )

    calls = {"n": 0}

    def boom_read_html(*_args, **_kwargs):
        calls["n"] += 1
        raise ValueError("No tables found")

    monkeypatch.setattr(sources.pd, "read_html", boom_read_html)

    with pytest.raises(DataUnavailableError):
        sources.fetch_moneydj_margin(
            requests.Session(), "2330", dt.date(2026, 5, 23), dt.date(2026, 6, 2)
        )
    # ValueError 經 retry（短窗口）多次後才放棄，而非第一次就失敗
    assert calls["n"] == PER_SYMBOL_RETRY_ATTEMPTS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
