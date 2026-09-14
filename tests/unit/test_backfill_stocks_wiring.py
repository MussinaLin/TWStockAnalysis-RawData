"""Unit tests: --backfill-stocks 走 per-stock provider 時不打全市場批次來源。

這是整個改造的驗收點：回補 1 檔 N 天，T86 / MI_INDEX / TPEX quotes / TPEX 3insti
的呼叫次數必須是 0（改造前是每天各 1 次）。
"""

from __future__ import annotations

import argparse
import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run


@pytest.fixture
def batch_spies(monkeypatch):
    """把四個全市場批次 fetch 換成計數器：被呼叫就代表改造沒生效。"""
    calls = {"t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0}

    monkeypatch.setattr(
        run, "_fetch_twse_3insti",
        lambda *a, **k: calls.__setitem__("t86", calls["t86"] + 1)
        or pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"]),
    )
    monkeypatch.setattr(
        run, "fetch_twse_mi_index",
        lambda *a, **k: calls.__setitem__("mi_index", calls["mi_index"] + 1)
        or (pd.DataFrame(), None),
    )
    monkeypatch.setattr(
        run, "_fetch_tpex_sources",
        lambda *a, **k: calls.__setitem__("tpex_quotes", calls["tpex_quotes"] + 1)
        or (None, None, None, None),
    )
    return calls


class _StubProvider:
    """最小 provider：固定回一天有價格、三大法人 OK。"""

    def ohlcv(self, symbol, date, market_type):  # noqa: ANN001 - 測試替身
        return run.OhlcvResult(
            open=100.0, close=105.0, high=106.0, low=99.0,
            volume=1_234_000, change=5.0,
        )

    def insti(self, symbol, date):  # noqa: ANN001 - 測試替身
        return (9_040_000, -1_293_000, 1_612_000)

    def insti_ok(self, symbol, market_type):  # noqa: ANN001 - 測試替身
        return True


def test_run_for_date_with_provider_skips_batch_fetches(
    batch_spies, monkeypatch
) -> None:
    written: list = []
    monkeypatch.setattr(
        run, "upsert_daily_raw",
        lambda url, date, df: written.append((date, len(df))),
    )

    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is True
    assert written == [(dt.date(2025, 7, 31), 1)]
    assert batch_spies == {
        "t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0
    }


def test_run_for_date_without_provider_still_fetches_batches(
    batch_spies, monkeypatch
) -> None:
    """不傳 provider 時維持現行行為（daily / 全市場 backfill 不受影響）。

    快取／處置名單一律傳空的：這個測試只驗「批次來源有沒有被呼叫」，若哪天
    `twse_confirmed` 之類的前置判斷改了而讓流程往下走，沒傳這些參數就會變成
    逐檔打真實的 MoneyDJ 與兩支處置公告端點（本 repo 有被限流封 IP 的前例，
    見 memory/twse-rate-limit-ambiguous-response.md）。
    """
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        margin_cache={},
        holding_pct_cache={},
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
    )

    assert batch_spies["mi_index"] >= 1


