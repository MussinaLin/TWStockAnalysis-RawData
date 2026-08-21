"""Unit tests for 處置股（disposition）擷取、解析與寫入接縫。

鎖住的不變量：
1. `prepare_disposition` 同時吃 TWSE（全形「～」＋國字分鐘）與 TPEX（半形「~」＋
   阿拉伯數字），並丟掉 TPEX 的「本日無處置資料」佔位列。
2. `DispositionData.resolve` 的三態語意：處置中 / 已確認非處置 / 名單未取得。
   非處置寫 (False, 0) 而不是 (False, None) —— upsert 的 COALESCE 不以 NULL 覆寫
   舊值，寫 None 會讓前一段處置的分鐘數永遠殘留。
3. `_fetch_disposition` 會把公告展開到期間內每一天、clamp 在查詢區間內，
   且單一市場失敗不影響另一市場。
4. `_build_daily_rows` 真的把兩個欄位放進輸出 DataFrame，且餵進
   `_build_raw_rows` 後落在參數列表正確位置。
"""

from __future__ import annotations

import argparse
import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from tw_stock_rawdata import db_utils, run
from tw_stock_rawdata.prepare import _cn_to_int, prepare_disposition
from tw_stock_rawdata.sources import DataUnavailableError

DATE = dt.date(2026, 8, 12)

_EMPTY_WITH_SYMBOL = pd.DataFrame(columns=["symbol"])
_EMPTY_TPEX_QUOTES = pd.DataFrame(
    columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
)

# 取自真實回應的欄位順序。
_TWSE_FIELDS = [
    "編號", "公布日期", "證券代號", "證券名稱", "累計", "處置條件",
    "處置起迄時間", "處置措施", "處置內容", "備註",
]
_TPEX_FIELDS = [
    "編號", "公布日期", "證券代號", "證券名稱", "累計", "處置起訖時間",
    "處置原因", "處置內容", "收盤價", "本益比", " ",
]


def _twse_row(symbol: str, period: str, detail: str) -> list:
    return [1, "115/08/11", symbol, "測試股", 1, "連續三次", period, "第一次處置", detail, ""]


def _tpex_row(symbol: str, period: str, detail: str) -> list:
    # 真實回應的最後一欄是個空白欄名的空欄，長度要對齊 _TPEX_FIELDS。
    return [1, "115/08/11", symbol, "測試股", 1, period, "連續3個營業日", detail, "10.5", "-", ""]


# ---------------------------------------------------------------------------
# _cn_to_int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5), ("25", 25), ("２５", 25),          # 半形 / 全形阿拉伯數字
        ("二", 2), ("十", 10), ("二十", 20),          # 國字
        ("二十五", 25), ("四十五", 45), ("六十", 60),
        ("", None), ("甲", None), ("甲十", None), ("十甲", None),  # 無法解析
    ],
)
def test_cn_to_int(text: str, expected: int | None) -> None:
    assert _cn_to_int(text) == expected


# ---------------------------------------------------------------------------
# prepare_disposition
# ---------------------------------------------------------------------------


def test_prepare_disposition_twse_fullwidth_tilde_and_cn_numeral() -> None:
    """TWSE 用全形「～」分隔、國字寫撮合分鐘（「約每二十分鐘撮合一次」）。"""
    df = pd.DataFrame(
        [_twse_row("2330", "115/08/20～115/08/26", "３處置措施：\n約每二十分鐘撮合一次。")],
        columns=_TWSE_FIELDS,
    )
    out = prepare_disposition(df)

    assert len(out) == 1
    assert out.iloc[0]["symbol"] == "2330"
    assert out.iloc[0]["start_date"] == dt.date(2026, 8, 20)
    assert out.iloc[0]["end_date"] == dt.date(2026, 8, 26)
    assert out.iloc[0]["match_minutes"] == 20


