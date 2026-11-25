"""一次性：從 e添富 ETF 商品頁回補 etf_daily.aum_100m 的近 12 個月月底規模。

MIS all_etf.txt 只有當下快照（daily 只能從上線那天開始累積），更早的規模用 e添富頁面
內嵌的「月規模」（`chartData.totalMonthAv`）補：tooltip 說明是「每個月最後一個營業日的
資產規模」，單位億元。所以每個月寫在 market_daily 該月最後一個 trade_date。

**只補 NULL，不覆蓋**：已有 aum_100m 的列（daily 寫的）一律不動，其餘欄位也不碰
（upsert_etf_daily 對缺鍵 COALESCE）。上市前的月份（值 <= 0）略過。
對象是 etfs 清單中啟用的 ETF，每檔 1 次 www.twse.com.tw 請求（build_session 限速）。
某檔頁面拿不到（限流時的空殼也是）→ 印出該檔失敗、繼續下一檔，不重試；之後重跑即可。

預設只模擬、不寫入；加 `--apply` 才寫。目標 DB 取環境變數 `DATABASE_URL`（沒有才讀 .env），
執行前先印出 host，確認 DEV／PROD 再加 `--apply`。

    python tools/backfill_etf_month_aum.py
    python tools/backfill_etf_month_aum.py --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv

from tw_stock_rawdata.db import close_pool
from tw_stock_rawdata.db_utils import get_enabled_etfs, upsert_etf_daily
from tw_stock_rawdata.prepare import resolve_month_labels
from tw_stock_rawdata.sources import DataUnavailableError, build_session, fetch_etf_month_aum

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def load_month_end_dates(database_url: str) -> dict[tuple[int, int], dt.date]:
    """market_daily 每個月最後一個 trade_date（= 該月最後一個營業日）。"""
    with psycopg.connect(database_url) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT max(trade_date) FROM market_daily"
            " GROUP BY date_trunc('month', trade_date)"
        ).fetchall()
    return {(d.year, d.month): d for (d,) in rows}


def load_filled(database_url: str) -> set[tuple[str, dt.date]]:
    """etf_daily 已有 aum_100m 的 (symbol, trade_date)。"""
    with psycopg.connect(database_url) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT symbol, trade_date FROM etf_daily WHERE aum_100m IS NOT NULL"
        ).fetchall()
    return {(r[0], r[1]) for r in rows}


def plan_symbol(
    symbol: str,
    months: list[tuple[str, Decimal]],
    today: dt.date,
    month_ends: dict[tuple[int, int], dt.date],
    filled: set[tuple[str, dt.date]],
) -> tuple[list[dict], list[str]]:
    """把一檔的月規模轉成要寫的列；回傳 (rows, 略過說明)。"""
    resolved = resolve_month_labels([label for label, _ in months], today)
    rows: list[dict] = []
    notes: list[str] = []
    for (year, month), (_, value) in zip(resolved, months):
        ym = f"{year}-{month:02d}"
        if value <= 0:
            notes.append(f"{ym} 上市前")
            continue
        trade_date = month_ends.get((year, month))
        if trade_date is None:
            notes.append(f"{ym} market_daily 無該月")
            continue
        if (symbol, trade_date) in filled:
            notes.append(f"{trade_date} 已有值")
            continue
        rows.append({"symbol": symbol, "trade_date": trade_date, "aum_100m": value})
    return rows, notes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一次性：從 e添富回補 etf_daily.aum_100m 近 12 個月月底規模（只補 NULL）"
    )
    parser.add_argument("--apply", action="store_true", help="真的寫入（預設只模擬）")
    args = parser.parse_args()

    # 已 export 的 DATABASE_URL 優先（load_dotenv 預設不覆寫既有環境變數）。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("沒有 DATABASE_URL")
    target = urlparse(database_url)
    print(f"目標 DB：{target.hostname}:{target.port}{target.path}")

    today = dt.datetime.now(TAIPEI_TZ).date()
    try:
        etfs = get_enabled_etfs(database_url)
        if not etfs:
            print("etfs 沒有啟用的 ETF，略過")
            return
        month_ends = load_month_end_dates(database_url)
        filled = load_filled(database_url)
        session = build_session()

        all_rows: list[dict] = []
        failed: list[str] = []
        for symbol, name in etfs:
            try:
                months = fetch_etf_month_aum(session, symbol)
                rows, notes = plan_symbol(symbol, months, today, month_ends, filled)
            except (DataUnavailableError, requests.RequestException, ValueError) as exc:
                failed.append(symbol)
                print(f"  ⚠ {symbol} {name} 失敗：{exc}")
                continue
            span = f"{rows[0]['trade_date']} ~ {rows[-1]['trade_date']}" if rows else "-"
            print(f"  {symbol} {name}：將寫入 {len(rows)} 個月（{span}）")
            if notes:
                print(f"    略過：{', '.join(notes)}")
            all_rows.extend(rows)

        print(f"合計將寫入 {len(all_rows)} 列；失敗 {len(failed)} 檔" + (f"（{', '.join(failed)}）" if failed else ""))
        if not args.apply:
            print("模擬結束，未寫入；確認目標 DB 後加 --apply")
            return
        n = upsert_etf_daily(database_url, all_rows)
        print(f"已寫入 {n} 列")
    finally:
        close_pool()  # get_enabled_etfs / upsert_etf_daily 走 db.get_pool 的連線池


if __name__ == "__main__":
    main()
