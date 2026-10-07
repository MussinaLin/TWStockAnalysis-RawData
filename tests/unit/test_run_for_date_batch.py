"""Unit tests: _run_for_date 的 batch 路徑（provider=None）。

docs/refactor-plan.md T1 第 1 步。現有三個 _run_for_date 測試全走
`--backfill-stocks` 的 provider 早返路徑，在 run.py:1988 就 return，
從未進入後面 227 行——那一段（TPEX 整批之後）覆蓋率是 0%。

拆函式之前必須先有這層保護：本檔把 batch 路徑的每個階段
（三大法人 / STOCK_DAY_ALL / MI_INDEX / twse_confirmed 閘門 / TPEX 整批 /
融資融券三分支 / 持股佔比 / 處置名單 / 組列與寫入）各自的分支釘住。

所有外部相依都 monkeypatch 並記錄呼叫，斷言的是「誰被呼叫、拿到什麼參數」，
不是實際資料內容。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import run
from tw_stock_rawdata.config import AppConfig
from tw_stock_rawdata.sources import DataUnavailableError

CONFIG = AppConfig(database_url="postgres://x", use_db=True)
TODAY = dt.date(2026, 8, 21)          # 週五
PAST = dt.date(2026, 8, 19)           # 週三，歷史日
WEEKEND = dt.date(2026, 8, 22)        # 週六

INSTI_COLS = ["symbol", "foreign_net", "trust_net", "dealer_net"]
QUOTE_COLS = ["symbol", "name", "open", "close", "high", "low", "volume"]


def _holdings() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "2330", "name": "台積電", "market_type": "twse"},
        {"symbol": "6488", "name": "環球晶", "market_type": "tpex"},
    ])


def _insti(symbol="2330") -> pd.DataFrame:
    return pd.DataFrame([{"symbol": symbol, "foreign_net": 1,
                          "trust_net": 2, "dealer_net": 3}])


def _quotes(symbol="6488") -> pd.DataFrame:
    return pd.DataFrame([{"symbol": symbol, "name": "x", "open": 1.0,
                          "close": 2.0, "high": 3.0, "low": 0.5, "volume": 100}])


def _rows_df(close=2.0) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": "2330", "close": close}])


@pytest.fixture
def wiring(monkeypatch):
    """batch 路徑的完整假接線。calls 記錄每個外部相依被呼叫幾次與拿到什麼。"""
    calls: dict = {"upserts": [], "build_rows": [], "market_daily": 0,
                   "refresh_prev": 0, "disposition": []}

    monkeypatch.setattr(run, "_fetch_twse_3insti", lambda s, d: _insti())
    monkeypatch.setattr(
        run, "fetch_twse_stock_day_all", lambda s: (pd.DataFrame(), TODAY))
    monkeypatch.setattr(run, "prepare_twse_day_all", lambda raw: _quotes("2330"))
    monkeypatch.setattr(
        run, "fetch_twse_mi_index", lambda s, d: (pd.DataFrame([{"x": 1}]), d))
    monkeypatch.setattr(run, "prepare_twse_mi_index", lambda raw: _quotes("2330"))
    monkeypatch.setattr(
        run, "_fetch_tpex_sources",
        lambda s, d: (_quotes(), d, _insti("6488"), d))

    monkeypatch.setattr(run, "fetch_twse_margin", lambda s, d: (pd.DataFrame(), d))
    monkeypatch.setattr(run, "prepare_twse_margin", lambda raw: pd.DataFrame())
    monkeypatch.setattr(run, "fetch_tpex_margin_v2", lambda s, d: (pd.DataFrame(), d))
    monkeypatch.setattr(run, "prepare_tpex_margin_v2", lambda raw: pd.DataFrame())
    monkeypatch.setattr(run, "fetch_tpex_margin", lambda s: (pd.DataFrame(), None))
    monkeypatch.setattr(run, "prepare_tpex_margin", lambda raw: pd.DataFrame())
    monkeypatch.setattr(
        run, "fetch_moneydj_margin",
        lambda s, sym, a, b: (_ for _ in ()).throw(DataUnavailableError("no")))
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct",
        lambda s, sym, a, b: (_ for _ in ()).throw(DataUnavailableError("no")))

    def fake_disposition(s, a, b):
        calls["disposition"].append((a, b))
        return None

    monkeypatch.setattr(run, "_fetch_disposition", fake_disposition)

    def fake_build(**kw):
        calls["build_rows"].append(kw)
        return _rows_df()

    monkeypatch.setattr(run, "_build_daily_rows", fake_build)
    monkeypatch.setattr(
        run, "upsert_daily_raw",
        lambda url, d, df: calls["upserts"].append((d, len(df))))
    monkeypatch.setattr(
        run, "_fetch_and_upsert_market_daily",
        lambda s, d, c: calls.__setitem__("market_daily", calls["market_daily"] + 1))
    monkeypatch.setattr(
        run, "_refresh_prev_day_margin",
        lambda s, h, d, c: calls.__setitem__("refresh_prev", calls["refresh_prev"] + 1))
    return calls


def _run(date=TODAY, today=TODAY, **kw):
    params = dict(
        session=None, date=date, holdings=_holdings(), sheet_names=set(),
        twse_month_cache={}, config=CONFIG, today=today,
    )
    params.update(kw)
    return run._run_for_date(**params)


class TestEarlyGuards:
    def test_weekend_is_skipped(self, wiring) -> None:
        assert _run(date=WEEKEND) is False
        assert wiring["upserts"] == []

    def test_skip_existing_short_circuits(self, wiring) -> None:
        names = {TODAY.isoformat()}
        assert _run(sheet_names=names, skip_existing=True) is False
        assert wiring["upserts"] == []

    def test_without_skip_existing_still_writes(self, wiring) -> None:
        names = {TODAY.isoformat()}
        assert _run(sheet_names=names, skip_existing=False) is True


class TestTwseConfirmedGate:
    """三個來源全都拿不到當日資料時視為休市，不可寫入。"""

    def test_all_sources_missing_returns_false(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(
            run, "_fetch_twse_3insti",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("尚未公告")))
        monkeypatch.setattr(
            run, "fetch_twse_mi_index",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("尚未公告")))
        assert _run(date=PAST, today=TODAY) is False
        assert wiring["upserts"] == []

    def test_only_insti_available_is_enough(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(
            run, "fetch_twse_mi_index",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("x")))
        assert _run(date=PAST, today=TODAY) is True

    def test_network_error_on_insti_aborts_immediately(self, wiring, monkeypatch) -> None:
        """連線失敗與「尚未公告」不同——前者直接放棄，不往下打其他來源。"""
        monkeypatch.setattr(
            run, "_fetch_twse_3insti",
            lambda s, d: (_ for _ in ()).throw(requests.ConnectionError("boom")))
        assert _run() is False
        assert wiring["build_rows"] == []


class TestStockDayAllOnlyToday:
    def test_not_fetched_for_historical_date(self, wiring, monkeypatch) -> None:
        called = {"n": 0}

        def spy(s):
            called["n"] += 1
            return pd.DataFrame(), PAST

        monkeypatch.setattr(run, "fetch_twse_stock_day_all", spy)
        _run(date=PAST, today=TODAY)
        assert called["n"] == 0

    def test_fetched_for_today(self, wiring, monkeypatch) -> None:
        called = {"n": 0}

        def spy(s):
            called["n"] += 1
            return pd.DataFrame(), TODAY

        monkeypatch.setattr(run, "fetch_twse_stock_day_all", spy)
        _run()
        assert called["n"] == 1

    def test_date_mismatch_is_discarded(self, wiring, monkeypatch) -> None:
        """回傳別天的資料不可誤用。"""
        monkeypatch.setattr(
            run, "fetch_twse_stock_day_all", lambda s: (pd.DataFrame(), PAST))
        used = {}
        monkeypatch.setattr(
            run, "prepare_twse_day_all",
            lambda raw: used.setdefault("called", True) or _quotes())
        _run()
        assert "called" not in used


class TestMarginBranches:
    """三個互斥分支：cache 已備 / 當日走整批 API / 歷史走逐檔 MoneyDJ。"""

    def test_cache_provided_skips_all_margin_fetches(self, wiring, monkeypatch) -> None:
        hits = {"n": 0}
        for name in ("fetch_twse_margin", "fetch_tpex_margin_v2",
                     "fetch_tpex_margin", "fetch_moneydj_margin"):
            monkeypatch.setattr(
                run, name,
                lambda *a, **k: hits.__setitem__("n", hits["n"] + 1) or (pd.DataFrame(), TODAY))
        _run(margin_cache={"2330": {}})
        assert hits["n"] == 0

    def test_today_uses_batch_apis(self, wiring, monkeypatch) -> None:
        hits = []
        monkeypatch.setattr(
            run, "fetch_twse_margin",
            lambda s, d: hits.append("twse") or (pd.DataFrame(), d))
        monkeypatch.setattr(
            run, "fetch_tpex_margin_v2",
            lambda s, d: hits.append("tpex_v2") or (pd.DataFrame(), d))
        _run()
        assert hits == ["twse", "tpex_v2"]

    def test_tpex_v2_failure_falls_back_to_openapi(self, wiring, monkeypatch) -> None:
        hits = []
        monkeypatch.setattr(
            run, "fetch_tpex_margin_v2",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("v2 掛了")))
        monkeypatch.setattr(
            run, "fetch_tpex_margin",
            lambda s: hits.append("openapi") or (pd.DataFrame(), None))
        _run()
        assert hits == ["openapi"]

    def test_twse_margin_date_mismatch_is_discarded(self, wiring, monkeypatch) -> None:
        """日期不符不可寫——缺值由 D+1 的 MoneyDJ 修正補。"""
        monkeypatch.setattr(run, "fetch_twse_margin", lambda s, d: (pd.DataFrame(), PAST))
        used = {}
        monkeypatch.setattr(
            run, "prepare_twse_margin",
            lambda raw: used.setdefault("called", True) or pd.DataFrame())
        _run()
        assert "called" not in used

    def test_historical_date_uses_moneydj_per_symbol(self, wiring, monkeypatch) -> None:
        seen = []
        monkeypatch.setattr(
            run, "fetch_moneydj_margin",
            lambda s, sym, a, b: seen.append(sym) or (_ for _ in ()).throw(
                DataUnavailableError("no")))
        _run(date=PAST, today=TODAY)
        assert seen == ["2330", "6488"]

    def test_historical_date_does_not_use_batch_apis(self, wiring, monkeypatch) -> None:
        hits = {"n": 0}
        monkeypatch.setattr(
            run, "fetch_twse_margin",
            lambda s, d: hits.__setitem__("n", 1) or (pd.DataFrame(), d))
        _run(date=PAST, today=TODAY)
        assert hits["n"] == 0


class TestHoldingPctAndDisposition:
    def test_cache_provided_skips_per_symbol_fetch(self, wiring, monkeypatch) -> None:
        hits = {"n": 0}
        monkeypatch.setattr(
            run, "fetch_moneydj_holding_pct",
            lambda *a: hits.__setitem__("n", 1) or pd.DataFrame())
        _run(holding_pct_cache={"2330": {}})
        assert hits["n"] == 0

    def test_no_cache_fetches_each_symbol(self, wiring, monkeypatch) -> None:
        seen = []
        monkeypatch.setattr(
            run, "fetch_moneydj_holding_pct",
            lambda s, sym, a, b: seen.append(sym) or (_ for _ in ()).throw(
                DataUnavailableError("no")))
        _run()
        assert seen == ["2330", "6488"]

    def test_per_symbol_failure_is_logged(self, wiring, capsys) -> None:
        """抓不到持股佔比仍不 gating，但不可靜默——要印出哪一檔、為什麼。"""
        _run()  # wiring 預設 fetch_moneydj_holding_pct 一律丟 DataUnavailableError("no")
        out = capsys.readouterr().out
        assert "2330 法人持股取得失敗：no" in out
        assert "6488 法人持股取得失敗：no" in out

    def test_disposition_prefetched_is_not_refetched(self, wiring) -> None:
        sentinel = run.DispositionData({}, frozenset())
        _run(disposition=sentinel)
        assert wiring["disposition"] == []

    def test_disposition_fetched_for_single_day_window(self, wiring) -> None:
        _run()
        assert wiring["disposition"] == [(TODAY, TODAY)]


class TestWriteAndSideEffects:
    def test_empty_rows_skips_write(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(run, "_build_daily_rows", lambda **k: pd.DataFrame())
        assert _run() is False
        assert wiring["upserts"] == []

    def test_all_close_na_skips_write(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(
            run, "_build_daily_rows",
            lambda **k: pd.DataFrame([{"symbol": "2330", "close": None}]))
        assert _run() is False
        assert wiring["upserts"] == []

    def test_successful_write_registers_sheet_name(self, wiring) -> None:
        names: set[str] = set()
        assert _run(sheet_names=names) is True
        assert names == {TODAY.isoformat()}
        assert wiring["upserts"] == [(TODAY, 1)]

    def test_market_daily_written_by_default(self, wiring) -> None:
        _run()
        assert wiring["market_daily"] == 1

    def test_market_daily_skipped_when_disabled(self, wiring) -> None:
        _run(write_market_daily=False)
        assert wiring["market_daily"] == 0

    def test_prev_margin_refreshed_only_without_cache(self, wiring) -> None:
        _run()
        assert wiring["refresh_prev"] == 1

    def test_prev_margin_not_refreshed_with_cache(self, wiring) -> None:
        _run(margin_cache={"2330": {}})
        assert wiring["refresh_prev"] == 0

    def test_build_rows_receives_insti_health_via_provider(self, wiring) -> None:
        """twse/tpex 三大法人健康度透過 BatchSourceProvider 傳給組列。"""
        _run()
        provider = wiring["build_rows"][0]["provider"]
        assert isinstance(provider, run.BatchSourceProvider)


class TestFailurePathsAndSuccessBranches:
    """補齊錯誤分支與 MoneyDJ 成功路徑——H 段（融資融券）是下一步要抽出的，
    抽之前每條分支都要有保護。"""

    def test_tpex_batch_failure_falls_back_to_empty_frames(self, wiring, monkeypatch) -> None:
        """TPEX 整批失敗時以空表續行，不可中斷整天——上市股仍要寫入。"""
        monkeypatch.setattr(
            run, "_fetch_tpex_sources",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("TPEX 掛了")))
        assert _run() is True
        provider = wiring["build_rows"][0]["provider"]
        assert provider._tpex_quotes.empty
        assert provider._tpex_3insti.empty

    def test_tpex_date_mismatch_is_reported(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            run, "_fetch_tpex_sources",
            lambda s, d: (_quotes(), PAST, _insti("6488"), PAST))
        _run()
        out = capsys.readouterr().out
        assert "TPEX 日行情日期不匹配" in out
        assert "TPEX 三大法人日期不匹配" in out

    def test_twse_margin_fetch_failure_is_contained(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            run, "fetch_twse_margin",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("MI_MARGN 掛了")))
        assert _run() is True
        assert "TWSE 融資融券取得失敗" in capsys.readouterr().out

    def test_tpex_v2_margin_date_mismatch_falls_back(self, wiring, monkeypatch, capsys) -> None:
        """V2 回別天的資料時棄用並退回 OpenAPI。"""
        monkeypatch.setattr(run, "fetch_tpex_margin_v2", lambda s, d: (pd.DataFrame(), PAST))
        hits = []
        monkeypatch.setattr(
            run, "fetch_tpex_margin", lambda s: hits.append("openapi") or (pd.DataFrame(), None))
        _run()
        assert hits == ["openapi"]
        assert "TPEX V2 融資融券日期不匹配" in capsys.readouterr().out

    def test_openapi_margin_date_mismatch_is_discarded(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            run, "fetch_tpex_margin_v2",
            lambda s, d: (_ for _ in ()).throw(DataUnavailableError("v2")))
        monkeypatch.setattr(run, "fetch_tpex_margin", lambda s: (pd.DataFrame(), PAST))
        used = {}
        monkeypatch.setattr(
            run, "prepare_tpex_margin",
            lambda raw: used.setdefault("called", True) or pd.DataFrame())
        _run()
        assert "called" not in used
        assert "TPEX OpenAPI 融資融券日期不匹配" in capsys.readouterr().out

    def test_both_tpex_margin_sources_fail(self, wiring, monkeypatch, capsys) -> None:
        for name in ("fetch_tpex_margin_v2", "fetch_tpex_margin"):
            monkeypatch.setattr(
                run, name, lambda *a: (_ for _ in ()).throw(DataUnavailableError("掛了")))
        assert _run() is True
        assert "TPEX 融資融券取得失敗" in capsys.readouterr().out

    def test_moneydj_margin_success_builds_frame(self, wiring, monkeypatch) -> None:
        """歷史日的逐檔 MoneyDJ 成功路徑：只取目標日那一列，並補上 symbol。"""
        monkeypatch.setattr(run, "fetch_moneydj_margin", lambda s, sym, a, b: pd.DataFrame())
        monkeypatch.setattr(
            run, "prepare_moneydj_margin",
            lambda raw: pd.DataFrame([
                {"date": PAST, "margin_balance": 100},
                {"date": dt.date(2026, 8, 18), "margin_balance": 90},
            ]))
        _run(date=PAST, today=TODAY)
        twse_margin = wiring["build_rows"][0]["twse_margin"]
        assert len(twse_margin) == 2          # 兩檔各一列
        assert set(twse_margin["symbol"]) == {"2330", "6488"}
        assert set(twse_margin["margin_balance"]) == {100}

    def test_moneydj_margin_without_target_date_yields_nothing(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(run, "fetch_moneydj_margin", lambda s, sym, a, b: pd.DataFrame())
        monkeypatch.setattr(
            run, "prepare_moneydj_margin",
            lambda raw: pd.DataFrame([{"date": dt.date(2026, 8, 18), "margin_balance": 90}]))
        _run(date=PAST, today=TODAY)
        assert wiring["build_rows"][0]["twse_margin"] is None

    def test_holding_pct_success_populates_cache(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(run, "fetch_moneydj_holding_pct", lambda s, sym, a, b: pd.DataFrame())
        monkeypatch.setattr(
            run, "prepare_moneydj_holding_pct",
            lambda raw: pd.DataFrame([
                {"date": TODAY, "foreign_holding_pct": 0.35, "insti_holding_pct": 0.4},
                {"date": "壞日期", "foreign_holding_pct": 0.1, "insti_holding_pct": 0.2},
            ]))
        _run()
        cache = wiring["build_rows"][0]["holding_pct_cache"]
        assert cache["2330"][TODAY]["foreign_holding_pct"] == 0.35
        # 非 date 型別的列被跳過，不可混進 cache
        assert list(cache["2330"]) == [TODAY]


class TestBatchSourceEdgeBranches:
    """D / E 段（STOCK_DAY_ALL、MI_INDEX）的錯誤與退化分支。
    這兩段結構相同，是後續要抽共用 helper 的對象。"""

    def test_stock_day_all_unparseable_date_is_skipped(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(run, "fetch_twse_stock_day_all", lambda s: (pd.DataFrame(), None))
        used = {}
        monkeypatch.setattr(
            run, "prepare_twse_day_all",
            lambda raw: used.setdefault("called", True) or _quotes())
        _run()
        assert "called" not in used
        assert "無法解析日期" in capsys.readouterr().out

    def test_stock_day_all_fetch_failure_is_contained(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            run, "fetch_twse_stock_day_all",
            lambda s: (_ for _ in ()).throw(DataUnavailableError("openapi 掛了")))
        assert _run() is True
        assert "TWSE STOCK_DAY_ALL 取得失敗" in capsys.readouterr().out

    def test_mi_index_null_date_today_is_treated_as_today(self, wiring, monkeypatch) -> None:
        """MI_INDEX 沒宣告日期但有資料且抓的是今天 → 視為今天，不棄用。"""
        monkeypatch.setattr(
            run, "fetch_twse_mi_index", lambda s, d: (pd.DataFrame([{"x": 1}]), None))
        used = {}
        monkeypatch.setattr(
            run, "prepare_twse_mi_index",
            lambda raw: used.setdefault("called", True) or _quotes("2330"))
        _run()
        assert used.get("called") is True

    def test_mi_index_null_date_historical_is_discarded(self, wiring, monkeypatch) -> None:
        """同樣沒宣告日期，但抓的是歷史日 → 不可假設是那天。"""
        monkeypatch.setattr(
            run, "fetch_twse_mi_index", lambda s, d: (pd.DataFrame([{"x": 1}]), None))
        used = {}
        monkeypatch.setattr(
            run, "prepare_twse_mi_index",
            lambda raw: used.setdefault("called", True) or _quotes("2330"))
        _run(date=PAST, today=TODAY)
        assert "called" not in used

    def test_mi_index_date_mismatch_is_reported(self, wiring, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            run, "fetch_twse_mi_index", lambda s, d: (pd.DataFrame([{"x": 1}]), PAST))
        _run()
        assert "TWSE MI_INDEX 日期不匹配" in capsys.readouterr().out


class TestProviderPathRemainingBranches:
    """provider 早返路徑尚未覆蓋的兩條分支。"""

    def _provider(self):
        class _P:
            def ohlcv(self, symbol, date, market_type):
                return run.OhlcvResult(None, None, None, None, None, None)

            def insti(self, symbol, date):
                return (None, None, None)

            def insti_ok(self, symbol, market_type):
                return True

        return _P()

    def test_fetches_disposition_when_not_prefetched(self, wiring) -> None:
        _run(provider=self._provider())
        assert wiring["disposition"] == [(TODAY, TODAY)]

    def test_empty_rows_returns_false(self, wiring, monkeypatch) -> None:
        monkeypatch.setattr(run, "_build_daily_rows", lambda **k: pd.DataFrame())
        assert _run(provider=self._provider(), disposition=run.DispositionData({}, frozenset())) is False
        assert wiring["upserts"] == []
