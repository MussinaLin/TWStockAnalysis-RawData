"""Unit tests: --backfill-trust-holding（只回補 stock_daily_raw.trust_holding_pct）。

每檔整段只打 1 次 MoneyDJ zcl，逐檔只 UPDATE 已存在的列、只寫這一欄；
算不出來的日期跳過，不以 NULL 覆寫。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils, run
from tw_stock_rawdata.sources import DataUnavailableError

JUL30 = dt.date(2025, 7, 30)
JUL31 = dt.date(2025, 7, 31)
CFG = SimpleNamespace(database_url="postgres://x")


# ---------------------------------------------------------------------------
# db_utils.update_trust_holding_batch
# ---------------------------------------------------------------------------


class TestUpdateTrustHoldingBatch:
    def test_empty_updates_returns_zero(self) -> None:
        assert db_utils.update_trust_holding_batch("postgres://x", []) == 0

    def test_updates_only_trust_holding_pct(self, monkeypatch) -> None:
        """必須是 UPDATE 且只動這一欄：走 upsert 會 INSERT 出半套 row，多動欄位會蓋掉別的資料。"""
        cursor = FakeCursor(rowcounts=[1])
        install_fake_pool(monkeypatch, db_utils, cursor)

        db_utils.update_trust_holding_batch("postgres://x", [("2330", JUL31, 0.0238)])

        sql, params = cursor.executed[0]
        assert sql.startswith("UPDATE stock_daily_raw")
        assert "INSERT" not in sql
        set_clause = sql.split(" SET ", 1)[1].split(" FROM ", 1)[0]
        assert set_clause.strip() == "trust_holding_pct = v.trust_holding_pct"
        assert params == ["2330", JUL31, 0.0238]

    def test_chunks_to_stay_under_param_limit(self, monkeypatch) -> None:
        """一批一句（逐列 execute 是一列一次網路往返），超過 chunk 大小才分句。"""
        cursor = FakeCursor(rowcounts=[1000, 500])
        install_fake_pool(monkeypatch, db_utils, cursor)

        n_rows = db_utils._BATCH_UPDATE_CHUNK + 500
        updates = [(f"{i:05d}", JUL31, 0.01) for i in range(n_rows)]
        db_utils.update_trust_holding_batch("postgres://x", updates)

        assert len(cursor.executed) == 2
        assert len(cursor.executed[0][1]) == db_utils._BATCH_UPDATE_CHUNK * 3
        assert len(cursor.executed[1][1]) == 500 * 3

    def test_returns_total_rowcount_and_commits(self, monkeypatch) -> None:
        """不存在的 (symbol, trade_date) 不計入 —— 由 UPDATE 的 rowcount 反映。"""
        cursor = FakeCursor(rowcounts=[1])
        conn = install_fake_pool(monkeypatch, db_utils, cursor)

        n = db_utils.update_trust_holding_batch(
            "postgres://x", [("2330", JUL31, 0.0238), ("9999", JUL31, 0.01)]
        )

        assert n == 1
        assert conn.committed is True


class TestLoadSymbolsInRange:
    def test_returns_sorted_distinct_symbols(self, monkeypatch) -> None:
        conn = install_fake_pool(
            monkeypatch, db_utils, fetchall_rows=[("2330",), ("1101",), ("2330",)]
        )

        symbols = db_utils.load_symbols_in_range("postgres://x", JUL30, JUL31)

        assert symbols == ["1101", "2330"]
        sql, params = conn.executed[0]
        assert "stock_daily_raw" in sql
        assert params == (JUL30, JUL31)


# ---------------------------------------------------------------------------
# _backfill_trust_holding_command
# ---------------------------------------------------------------------------


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date=None, backfill_start="2025-07-30", backfill_end="2025-07-31",
        backfill_stocks=None, backfill_trust_holding=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _zcl_raw(*rows: tuple[str, str, str, str]) -> pd.DataFrame:
    """fetch_moneydj_holding_pct 的完整輸出形狀。

    每列給 (ROC 日期, 三大法人持股比例, 投信估計持股, 合計估計持股)，其餘欄位填固定值。
    """
    n = len(rows)
    return pd.DataFrame({
        "date": [r[0] for r in rows],
        "foreign_net_lots": ["0"] * n,
        "trust_net_lots": ["0"] * n,
        "dealer_net_lots": ["0"] * n,
        "foreign_holding_pct": ["73.54%"] * n,
        "insti_holding_pct": [r[1] for r in rows],
        "trust_holding_lots": [r[2] for r in rows],
        "insti_holding_lots": [r[3] for r in rows],
    })


@pytest.fixture
def backfill(monkeypatch):
    """換掉 DB 與 MoneyDJ，只留指令流程與真實的 prepare／轉換。"""
    state: dict = {"symbols": ["2330"], "raw": {}, "fetched": [], "written": []}

    def fake_fetch(session, symbol, start, end):  # noqa: ANN001 - 測試替身
        state["fetched"].append((symbol, start, end))
        raw = state["raw"][symbol]
        if isinstance(raw, Exception):
            raise raw
        return raw

    monkeypatch.setattr(
        run, "load_symbols_in_range", lambda url, start, end: list(state["symbols"])
    )
    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", fake_fetch)
    monkeypatch.setattr(
        run, "update_trust_holding_batch",
        lambda url, updates: (state["written"].append(list(updates)), len(updates))[1],
    )
    return state


def test_writes_derived_values_and_skips_uncomputable_dates(backfill) -> None:
    """算不出來的日期（比例缺值）直接跳過，不以 NULL 覆寫既有值。"""
    backfill["raw"]["2330"] = _zcl_raw(
        ("114/07/31", "76.79%", "618021", "19916237"),
        ("114/07/30", "--", "619314", "19907166"),
    )

    run._backfill_trust_holding_command(None, CFG, _args())

    assert backfill["fetched"] == [("2330", JUL30, JUL31)]
    assert backfill["written"] == [[("2330", JUL31, 0.0238)]]


def test_dates_outside_requested_range_are_not_written(backfill) -> None:
    """來源多給的日期不可順手寫進去——回補範圍以參數為準。"""
    backfill["raw"]["2330"] = _zcl_raw(
        ("114/08/01", "76.80%", "618000", "19916000"),
        ("114/07/31", "76.79%", "618021", "19916237"),
    )

    run._backfill_trust_holding_command(None, CFG, _args())

    assert backfill["written"] == [[("2330", JUL31, 0.0238)]]


def test_honours_backfill_stocks(backfill) -> None:
    backfill["symbols"] = ["2317", "2330"]
    backfill["raw"]["2317"] = _zcl_raw(("114/07/31", "10.00%", "25", "100"))

    run._backfill_trust_holding_command(None, CFG, _args(backfill_stocks="2317"))

    assert [f[0] for f in backfill["fetched"]] == ["2317"]
    assert backfill["written"] == [[("2317", JUL31, 0.025)]]


def test_moneydj_failure_skips_symbol_and_is_reported(backfill, capsys) -> None:
    """某檔失敗不中斷其他檔；收尾要列出失敗代號，操作者才知道要重跑哪幾檔。"""
    backfill["symbols"] = ["2317", "2330"]
    backfill["raw"]["2317"] = DataUnavailableError("MoneyDJ 找不到法人持股表格")
    backfill["raw"]["2330"] = _zcl_raw(("114/07/31", "76.79%", "618021", "19916237"))

    run._backfill_trust_holding_command(None, CFG, _args())

    assert backfill["written"] == [[("2330", JUL31, 0.0238)]]
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "2317" in last_line


def test_no_rows_in_range_skips_moneydj_entirely(backfill) -> None:
    backfill["symbols"] = []

    run._backfill_trust_holding_command(None, CFG, _args())

    assert backfill["fetched"] == []


def test_requires_start_and_end(backfill) -> None:
    run._backfill_trust_holding_command(None, CFG, _args(backfill_end=None))

    assert backfill["fetched"] == []


def test_reversed_range_is_normalized(backfill) -> None:
    backfill["raw"]["2330"] = _zcl_raw(("114/07/31", "76.79%", "618021", "19916237"))

    run._backfill_trust_holding_command(
        None, CFG, _args(backfill_start="2025-07-31", backfill_end="2025-07-30")
    )

    assert backfill["fetched"] == [("2330", JUL30, JUL31)]


def test_date_is_ignored_with_warning(backfill, capsys) -> None:
    backfill["raw"]["2330"] = _zcl_raw(("114/07/31", "76.79%", "618021", "19916237"))

    run._backfill_trust_holding_command(None, CFG, _args(date="2025-07-31"))

    out = capsys.readouterr().out
    assert "警告" in out
    assert "--date" in out
    assert backfill["fetched"] == [("2330", JUL30, JUL31)]


def test_cli_flag_parses(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", [
        "tw-stock-rawdata", "--backfill-trust-holding",
        "--backfill-start", "2024-01-02", "--backfill-end", "2026-09-14",
    ])

    assert run._parse_args().backfill_trust_holding is True
