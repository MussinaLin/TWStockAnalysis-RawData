"""一次性：從期交所「指數專區」回補 market_daily.tw_vix_close 的歷史值。

期交所月檔只保留近 4 個月（`--backfill-vix`），更早的 VIX 用指數專區圖表背後的端點補：
`POST https://www.taifex.com.tw/indes/index.aspx/GetStockDayPrices`，1 次請求回整段日收盤值。
網頁寫「可查詢最近三年」只是日期選擇器的限制，端點實測可回 2022 年。

**只補 NULL，不覆蓋**：指數專區偶爾沿用前一天的值（2026-08-14 回 31.09 = 08-13 的值，
月檔實際是 30.22），可信度低於月檔／當日檔。所以只 UPDATE `tw_vix_close IS NULL` 的列，
既有值（daily 當日檔、`--backfill-vix` 月檔）一律不動；也不 INSERT（理由同 `--backfill-vix`）。

預設只模擬、不寫入；加 `--apply` 才寫。目標 DB 取環境變數 `DATABASE_URL`（沒有才讀 .env），
執行前先印出 host，確認 DEV／PROD 再加 `--apply`。

    python tools/backfill_vix_history.py --start 2022-01-01 --end 2026-07-31
    python tools/backfill_vix_history.py --start 2022-01-01 --end 2026-07-31 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import requests
from dotenv import load_dotenv

from tw_stock_rawdata.db import close_pool
from tw_stock_rawdata.db_utils import update_market_vix_batch

INDEX_ZONE_URL = "https://www.taifex.com.tw/indes/index.aspx/GetStockDayPrices"
_CENT = Decimal("0.01")


def fetch_index_zone_vix(start: dt.date, end: dt.date) -> dict[dt.date, Decimal]:
    """指數專區的台指 VIX 日收盤值（13:45，與月檔「臺指選擇權波動率指數」欄同口徑）。"""
    body = {
        "syid": "TAIWANVIX",
        "flag": "MS",
        "startDate": start.strftime("%Y/%m/%d"),
        "endDate": end.strftime("%Y/%m/%d"),
    }
    resp = requests.post(
        INDEX_ZONE_URL, json=body, timeout=60, headers={"User-Agent": "tw-stock-rawdata/0.1"}
    )
    resp.raise_for_status()
    payload = json.loads(resp.json()["d"])  # d 是再包一層的 JSON 字串
    echoed = (payload.get("startDate"), payload.get("endDate"))
    if echoed != (start.strftime("%Y%m%d"), end.strftime("%Y%m%d")):
        raise RuntimeError(f"指數專區回應的區間 {echoed} 不是請求的 {start}~{end}")

    out: dict[dt.date, Decimal] = {}
    for row in payload["TrendData"]:
        price = Decimal(str(row["Price"])).quantize(_CENT)
        if price <= 0:
            raise RuntimeError(f"指數專區 {row['Time']} 的值不合理：{row['Price']}")
        out[dt.datetime.strptime(row["Time"], "%Y%m%d").date()] = price
    return out


def load_market_vix(database_url: str, start: dt.date, end: dt.date) -> dict[dt.date, Decimal | None]:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, tw_vix_close FROM market_daily"
            " WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (start, end),
        )
        return dict(cur.fetchall())


def _fmt(dates: list[dt.date], limit: int = 10) -> str:
    shown = ", ".join(d.isoformat() for d in dates[:limit])
    return shown + (f" …（共 {len(dates)} 天）" if len(dates) > limit else "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一次性：從期交所指數專區回補 market_daily.tw_vix_close（只補 NULL）"
    )
    parser.add_argument("--start", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--end", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--apply", action="store_true", help="真的寫入（預設只模擬）")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start 不可晚於 --end")

    # 已 export 的 DATABASE_URL 優先（load_dotenv 預設不覆寫既有環境變數）。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("沒有 DATABASE_URL")
    target = urlparse(database_url)
    print(f"目標 DB：{target.hostname}:{target.port}{target.path}")

    db = load_market_vix(database_url, args.start, args.end)
    if not db:
        print(f"market_daily 在 {args.start} ~ {args.end} 無資料，略過")
        return
    src = fetch_index_zone_vix(args.start, args.end)
    print(f"market_daily {len(db)} 列；指數專區 {len(src)} 天（1 次請求）")

    null_dates = [d for d, v in db.items() if v is None]
    updates = [(d, {"tw_vix_close": src[d]}) for d in null_dates if d in src]
    missing = [d for d in null_dates if d not in src]
    differs = [d for d, v in db.items() if v is not None and d in src and src[d] != v]
    not_in_db = sorted(set(src) - set(db))
    src_days = sorted(src)
    repeats = [b for a, b in zip(src_days, src_days[1:]) if src[a] == src[b] and db.get(b) is None]

    print(f"將寫入（目前 NULL）：{len(updates)} 天" + (f"，{updates[0][0]} ~ {updates[-1][0]}" if updates else ""))
    if repeats:
        print(f"  ⚠ 其中與前一交易日同值、無法查證：{_fmt(repeats)}")
    if differs:
        print("ℹ 已有值、與指數專區不同（不覆蓋）：" + ", ".join(
            f"{d}（DB {db[d]} / 指數專區 {src[d]}）" for d in differs))
    if missing:
        print(f"⚠ market_daily NULL、指數專區沒有（未回補）：{_fmt(missing)}")
    if not_in_db:
        print(f"ℹ 指數專區有、market_daily 沒有（不 INSERT）：{_fmt(not_in_db)}")

    if not args.apply:
        print("模擬結束，未寫入；確認目標 DB 後加 --apply")
        return
    try:
        n = update_market_vix_batch(database_url, updates)
    finally:
        close_pool()  # 寫入走 db.get_pool 的連線池，不關會卡 5 秒並印 worker 警告
    print(f"已寫入 {n} 列")


if __name__ == "__main__":
    main()
