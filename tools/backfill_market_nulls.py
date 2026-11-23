"""一次性：補 market_daily 裡還是 NULL 的欄位（只補 NULL，不覆蓋、不 INSERT）。

處理三組欄位，各自的來源：

- `day-trade`（day_trade_* 6 欄）：TWSE TWTB4U，每天 1 次請求（沿用 fetch_twse_day_trade）。
  2014-01-06 起都有資料；舊日期抓到的已是 T+2 後的最終值。
- `txf`（txf_* 8 欄）：FinMind `TaiwanFuturesInstitutionalInvestors`（data_id=TX），整段 1 次請求。
  期交所伺服器端只給近三年（「無法查詢 2023/11/23 日以前資料」），更早的只能靠第三方；
  2026-11-23 與 DEV 重疊的 272 天、2,176 格逐格比對完全一致。
- `foreign-net`（foreign_net）：TWSE BFI82U，每天 1 次請求（沿用 fetch_twse_foreign_net）。

**只補 NULL 的格子**：每列只帶 DB 目前是 NULL 的欄位進 UPDATE（其餘欄缺鍵 → COALESCE 保留），
既有值一律不動。也不 INSERT（理由同 `--backfill-futures-oi`）。

**TWSE 限流**：限流回應與「沒資料」同字串，但要補的日期在 market_daily 已有列 = 確定是交易日，
所以連續 3 次抓不到就當成被限流、停下來。每 20 天寫入一次；中斷後重跑只會補剩下的 NULL。

預設只模擬（TWSE 逐日組不發請求，只印計畫）；加 `--apply` 才抓取並寫入。目標 DB 取環境變數
`DATABASE_URL`（沒有才讀 .env），執行前先印出 host。

    python tools/backfill_market_nulls.py
    python tools/backfill_market_nulls.py --only txf --apply
    python tools/backfill_market_nulls.py --only day-trade --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import requests
from dotenv import load_dotenv

from tw_stock_rawdata.db import close_pool
from tw_stock_rawdata.db_utils import (
    _DAY_TRADE_COL_TYPES,
    _FUTURES_OI_COLS,
    update_market_day_trade_batch,
    update_market_futures_oi_batch,
    upsert_market_daily,
)
from tw_stock_rawdata.sources import (
    DataUnavailableError,
    build_session,
    fetch_twse_day_trade,
    fetch_twse_foreign_net,
)

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_FINMIND_INVESTORS = {"外資": "foreign", "投信": "trust", "自營商": "dealer"}

FLUSH_EVERY = 20
MAX_CONSECUTIVE_FAILURES = 3

DAY_TRADE_COLS = list(_DAY_TRADE_COL_TYPES)
TXF_COLS = list(_FUTURES_OI_COLS)
FOREIGN_NET_COLS = ["foreign_net"]
HANDLED = {*DAY_TRADE_COLS, *TXF_COLS, *FOREIGN_NET_COLS}
# 不由這支處理的欄位，NULL 時提示該走哪裡。
_UNHANDLED_HINT = {
    "tw_vix_close": "用 tools/backfill_vix_history.py",
    "margin_balance_change": "DB 第一天沒有前一交易日時是刻意的 NULL（correct_prev_margin_balance）",
}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------


def load_rows(database_url: str, start: dt.date, end: dt.date) -> dict[dt.date, dict]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM market_daily WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )
        names = [d.name for d in cur.description or []]
        return {row[0]: dict(zip(names, row)) for row in cur.fetchall()}


def null_cells(rows: dict[dt.date, dict], cols: list[str]) -> dict[dt.date, list[str]]:
    """每個日期還是 NULL 的欄位（只列有缺的日期）。"""
    out = {d: [c for c in cols if row[c] is None] for d, row in rows.items()}
    return {d: cs for d, cs in out.items() if cs}


def only_null(values: dict, wanted: list[str]) -> dict:
    """只留 DB 是 NULL、來源有值的欄位；其餘不進 UPDATE（缺鍵 → COALESCE 保留舊值）。"""
    return {c: values[c] for c in wanted if values.get(c) is not None}


def _span(dates: list[dt.date]) -> str:
    return f"{dates[0]} ~ {dates[-1]}" if dates else "-"


def _fmt(dates: list[dt.date], limit: int = 10) -> str:
    shown = ", ".join(d.isoformat() for d in dates[:limit])
    return shown + (f" …（共 {len(dates)} 天）" if len(dates) > limit else "")


# ---------------------------------------------------------------------------
# txf：FinMind 整段 1 次
# ---------------------------------------------------------------------------


def fetch_finmind_txf(start: dt.date, end: dt.date) -> dict[dt.date, dict]:
    """FinMind 臺股期貨（TX）三大法人未平倉 → market_daily txf_* 欄（與 TAIFEX CSV 同口徑）。

    口數淨額 = 多方未平倉口數 − 空方未平倉口數；金額淨額 = (多 − 空) 契約金額（千元）× 1000。
    合計欄只在三個身份別都在時才算，缺的不當 0 加（同 prepare 的規則）。
    """
    resp = requests.get(
        FINMIND_URL,
        params={
            "dataset": "TaiwanFuturesInstitutionalInvestors",
            "data_id": "TX",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        timeout=60,
        headers={"User-Agent": "tw-stock-rawdata/0.1"},
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"FinMind 回應異常：{payload.get('msg')!r}")

    out: dict[dt.date, dict] = {}
    for row in payload["data"]:
        who = _FINMIND_INVESTORS.get(row["institutional_investors"])
        if who is None:
            raise RuntimeError(f"FinMind 身份別認不得：{row['institutional_investors']!r}")
        day = out.setdefault(dt.date.fromisoformat(row["date"]), {})
        day[f"txf_{who}_oi_net"] = (
            row["long_open_interest_balance_volume"] - row["short_open_interest_balance_volume"]
        )
        day[f"txf_{who}_oi_net_amount"] = (
            row["long_open_interest_balance_amount"] - row["short_open_interest_balance_amount"]
        ) * 1000
    for day in out.values():
        if all(f"txf_{w}_oi_net" in day for w in _FINMIND_INVESTORS.values()):
            for suffix in ("", "_amount"):
                day[f"txf_total_oi_net{suffix}"] = sum(
                    day[f"txf_{w}_oi_net{suffix}"] for w in _FINMIND_INVESTORS.values()
                )
    return out


def run_txf(database_url: str, rows: dict[dt.date, dict], apply: bool) -> None:
    todo = null_cells(rows, TXF_COLS)
    dates = sorted(todo)
    print(f"[txf] NULL：{len(dates)} 天（{_span(dates)}）；來源 FinMind 1 次請求")
    if not dates:
        return
    src = fetch_finmind_txf(dates[0], dates[-1])
    updates = [(d, only_null(src[d], todo[d])) for d in dates if d in src]
    updates = [(d, v) for d, v in updates if v]
    missing = [d for d in dates if d not in src]
    partial = [d for d, v in updates if len(v) != len(todo[d])]
    print(f"  可補 {len(updates)} 天")
    if missing:
        print(f"  ⚠ FinMind 沒有（未回補）：{_fmt(missing)}")
    if partial:
        print(f"  ⚠ 只補到部分欄位（身份別缺席）：{_fmt(partial)}")
    if apply:
        print(f"  已寫入 {update_market_futures_oi_batch(database_url, updates)} 列")


# ---------------------------------------------------------------------------
# TWSE 逐日：day-trade / foreign-net
# ---------------------------------------------------------------------------


def run_twse_daily(
    name: str,
    cols: list[str],
    fetch: Callable[[requests.Session, dt.date], dict],
    write: Callable[[list[tuple[dt.date, dict]]], int],
    rows: dict[dt.date, dict],
    apply: bool,
    min_interval: float,
) -> None:
    todo = null_cells(rows, cols)
    dates = sorted(todo)
    eta = len(dates) * (min_interval + 0.3) / 60
    print(f"[{name}] NULL：{len(dates)} 天（{_span(dates)}）；TWSE 每天 1 次，約 {eta:.0f} 分鐘")
    if not dates or not apply:
        return

    session = build_session(min_interval=min_interval)
    pending: list[tuple[dt.date, dict]] = []
    written = 0
    failures: list[tuple[dt.date, str]] = []
    consecutive = 0
    t0 = time.monotonic()

    def flush() -> None:
        nonlocal written
        if pending:
            written += write(list(pending))
            pending.clear()

    try:
        for i, d in enumerate(dates, 1):
            try:
                values = only_null(fetch(session, d), todo[d])
            except (DataUnavailableError, requests.RequestException) as exc:
                failures.append((d, str(exc)))
                consecutive += 1
                print(f"  ⚠ {d} 抓不到：{exc}")
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    print(f"  ✋ 連續 {consecutive} 次抓不到，疑似 TWSE 限流，停止；稍後重跑會接著補")
                    break
                continue
            consecutive = 0
            if values:
                pending.append((d, values))
            if len(pending) >= FLUSH_EVERY:
                flush()
            if i % 50 == 0:
                print(f"  {i}/{len(dates)}（{d}）已寫入 {written} 列，{time.monotonic() - t0:.0f}s")
    finally:
        flush()  # Ctrl-C 或例外也先把抓到的寫進去
    print(f"  完成：寫入 {written} 列，失敗 {len(failures)} 天，{time.monotonic() - t0:.0f}s")
    if failures:
        print(f"  失敗日期：{_fmt([d for d, _ in failures])}")


def _fetch_foreign_net(session: requests.Session, d: dt.date) -> dict:
    return {"foreign_net": fetch_twse_foreign_net(session, d)}


def _make_foreign_net_writer(database_url: str) -> Callable[[list[tuple[dt.date, dict]]], int]:
    def write(updates: list[tuple[dt.date, dict]]) -> int:
        # 只對 market_daily 已有的列呼叫，ON CONFLICT 走 COALESCE 更新、不會新增列。
        for d, values in updates:
            upsert_market_daily(database_url, d, values)
        return len(updates)
    return write


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def report_unhandled(rows: dict[dt.date, dict]) -> None:
    cols = [c for c in next(iter(rows.values())) if c not in HANDLED
            and c not in ("trade_date", "created_time", "updated_time")]
    for c in cols:
        dates = sorted(d for d, row in rows.items() if row[c] is None)
        if dates:
            hint = _UNHANDLED_HINT.get(c, "此 script 不處理")
            print(f"ℹ {c} 有 {len(dates)} 天 NULL（{_fmt(dates, 5)}）— {hint}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一次性：補 market_daily 還是 NULL 的欄位（只補 NULL）"
    )
    parser.add_argument("--start", type=dt.date.fromisoformat, help="預設 market_daily 最早日")
    parser.add_argument("--end", type=dt.date.fromisoformat, help="預設 market_daily 最晚日")
    parser.add_argument("--only", action="append", choices=["day-trade", "txf", "foreign-net"],
                        help="只跑指定組（可重複）；預設全跑")
    parser.add_argument("--min-interval", type=float, default=3.0,
                        help="TWSE 請求最小間隔秒數（預設 3）")
    parser.add_argument("--apply", action="store_true", help="真的抓取並寫入（預設只模擬）")
    args = parser.parse_args()
    groups = args.only or ["foreign-net", "txf", "day-trade"]

    # 已 export 的 DATABASE_URL 優先（load_dotenv 預設不覆寫既有環境變數）。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("沒有 DATABASE_URL")
    target = urlparse(database_url)
    print(f"目標 DB：{target.hostname}:{target.port}{target.path}")

    rows = load_rows(database_url, args.start or dt.date.min, args.end or dt.date.max)
    if not rows:
        print("market_daily 在指定區間無資料，略過")
        return
    dates = sorted(rows)
    print(f"market_daily {len(rows)} 列（{_span(dates)}）；模式：{'寫入' if args.apply else '模擬'}")
    report_unhandled(rows)

    try:
        if "foreign-net" in groups:
            run_twse_daily("foreign-net", FOREIGN_NET_COLS, _fetch_foreign_net,
                           _make_foreign_net_writer(database_url),
                           rows, args.apply, args.min_interval)
        if "txf" in groups:
            run_txf(database_url, rows, args.apply)
        if "day-trade" in groups:
            run_twse_daily("day-trade", DAY_TRADE_COLS, fetch_twse_day_trade,
                           lambda u: update_market_day_trade_batch(database_url, u),
                           rows, args.apply, args.min_interval)
    finally:
        close_pool()
    if not args.apply:
        print("模擬結束，未寫入；確認目標 DB 後加 --apply")


if __name__ == "__main__":
    main()