def test_prepare_disposition_tpex_halfwidth_tilde_and_arabic_numeral() -> None:
    """TPEX 用半形「~」分隔、阿拉伯數字寫撮合分鐘。"""
    df = pd.DataFrame(
        [_tpex_row("5321", "115/06/30~115/07/13", "改以人工管制之撮合終端機執行撮合作業(約每5分鐘撮合一次)")],
        columns=_TPEX_FIELDS,
    )
    out = prepare_disposition(df)

    assert len(out) == 1
    assert out.iloc[0]["symbol"] == "5321"
    assert out.iloc[0]["start_date"] == dt.date(2026, 6, 30)
    assert out.iloc[0]["end_date"] == dt.date(2026, 7, 13)
    assert out.iloc[0]["match_minutes"] == 5


def test_prepare_disposition_drops_placeholder_and_bad_period() -> None:
    """TPEX 會夾「本日無處置資料」佔位列（代號空字串）；期間反序的列也要丟。"""
    df = pd.DataFrame(
        [
            _tpex_row("", "", "本日無處置資料"),
            _tpex_row("3490", "115/08/26~115/08/20", "約每5分鐘撮合一次"),   # end < start
            _tpex_row("5475", "看不懂的期間", "約每5分鐘撮合一次"),
            _tpex_row("8024", "115/08/20~115/08/26", "約每5分鐘撮合一次"),   # 唯一合法列
        ],
        columns=_TPEX_FIELDS,
    )
    out = prepare_disposition(df)

    assert list(out["symbol"]) == ["8024"]


def test_prepare_disposition_keeps_row_when_minutes_unparsable() -> None:
    """撮合頻率解析不到仍保留該列 —— 處置這件事本身仍然成立。"""
    df = pd.DataFrame(
        [_twse_row("2330", "115/08/20～115/08/26", "處置措施：人工管制撮合，未載明頻率。")],
        columns=_TWSE_FIELDS,
    )
    out = prepare_disposition(df)

    assert len(out) == 1
    assert out.iloc[0]["match_minutes"] is None


def test_prepare_disposition_takes_first_interval_when_detail_has_two() -> None:
    """可轉債公告會併同標的股票處置、夾兩段撮合描述，取第一個＝主區間頻率。"""
    detail = (
        "自115年6月16日起10個營業日改以人工管制之撮合終端機執行撮合作業(約每5分鐘撮合一次)，"
        "惟其標的股票前經本中心發布處置...仍應改以人工管制之撮合終端機執行撮合作業(約每20分鐘撮合一次)"
    )
    df = pd.DataFrame(
        [_tpex_row("49793", "115/06/16~115/06/30", detail)], columns=_TPEX_FIELDS
    )
    out = prepare_disposition(df)

    assert out.iloc[0]["match_minutes"] == 5


def test_prepare_disposition_accepts_compact_roc_period() -> None:
    """OpenAPI 快照版給的是 `1150821` 緊湊民國日期，也要能吃。"""
    df = pd.DataFrame(
        [_tpex_row("3490", "1150820~1150826", "約每5分鐘撮合一次")], columns=_TPEX_FIELDS
    )
    out = prepare_disposition(df)

    assert out.iloc[0]["start_date"] == dt.date(2026, 8, 20)
    assert out.iloc[0]["end_date"] == dt.date(2026, 8, 26)


def test_prepare_disposition_empty_and_missing_columns() -> None:
    assert prepare_disposition(pd.DataFrame()).empty
    with pytest.raises(DataUnavailableError):
        prepare_disposition(pd.DataFrame([{"foo": 1}]))


# ---------------------------------------------------------------------------
# DispositionData.resolve
# ---------------------------------------------------------------------------


def _data(by_date=None, ok=("twse", "tpex")) -> run.DispositionData:
    return run.DispositionData(by_date or {}, frozenset(ok))


def test_resolve_in_disposition_returns_true_and_minutes() -> None:
    data = _data({DATE: {"2330": 20}})
    assert data.resolve(DATE, "2330", "twse") == (True, 20)


