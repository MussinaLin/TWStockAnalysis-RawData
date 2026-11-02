"""Unit tests: --backfill-margin（只回補 stock_daily_raw 的 9 個融資融券欄位）。

逐日向交易所取整批（TWSE MI_MARGN、TPEX 融資融券 V2），資料日期必須等於請求日期
——TWSE 尚未發布時會回前一日的資料，寫下去等於把 D-1 的值標成 D（2026-04～05 實際
發生過的錯）。只 UPDATE 已存在的列、只寫這 9 欄，來源給 NULL 的欄位保留舊值。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils, run
from tw_stock_rawdata.sources import DataUnavailableError

MAY21 = dt.date(2026, 5, 21)
MAY22 = dt.date(2026, 5, 22)
MAY23 = dt.date(2026, 5, 23)   # 週六，DB 無列
CFG = SimpleNamespace(database_url="postgres://x")

MARGIN_COLS = [
    "margin_buy", "margin_sell", "margin_balance", "margin_change",
    "short_sell", "short_buy", "short_balance", "short_change",
    "short_margin_ratio",
]


def _data(**kw) -> dict:
    base = dict.fromkeys(MARGIN_COLS)
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# db_utils.update_margin_batch
# ---------------------------------------------------------------------------


class TestUpdateMarginBatch:
    def test_empty_updates_returns_zero(self) -> None:
        assert db_utils.update_margin_batch("postgres://x", []) == 0

    def test_updates_only_margin_columns_with_coalesce(self, monkeypatch) -> None:
        """必須是 UPDATE、只動這 9 欄，且 NULL 不覆寫舊值（同 upsert 的 COALESCE 不變量）。"""
        cursor = FakeCursor(rowcounts=[1])
        install_fake_pool(monkeypatch, db_utils, cursor)

        db_utils.update_margin_batch("postgres://x", [
            ("2330", MAY21, _data(margin_buy=655, margin_sell=731, margin_balance=26952,
                                  short_margin_ratio=0.00371)),
        ])

        sql, params = cursor.executed[0]
        assert sql.startswith("UPDATE stock_daily_raw")
        assert "INSERT" not in sql
        set_clause = sql.split(" SET ", 1)[1].split(" FROM ", 1)[0]
        assert set_clause.count(" = ") == len(MARGIN_COLS)   # 不多動任何其他欄
        assert re.findall(r"(\w+) = COALESCE\(v\.(\w+), t\.(\w+)\)", set_clause) == [
            (c, c, c) for c in MARGIN_COLS
        ]
        assert params == ["2330", MAY21, 655, 731, 26952, None, None, None, None, None,
                          0.00371]

    def test_chunks_to_stay_under_param_limit(self, monkeypatch) -> None:
        cursor = FakeCursor(rowcounts=[1000, 500])
        install_fake_pool(monkeypatch, db_utils, cursor)

        n_rows = db_utils._BATCH_UPDATE_CHUNK + 500
        db_utils.update_margin_batch(
            "postgres://x", [(f"{i:05d}", MAY21, _data(margin_buy=1)) for i in range(n_rows)]
        )

        assert len(cursor.executed) == 2
        assert len(cursor.executed[0][1]) == db_utils._BATCH_UPDATE_CHUNK * 11
        assert len(cursor.executed[1][1]) == 500 * 11

    def test_returns_total_rowcount_and_commits(self, monkeypatch) -> None:
        cursor = FakeCursor(rowcounts=[1])
        conn = install_fake_pool(monkeypatch, db_utils, cursor)

        n = db_utils.update_margin_batch(
            "postgres://x", [("2330", MAY21, _data(margin_buy=1)), ("9999", MAY21, _data())]
        )

        assert n == 1
        assert conn.committed is True


# ---------------------------------------------------------------------------
# _backfill_margin_command
# ---------------------------------------------------------------------------


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date=None, backfill_start="2026-05-21", backfill_end="2026-05-21",
        backfill_stocks=None, backfill_margin=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _table(*rows: dict) -> pd.DataFrame:
    """prepare_twse_margin / prepare_tpex_margin_v2 之後的標準形狀。"""
    return pd.DataFrame(list(rows))


def _row(symbol: str, buy: int, sell: int, bal: int, short_bal: int) -> dict:
    return {"symbol": symbol, "margin_buy": buy, "margin_sell": sell, "margin_balance": bal,
            "margin_change": buy - sell, "short_sell": 1, "short_buy": 2,
            "short_balance": short_bal, "short_change": -1}


@pytest.fixture
def backfill(monkeypatch):
    """換掉 DB 與兩個交易所來源（prepare 換成直通），只留指令流程。"""
    state: dict = {
        "db": {MAY21: {"2330", "6488"}, MAY22: {"2330", "6488"}},
        "twse": {}, "tpex": {}, "fetched": [], "written": [],
    }

    def fake_fetch(market):
        def _fetch(session, date):  # noqa: ANN001 - 測試替身
            state["fetched"].append((market, date))
            out = state[market].get(date)
            if out is None:
                raise DataUnavailableError(f"{market} 無資料")
            if isinstance(out, Exception):
                raise out
            return out
        return _fetch

    monkeypatch.setattr(run, "load_symbols_for_date",
                        lambda url, date: set(state["db"].get(date, set())))
    monkeypatch.setattr(run, "fetch_twse_margin", fake_fetch("twse"))
    monkeypatch.setattr(run, "fetch_tpex_margin_v2", fake_fetch("tpex"))
    monkeypatch.setattr(run, "prepare_twse_margin", lambda df: df)
    monkeypatch.setattr(run, "prepare_tpex_margin_v2", lambda df: df)
    monkeypatch.setattr(
        run, "update_margin_batch",
        lambda url, updates: (state["written"].append(list(updates)), len(updates))[1],
    )
    return state


def test_writes_twse_and_tpex_with_recomputed_short_margin_ratio(backfill) -> None:
    """上市取 TWSE、上櫃取 TPEX；券資比與 daily 同一個公式（_short_margin_ratio）重算。"""
    backfill["twse"][MAY21] = (_table(_row("2330", 655, 731, 26952, 100)), MAY21)
    backfill["tpex"][MAY21] = (_table(_row("6488", 10, 5, 2000, 50)), MAY21)

    run._backfill_margin_command(None, CFG, _args())

    (written,) = backfill["written"]
    by_symbol = {s: (d, data) for s, d, data in written}
    assert by_symbol["2330"][0] == MAY21
    assert by_symbol["2330"][1]["margin_buy"] == 655
    assert by_symbol["2330"][1]["margin_balance"] == 26952
    assert by_symbol["2330"][1]["short_margin_ratio"] == run._short_margin_ratio(26952, 100)
    assert by_symbol["6488"][1]["margin_balance"] == 2000
    assert by_symbol["6488"][1]["short_margin_ratio"] == run._short_margin_ratio(2000, 50)


def test_twse_date_mismatch_skips_twse_and_is_reported(backfill, capsys) -> None:
    """TWSE 回的是前一日資料 → 上市股這天一律不寫，上櫃照寫，並列為失敗。"""
    backfill["twse"][MAY21] = (_table(_row("2330", 632, 473, 27034, 89)),
                               dt.date(2026, 5, 20))
    backfill["tpex"][MAY21] = (_table(_row("6488", 10, 5, 2000, 50)), MAY21)

    run._backfill_margin_command(None, CFG, _args())

    assert [s for s, _, _ in backfill["written"][0]] == ["6488"]
    out = capsys.readouterr().out
    assert "2026-05-20" in out
    assert "2026-05-21" in out.strip().splitlines()[-1]


def test_tpex_without_data_date_is_not_trusted(backfill) -> None:
    """歷史回補不接受無法確認日期的整批資料（daily 模式才容忍 V2 不回日期）。"""
    backfill["twse"][MAY21] = (_table(_row("2330", 655, 731, 26952, 100)), MAY21)
    backfill["tpex"][MAY21] = (_table(_row("6488", 10, 5, 2000, 50)), None)

    run._backfill_margin_command(None, CFG, _args())

    assert [s for s, _, _ in backfill["written"][0]] == ["2330"]


def test_symbol_missing_from_source_is_left_alone(backfill) -> None:
    """不在交易所表內（例如不開放信用交易）→ 不產生更新，不以全 NULL 覆寫。"""
    backfill["twse"][MAY21] = (_table(_row("2330", 655, 731, 26952, 100)), MAY21)
    backfill["tpex"][MAY21] = (_table(_row("9999", 1, 1, 1, 1)), MAY21)

    run._backfill_margin_command(None, CFG, _args())

    assert [s for s, _, _ in backfill["written"][0]] == ["2330"]


def test_only_symbols_with_rows_in_db_are_updated(backfill) -> None:
    """交易所表是全市場；只寫該日 DB 已有列的個股（本 repo 不新增列）。"""
    backfill["twse"][MAY21] = (_table(_row("2330", 655, 731, 26952, 100),
                                      _row("2317", 1, 1, 1, 1)), MAY21)
    backfill["tpex"][MAY21] = (_table(), MAY21)

    run._backfill_margin_command(None, CFG, _args())

    assert [s for s, _, _ in backfill["written"][0]] == ["2330"]


def test_honours_backfill_stocks(backfill) -> None:
    backfill["twse"][MAY21] = (_table(_row("2330", 655, 731, 26952, 100)), MAY21)
    backfill["tpex"][MAY21] = (_table(_row("6488", 10, 5, 2000, 50)), MAY21)

    run._backfill_margin_command(None, CFG, _args(backfill_stocks="6488"))

    assert [s for s, _, _ in backfill["written"][0]] == ["6488"]


def test_day_without_db_rows_makes_no_request(backfill) -> None:
    """週末與休市日 DB 無列 → 不打交易所（每個請求都可能被 ban）。"""
    backfill["twse"][MAY22] = (_table(_row("2330", 699, 692, 26946, 97)), MAY22)
    backfill["tpex"][MAY22] = (_table(), MAY22)

    run._backfill_margin_command(
        None, CFG, _args(backfill_start="2026-05-22", backfill_end="2026-05-23")
    )

    assert {d for _, d in backfill["fetched"]} == {MAY22}


def test_fetch_failure_does_not_stop_other_days(backfill, capsys) -> None:
    backfill["twse"][MAY21] = DataUnavailableError("MI_MARGN 回傳異常")
    backfill["tpex"][MAY21] = (_table(), MAY21)
    backfill["twse"][MAY22] = (_table(_row("2330", 699, 692, 26946, 97)), MAY22)
    backfill["tpex"][MAY22] = (_table(), MAY22)

    run._backfill_margin_command(
        None, CFG, _args(backfill_start="2026-05-21", backfill_end="2026-05-22")
    )

    written = [(s, d) for batch in backfill["written"] for s, d, _ in batch]
    assert written == [("2330", MAY22)]
    assert "2026-05-21" in capsys.readouterr().out.strip().splitlines()[-1]


def test_requires_start_and_end(backfill, capsys) -> None:
    run._backfill_margin_command(None, CFG, _args(backfill_end=None))

    assert backfill["fetched"] == []
    assert "--backfill-start" in capsys.readouterr().out
