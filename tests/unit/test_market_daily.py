"""Unit tests: 大盤行情寫入 market_daily。

docs/refactor-plan.md T2。_fetch_and_upsert_market_daily 原本覆蓋率 2%
（58 行只有 1 行被執行），CC 16，且是**唯一**寫共用表 market_daily 的路徑
（CLAUDE.md 列為 --backfill-stocks 刻意不碰的表），完全沒有回歸保護。

核心不變量：四個子來源各自獨立失敗，任一失敗不影響其他三個，也不阻止寫入
已取得的部分——搭配 upsert_market_daily 的 COALESCE，缺的欄位不會覆寫舊值。
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.config import AppConfig
from tw_stock_rawdata.sources import DataUnavailableError

CONFIG = AppConfig(database_url="postgres://x", use_db=True)
DATE = dt.date(2026, 8, 21)

OHLC = {"taiex_open": 24000.0, "taiex_high": 24200.0,
        "taiex_low": 23900.0, "taiex_close": 24100.0}


@pytest.fixture
def wiring(monkeypatch):
    calls = {"upserts": [], "corrections": []}
    monkeypatch.setattr(run, "fetch_twse_taiex_ohlc", lambda s, d: {DATE: dict(OHLC)})
    monkeypatch.setattr(run, "fetch_twse_market_volume", lambda s, d: {DATE: 123456})
    monkeypatch.setattr(run, "fetch_twse_foreign_net", lambda s, d: -5000)
    monkeypatch.setattr(
        run, "fetch_twse_market_margin",
        lambda s, d: {"margin_balance": 300, "prev_margin_balance": 280},
    )

    def fake_correct(url, date, prev_balance):
        calls["corrections"].append((date, prev_balance))
        return None

    monkeypatch.setattr(run, "correct_prev_margin_balance", fake_correct)

    def fake_upsert(url, date, data):
        calls["upserts"].append((date, dict(data)))

    monkeypatch.setattr(run, "upsert_market_daily", fake_upsert)
    return calls


def _written(calls) -> dict:
    assert len(calls["upserts"]) == 1
    return calls["upserts"][0][1]


class TestHappyPath:
    def test_all_sources_merged_into_one_upsert(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert data["taiex_close"] == 24100.0
        assert data["total_volume"] == 123456
        assert data["foreign_net"] == -5000
        assert data["margin_balance"] == 300

    def test_prev_margin_balance_is_not_written_to_market_daily(self, wiring) -> None:
        """prev_margin_balance 只用來校正 D-1，不是 market_daily 的欄位。"""
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "prev_margin_balance" not in _written(wiring)

    def test_prev_margin_balance_triggers_correction(self, wiring) -> None:
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["corrections"] == [(DATE, 280)]

    def test_date_not_in_ohlc_map_is_skipped(self, monkeypatch, wiring) -> None:
        """月表回傳的是整月，請求日不在其中時不可誤用別天的資料。"""
        other = dt.date(2026, 8, 20)
        monkeypatch.setattr(run, "fetch_twse_taiex_ohlc", lambda s, d: {other: dict(OHLC)})
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "taiex_close" not in _written(wiring)


class TestPartialFailureIsolation:
    """四個子來源各自獨立失敗，不影響其他來源，也不阻止寫入已取得的部分。"""

    @pytest.mark.parametrize(
        "attr,exc,lost_key,kept_key",
        [
            ("fetch_twse_taiex_ohlc", DataUnavailableError("x"), "taiex_close", "total_volume"),
            ("fetch_twse_market_volume", DataUnavailableError("x"), "total_volume", "taiex_close"),
            ("fetch_twse_foreign_net", requests.RequestException("x"), "foreign_net", "taiex_close"),
            ("fetch_twse_market_margin", requests.RequestException("x"), "margin_balance", "taiex_close"),
        ],
    )
    def test_one_source_failure_keeps_the_rest(
        self, monkeypatch, wiring, attr, exc, lost_key, kept_key
    ) -> None:
        monkeypatch.setattr(run, attr, lambda *a, **k: (_ for _ in ()).throw(exc))
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        data = _written(wiring)
        assert lost_key not in data
        assert kept_key in data

    def test_all_sources_fail_writes_nothing(self, monkeypatch, wiring) -> None:
        for attr, exc in [
            ("fetch_twse_taiex_ohlc", DataUnavailableError("x")),
            ("fetch_twse_market_volume", DataUnavailableError("x")),
            ("fetch_twse_foreign_net", requests.RequestException("x")),
            ("fetch_twse_market_margin", requests.RequestException("x")),
        ]:
            monkeypatch.setattr(run, attr, lambda *a, **k: (_ for _ in ()).throw(exc))
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["upserts"] == []

    def test_foreign_net_none_is_not_written(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(run, "fetch_twse_foreign_net", lambda s, d: None)
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "foreign_net" not in _written(wiring)

    def test_empty_margin_payload_skips_correction(self, monkeypatch, wiring) -> None:
        monkeypatch.setattr(run, "fetch_twse_market_margin", lambda s, d: {})
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert wiring["corrections"] == []


class TestCorrectionFailureIsContained:
    def test_correction_exception_does_not_block_upsert(self, monkeypatch, wiring) -> None:
        """D-1 校正是附加動作，失敗不可拖累當日大盤資料的寫入。"""
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda *a: (_ for _ in ()).throw(RuntimeError("DB 連線中斷")),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert _written(wiring)["taiex_close"] == 24100.0

    def test_correction_result_is_reported(self, monkeypatch, capsys, wiring) -> None:
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda url, d, bal: (dt.date(2026, 8, 20), 270, 280, 10, 20),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        out = capsys.readouterr().out
        assert "2026-08-20" in out and "TWSE 事後修正" in out

    def test_correction_handles_null_old_values(self, monkeypatch, capsys, wiring) -> None:
        """舊值為 None 時印 NULL，不可因格式化 None 而丟例外。"""
        monkeypatch.setattr(
            run, "correct_prev_margin_balance",
            lambda url, d, bal: (dt.date(2026, 8, 20), None, 280, None, 20),
        )
        run._fetch_and_upsert_market_daily(None, DATE, CONFIG)
        assert "NULL" in capsys.readouterr().out