def test_resolve_in_disposition_with_unknown_minutes_writes_zero() -> None:
    """分鐘數解析不到時寫 0 而非 NULL：COALESCE 會讓 NULL 保留前一段處置的殘值。"""
    data = _data({DATE: {"2330": None}})
    assert data.resolve(DATE, "2330", "twse") == (True, 0)


def test_resolve_not_in_list_returns_false_zero() -> None:
    """已成功取得該市場名單、此檔不在名單 → 明確寫 (False, 0)。"""
    assert _data().resolve(DATE, "2330", "twse") == (False, 0)


def test_resolve_returns_none_when_that_market_failed() -> None:
    """該市場名單沒抓到就不能宣稱「非處置」，回 (None, None) 讓 COALESCE 保留舊值。"""
    data = _data(ok=("tpex",))
    assert data.resolve(DATE, "2330", "twse") == (None, None)
    assert data.resolve(DATE, "5321", "tpex") == (False, 0)


def test_resolve_unknown_market_type_needs_both_markets_ok() -> None:
    """market_type 未知（--backfill-stocks 查不到）時，只有兩市場都成功才敢寫 FALSE。"""
    assert _data().resolve(DATE, "2330", None) == (False, 0)
    assert _data(ok=("twse",)).resolve(DATE, "2330", None) == (None, None)


def test_resolve_all_markets_failed_returns_none() -> None:
    assert _data(ok=()).resolve(DATE, "2330", "twse") == (None, None)


# ---------------------------------------------------------------------------
# _fetch_disposition
# ---------------------------------------------------------------------------


def _patch_fetchers(monkeypatch, twse=None, tpex=None, calls=None):
    def make(fields, rows, exc):
        def fetcher(_session, start, end):
            if calls is not None:
                calls.append((start, end))
            if exc is not None:
                raise exc
            return pd.DataFrame(rows, columns=fields)
        return fetcher

    monkeypatch.setattr(
        run, "fetch_twse_disposition",
        make(_TWSE_FIELDS, *(twse if twse else ([], None))),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_disposition",
        make(_TPEX_FIELDS, *(tpex if tpex else ([], None))),
    )


def test_fetch_disposition_expands_period_into_days(monkeypatch) -> None:
    """公告是一段期間，要展開成期間內每一天都算處置中。"""
    _patch_fetchers(
        monkeypatch,
        twse=([_twse_row("2330", "115/08/20～115/08/24", "約每二十分鐘撮合一次")], None),
    )
    data = run._fetch_disposition(None, dt.date(2026, 8, 19), dt.date(2026, 8, 25))

    assert data.ok_markets == frozenset({"twse", "tpex"})
    assert sorted(data.by_date) == [dt.date(2026, 8, d) for d in range(20, 25)]
    for day in data.by_date.values():
        assert day == {"2330": 20}
    # 期間外的日子不該出現
    assert dt.date(2026, 8, 19) not in data.by_date
    assert dt.date(2026, 8, 25) not in data.by_date


def test_fetch_disposition_clamps_to_query_range(monkeypatch) -> None:
    """公告期間超出查詢區間時只保留區間內的日期，避免展開出無用的大 dict。"""
    _patch_fetchers(
        monkeypatch,
        twse=([_twse_row("2330", "115/08/01～115/08/31", "約每五分鐘撮合一次")], None),
    )
    data = run._fetch_disposition(None, dt.date(2026, 8, 10), dt.date(2026, 8, 12))

    assert sorted(data.by_date) == [dt.date(2026, 8, d) for d in (10, 11, 12)]


def test_fetch_disposition_looks_back_before_start(monkeypatch) -> None:
    """查詢窗口必須往前推：處置期間最長 10 個營業日，公告日又早於期間起日，
    只查當日會漏掉正處在處置期間中段的個股。"""
    calls: list[tuple[dt.date, dt.date]] = []
    _patch_fetchers(monkeypatch, calls=calls)
    run._fetch_disposition(None, DATE, DATE)

    assert len(calls) == 2
    for start, end in calls:
        assert end == DATE
        assert start == DATE - dt.timedelta(days=run._DISPOSITION_LOOKBACK_DAYS)