def test_run_for_date_with_provider_skips_weekend(batch_spies) -> None:
    """週末照舊直接跳過，provider 不改變這個判斷。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 8, 2),  # 週六
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is False


def test_per_symbol_and_batch_agree_on_same_row(monkeypatch) -> None:
    """同一天同一檔，兩種 provider 組出來的列必須一致（三大法人容許 ±1 張）。

    這是「單一組列路徑、不產生雙軌」的驗收點：差異只該來自來源精度，
    不該來自組列邏輯。
    """
    date = dt.date(2025, 7, 31)
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    # batch 來源：交易所精確股數
    mi_index = pd.DataFrame([{
        "symbol": "2330", "name": "台積電", "open": 1250.0, "close": 1255.0,
        "high": 1260.0, "low": 1245.0, "volume": 24_000_000, "change": -5.0,
    }])
    twse_3insti = pd.DataFrame([{
        "symbol": "2330", "foreign_net": 9_039_647,
        "trust_net": -1_292_952, "dealer_net": 1_611_836,
    }])
    batch_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.BatchSourceProvider(
            session=None,
            twse_3insti=twse_3insti,
            twse_day_all=None,
            twse_mi_index=mi_index,
            tpex_quotes=pd.DataFrame(
                columns=["symbol", "name", "open", "close", "high", "low",
                         "volume", "change"]
            ),
            tpex_3insti=pd.DataFrame(columns=["symbol"]),
            twse_month_cache={},
        ),
    ).iloc[0]

    # per-symbol 來源：月表 + MoneyDJ（張級）
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    ))
    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", lambda *a, **k: pd.DataFrame({
        "date": ["114/07/31"],
        "foreign_net_lots": ["9040"], "trust_net_lots": ["-1293"],
        "dealer_net_lots": ["1612"],
        "foreign_holding_pct": ["73.54%"], "insti_holding_pct": ["76.79%"],
    }))
    per_symbol_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.PerSymbolRangeProvider.build(
            session=None, symbols=["2330"], market_types={"2330": "twse"},
            start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        ),
    ).iloc[0]

    # 價格類完全相同
    for col in ("open", "high", "low", "close", "volume", "limit_up", "limit_down"):
        assert batch_row[col] == per_symbol_row[col], col

    # 三大法人容許 ±1 張（floor vs round）
    for col in ("foreign_net", "trust_net", "dealer_net"):
        assert abs(batch_row[col] - per_symbol_row[col]) <= 1, col


def test_per_symbol_range_provider_fetches_holding_pct_once_per_symbol(
    monkeypatch,
) -> None:
    """Finding 1 回歸測試：三大法人與外資/法人持股佔比來自同一次 MoneyDJ zcl
    fetch —— `PerSymbolRangeProvider.build` 對每檔只能呼叫一次
    `fetch_moneydj_holding_pct`，不能為了兩種資料各打一次。

    同時驗證 `provider.holding_pct_cache` 的形狀與內容能直接餵給
    `_build_daily_rows`，回傳的列上看得到持股佔比欄位。
    """
    calls: dict[str, int] = {}

    def _fake_holding_pct(session, symbol, start, end):  # noqa: ANN001 - 測試替身
        calls[symbol] = calls.get(symbol, 0) + 1
        return pd.DataFrame({
            "date": ["114/07/31"],
            "foreign_net_lots": ["9040"], "trust_net_lots": ["-1293"],
            "dealer_net_lots": ["1612"],
            "foreign_holding_pct": ["73.54%"], "insti_holding_pct": ["76.79%"],
            "trust_holding_lots": ["618021"], "insti_holding_lots": ["19916237"],
        })

    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", _fake_holding_pct)
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    ))

    provider = run.PerSymbolRangeProvider.build(
        session=None,
        symbols=["2330", "2317"],
        market_types={"2330": "twse", "2317": "twse"},
        start=dt.date(2025, 7, 1),
        end=dt.date(2025, 7, 31),
    )

    # 每檔恰好 1 次，不是 2 次（三大法人 + 持股佔比各一次）。
    assert calls == {"2330": 1, "2317": 1}

    holdings = pd.DataFrame([
        {"symbol": "2330", "market_type": "twse"},
        {"symbol": "2317", "market_type": "twse"},
    ])
    output_df = run._build_daily_rows(
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        provider=provider,
        holding_pct_cache=provider.holding_pct_cache,
    )

    assert len(output_df) == 2
    for _, row in output_df.iterrows():
        assert row["foreign_holding_pct"] == pytest.approx(0.7354)
        assert row["insti_holding_pct"] == pytest.approx(0.7679)
        assert row["trust_holding_pct"] == 0.0238


def test_reversed_backfill_range_is_normalized_before_prefetch(monkeypatch) -> None:
    """`--backfill-start` 比 `--backfill-end` 晚時不可變成靜默 no-op。

    `_build_date_range` 會正規化，但 `_prefetch_margin_cache` /
    `PerSymbolRangeProvider.build` 吃的是 start_date / end_date 本身；顛倒時
    `_month_starts` 回空 list，一發 OHLCV 都不抓，卻照樣印出「N 天」的抬頭。
    """
    seen: dict[str, tuple] = {}

    monkeypatch.setattr(run, "build_session", lambda: None)
    monkeypatch.setattr(run, "load_market_types", lambda url: {})
    monkeypatch.setattr(run, "load_stock_names", lambda url: {})
    monkeypatch.setattr(run, "_get_issued_shares", lambda session, config: {})
    monkeypatch.setattr(
        run, "_prefetch_margin_cache",
        lambda session, holdings, start, end: seen.__setitem__("margin", (start, end)) or {},
    )
    monkeypatch.setattr(
        run, "_fetch_disposition",
        lambda session, start, end: seen.__setitem__("disposition", (start, end))
        or run.DispositionData(by_date={}, ok_markets=frozenset()),
    )
    monkeypatch.setattr(
        run.PerSymbolRangeProvider, "build",
        classmethod(
            lambda cls, session, symbols, market_types, start, end: (
                seen.__setitem__("provider", (start, end))
                or cls(
                    ohlcv_by_symbol={}, insti_by_symbol={}, insti_ok_by_symbol={},
                    resolved_market_types={}, holding_pct_cache={},
                )
            )
        ),
    )
    monkeypatch.setattr(run, "_run_for_date", lambda *a, **k: False)

    args = argparse.Namespace(
        date=None, dahu=False, update_shares=False,
        backfill_limits=False, backfill_disposition=False,
        backfill_stocks="2330",
        backfill_start="2025-10-15", backfill_end="2025-08-01",  # 顛倒
        force=False,
    )

    class _Config:
        database_url = "postgresql://stub"

    run._main_inner(_Config(), args, dt.date(2026, 8, 22), dt.date(2026, 8, 22))

    normalized = (dt.date(2025, 8, 1), dt.date(2025, 10, 15))
    assert seen["margin"] == normalized
    assert seen["disposition"] == normalized
    assert seen["provider"] == normalized


def _provider_with(insti_ok: dict[str, bool], failed_months: dict) -> run.PerSymbolRangeProvider:
    return run.PerSymbolRangeProvider(
        ohlcv_by_symbol={},
        insti_by_symbol={},
        insti_ok_by_symbol=insti_ok,
        resolved_market_types={},
        holding_pct_cache={},
        failed_months_by_symbol=failed_months,
    )


def test_summary_reports_days_symbols_and_failed_months(capsys) -> None:
    """回補 1 檔 3 年會印約 780 行逐日訊息，唯一的解釋行早就捲不見了；
    收尾摘要必須把「寫了幾天／哪檔整檔沒寫／哪些月沒寫」重講一次。"""
    run._print_backfill_stocks_summary(
        provider=_provider_with(
            insti_ok={"2330": True, "2317": False},
            failed_months={"2330": [dt.date(2025, 7, 1), dt.date(2025, 8, 1)]},
        ),
        total_days=780,
        written_days=520,
    )

    out = capsys.readouterr().out
    assert "780" in out and "520" in out and "260" in out
    assert "2317" in out          # MoneyDJ 整檔失敗的個股
    assert "2025-07" in out and "2025-08" in out


def test_summary_says_so_when_nothing_failed(capsys) -> None:
    run._print_backfill_stocks_summary(
        provider=_provider_with(insti_ok={"2330": True}, failed_months={}),
        total_days=10,
        written_days=7,
    )

    out = capsys.readouterr().out
    assert "全部個股取得成功" in out
    assert "沒有判定為取得失敗的月份" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