def test_fetch_disposition_takes_strictest_interval_on_overlap(monkeypatch) -> None:
    """同一檔同一天被多筆公告涵蓋時取最小分鐘數＝當日實際生效的最嚴格頻率。"""
    _patch_fetchers(
        monkeypatch,
        twse=(
            [
                _twse_row("2330", "115/08/20～115/08/26", "約每二十分鐘撮合一次"),
                _twse_row("2330", "115/08/20～115/08/26", "約每五分鐘撮合一次"),
            ],
            None,
        ),
    )
    data = run._fetch_disposition(None, dt.date(2026, 8, 20), dt.date(2026, 8, 20))

    assert data.by_date[dt.date(2026, 8, 20)] == {"2330": 5}


def test_fetch_disposition_one_market_failure_does_not_block_other(monkeypatch) -> None:
    """TWSE 掛掉不能拖累 TPEX；ok_markets 要誠實反映只有一邊成功。"""
    _patch_fetchers(
        monkeypatch,
        twse=([], requests.ConnectionError("boom")),
        tpex=([_tpex_row("5321", "115/08/20~115/08/20", "約每5分鐘撮合一次")], None),
    )
    data = run._fetch_disposition(None, dt.date(2026, 8, 20), dt.date(2026, 8, 20))

    assert data.ok_markets == frozenset({"tpex"})
    assert data.by_date[dt.date(2026, 8, 20)] == {"5321": 5}
    assert data.resolve(dt.date(2026, 8, 20), "2330", "twse") == (None, None)


def test_fetch_disposition_both_markets_failed(monkeypatch) -> None:
    _patch_fetchers(
        monkeypatch,
        twse=([], DataUnavailableError("no data")),
        tpex=([], DataUnavailableError("no data")),
    )
    data = run._fetch_disposition(None, DATE, DATE)

    assert data.ok_markets == frozenset()
    assert data.by_date == {}


# ---------------------------------------------------------------------------
# _build_daily_rows → _build_raw_rows 接縫
# ---------------------------------------------------------------------------


def _day_all_row(symbol: str, close: float) -> dict:
    return {
        "symbol": symbol, "name": "測試股", "open": close, "close": close,
        "high": close, "low": close, "volume": 1000,
    }


def test_build_daily_rows_places_disposition_correctly() -> None:
    """2330 處置中、3605 非處置、6182 所屬市場名單沒抓到 → 三態各走一條路。"""
    holdings = pd.DataFrame([
        {"symbol": "2330", "market_type": "twse"},
        {"symbol": "3605", "market_type": "twse"},
        {"symbol": "6182", "market_type": "tpex"},
    ])
    twse_day_all = pd.DataFrame([
        _day_all_row("2330", 2435.0), _day_all_row("3605", 120.0),
        _day_all_row("6182", 45.0),
    ])
    disposition = run.DispositionData({DATE: {"2330": 20}}, frozenset({"twse"}))

    result = run._build_daily_rows(
        session=None,
        date=DATE,
        holdings=holdings,
        twse_3insti=_EMPTY_WITH_SYMBOL,
        twse_day_all=twse_day_all,
        twse_mi_index=None,
        tpex_quotes=_EMPTY_TPEX_QUOTES,
        tpex_3insti=_EMPTY_WITH_SYMBOL,
        twse_month_cache={},
        disposition=disposition,
    )

    by_symbol = result.set_index("symbol")
    assert by_symbol.loc["2330", "is_disposition"] is True
    assert by_symbol.loc["2330", "disposition_match_minutes"] == 20
    assert by_symbol.loc["3605", "is_disposition"] is False
    assert by_symbol.loc["3605", "disposition_match_minutes"] == 0
    assert by_symbol.loc["6182", "is_disposition"] is None
    # match_minutes 混了 int 與 None，pandas 會把整欄轉 float64 → None 變 NaN。
    # 真正要緊的是餵進 _build_raw_rows 後 _safe() 把 NaN 收斂回 None（下方驗）。
    assert pd.isna(by_symbol.loc["6182", "disposition_match_minutes"])

    rows = db_utils._build_raw_rows(DATE, result)
    flag_idx = db_utils._RAW_COLUMNS.index("is_disposition")
    min_idx = db_utils._RAW_COLUMNS.index("disposition_match_minutes")
    by_symbol_row = {r[0]: r for r in rows}

    assert by_symbol_row["2330"][flag_idx] is True
    assert by_symbol_row["2330"][min_idx] == 20
    assert by_symbol_row["3605"][flag_idx] is False
    assert by_symbol_row["3605"][min_idx] == 0
    assert by_symbol_row["6182"][flag_idx] is None
    assert by_symbol_row["6182"][min_idx] is None


def test_build_daily_rows_without_disposition_writes_none() -> None:
    """沒帶處置資料時兩欄一律 None，交給 upsert 的 COALESCE 保留 DB 既有值。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])
    result = run._build_daily_rows(
        session=None,
        date=DATE,
        holdings=holdings,
        twse_3insti=_EMPTY_WITH_SYMBOL,
        twse_day_all=pd.DataFrame([_day_all_row("2330", 2435.0)]),
        twse_mi_index=None,
        tpex_quotes=_EMPTY_TPEX_QUOTES,
        tpex_3insti=_EMPTY_WITH_SYMBOL,
        twse_month_cache={},
    )

    assert result.iloc[0]["is_disposition"] is None
    assert result.iloc[0]["disposition_match_minutes"] is None


# ---------------------------------------------------------------------------
# db_utils.update_disposition_batch
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rowcounts: list[int]):
        self._rowcounts = list(rowcounts)
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.committed = True


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


def _patch_pool(monkeypatch, cursor: _FakeCursor) -> _FakeConn:
    conn = _FakeConn(cursor)
    monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))
    return conn


class TestUpdateDispositionBatch:
    def test_empty_updates_returns_zero(self) -> None:
        assert db_utils.update_disposition_batch("postgres://x", []) == 0

    def test_uses_update_not_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """必須是 UPDATE：走 upsert 會 INSERT 出其餘欄位全 NULL 的半套 row。"""
        cursor = _FakeCursor([1])
        _patch_pool(monkeypatch, cursor)

        db_utils.update_disposition_batch("postgres://x", [("2330", DATE, True, 20)])

        sql, params = cursor.executed[0]
        assert sql.startswith("UPDATE stock_daily_raw")
        assert "INSERT" not in sql
        assert params == ["2330", DATE, True, 20]

    def test_single_statement_regardless_of_row_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N 筆更新只能發一次 execute（逐列 execute 是一列一次網路往返）。"""
        cursor = _FakeCursor([500])
        _patch_pool(monkeypatch, cursor)

        updates = [(f"{i:04d}", DATE, False, 0) for i in range(500)]
        db_utils.update_disposition_batch("postgres://x", updates)

        assert len(cursor.executed) == 1
        assert len(cursor.executed[0][1]) == 500 * 4

    def test_chunks_to_stay_under_param_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cursor = _FakeCursor([1000, 500])
        _patch_pool(monkeypatch, cursor)

        n_rows = db_utils._BATCH_UPDATE_CHUNK + 500
        updates = [(f"{i:05d}", DATE, False, 0) for i in range(n_rows)]
        db_utils.update_disposition_batch("postgres://x", updates)

        assert len(cursor.executed) == 2
        assert len(cursor.executed[0][1]) == db_utils._BATCH_UPDATE_CHUNK * 4
        assert len(cursor.executed[1][1]) == 500 * 4

    def test_returns_total_rowcount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不存在的 (symbol, trade_date) 不計入 —— 由 UPDATE 的 rowcount 反映。"""
        cursor = _FakeCursor([2])
        conn = _patch_pool(monkeypatch, cursor)

        n = db_utils.update_disposition_batch(
            "postgres://x", [("2330", DATE, True, 5), ("9999", DATE, False, 0)]
        )

        assert n == 2
        assert conn.committed is True


# ---------------------------------------------------------------------------
# _backfill_disposition_command
# ---------------------------------------------------------------------------


def _bf_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        date=None,
        backfill_start="2026-08-20",
        backfill_end="2026-08-21",
        backfill_stocks=None,
        backfill_disposition=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_backfill(monkeypatch, disposition: run.DispositionData, existing: set[str]):
    """把指令會碰到的三個外部相依換掉，只留流程本身。"""
    captured: list[tuple] = []
    monkeypatch.setattr(run, "_fetch_disposition", lambda *a: disposition)
    monkeypatch.setattr(run, "load_symbols_for_date", lambda url, date: set(existing))
    monkeypatch.setattr(
        run, "load_market_types", lambda url: {"2330": "twse", "5321": "tpex"}
    )
    monkeypatch.setattr(
        run, "update_disposition_batch",
        lambda url, updates: (captured.append(tuple(updates)), len(updates))[1],
    )
    return captured


def test_backfill_disposition_writes_true_and_false(monkeypatch) -> None:
    """處置中寫 (True, N)、名單內沒有的寫 (False, 0)，週末不查。"""
    day = dt.date(2026, 8, 20)
    disposition = run.DispositionData({day: {"2330": 20}}, frozenset({"twse", "tpex"}))
    captured = _patch_backfill(monkeypatch, disposition, {"2330", "5321"})

    run._backfill_disposition_command(
        None, SimpleNamespace(database_url="postgres://x"), _bf_args()
    )

    # 2026-08-20(四) 與 08-21(五) 各一批
    assert len(captured) == 2
    assert captured[0] == (("2330", day, True, 20), ("5321", day, False, 0))
    next_day = dt.date(2026, 8, 21)
    assert captured[1] == (("2330", next_day, False, 0), ("5321", next_day, False, 0))


def test_backfill_disposition_skips_symbols_with_unknown_market(monkeypatch) -> None:
    """該市場名單沒抓到的個股整檔跳過，不能寫成「已確認非處置」。"""
    day = dt.date(2026, 8, 20)
    disposition = run.DispositionData({}, frozenset({"tpex"}))
    captured = _patch_backfill(monkeypatch, disposition, {"2330", "5321"})

    run._backfill_disposition_command(
        None, SimpleNamespace(database_url="postgres://x"), _bf_args()
    )

    # 只有上櫃的 5321 被寫入，上市的 2330 不在其中
    assert all(len(batch) == 1 and batch[0][0] == "5321" for batch in captured)
    assert captured[0][0] == ("5321", day, False, 0)


def test_backfill_disposition_honours_backfill_stocks(monkeypatch) -> None:
    """--backfill-stocks 要真的限縮範圍（這是與 --backfill-limits 的差別）。"""
    disposition = run.DispositionData({}, frozenset({"twse", "tpex"}))
    captured = _patch_backfill(monkeypatch, disposition, {"2330", "5321"})

    run._backfill_disposition_command(
        None,
        SimpleNamespace(database_url="postgres://x"),
        _bf_args(backfill_stocks="5321"),
    )

    assert all([u[0] for u in batch] == ["5321"] for batch in captured)


def test_backfill_disposition_aborts_when_both_markets_failed(monkeypatch) -> None:
    """兩市場都沒抓到就整個放棄，不可把全市場寫成非處置。"""
    captured = _patch_backfill(
        monkeypatch, run.DispositionData({}, frozenset()), {"2330"}
    )

    run._backfill_disposition_command(
        None, SimpleNamespace(database_url="postgres://x"), _bf_args()
    )

    assert captured == []
