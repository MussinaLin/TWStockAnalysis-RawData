"""一次性：從期交所回補 market_daily 的小台／微台未平倉 6 欄（mtx_* / tmf_*）。

daily 只寫當天；歷史用這支補：
- 三大法人多／空單：`futContractsDateDown`，每個商品整段 1 次請求。只保留近三年，
  所以小台從今天往前 3 年起；微台從上市日 2024-07-29 起。
- 全市場未平倉：`futDataDown`，一次最多查一個月，按日曆月切段，每段 1 次請求
  （三年約 36 次）。某段失敗印出該段、繼續下一段；重跑即可補上。

**只 UPDATE 已存在的列、不 INSERT**（只有期貨欄的列會被 find_consensus_prev_trade_date 當成 D-1）。
新值 NULL 保留舊值；非 NULL 直接覆寫（歷史值是最終值，覆寫 daily 寫過的同值無害）。

預設只模擬、不寫入；加 `--apply` 才寫。目標 DB 取環境變數 `DATABASE_URL`（沒有才讀 .env），
執行前先印出 host，確認 DEV／PROD 再加 `--apply`。法人回「日期時間錯誤」（近三年的邊界比預期晚）
時該商品整個不補，用 `--start` 指定較晚的起日重跑。

    python tools/backfill_mini_futures_oi.py
    python tools/backfill_mini_futures_oi.py --apply
    python tools/backfill_mini_futures_oi.py --start 2023-12-01 --apply
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv

from tw_stock_rawdata.db import close_pool
from tw_stock_rawdata.db_utils import (
    _MINI_FUTURES_OI_COLS,
    load_market_dates_in_range,
    update_market_mini_futures_oi_batch,
)
from tw_stock_rawdata.prepare import prepare_taifex_inst_long_short, prepare_taifex_market_oi
from tw_stock_rawdata.sources import (
    MINI_FUTURES,
    DataUnavailableError,
    build_session,
    fetch_taifex_fut_daily_report,
    fetch_taifex_fut_institutional,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TMF_LISTING_DATE = dt.date(2024, 7, 29)
_EARLIEST = dt.date(1990, 1, 1)


def month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """把 [start, end] 按日曆月切段（首尾截到區間內），每段不超過期交所行情的一個月上限。"""
    chunks: list[tuple[dt.date, dt.date]] = []
    lo = start
    while lo <= end:
        month_end = lo.replace(day=calendar.monthrange(lo.year, lo.month)[1])
        hi = min(month_end, end)
        chunks.append((lo, hi))
        lo = hi + dt.timedelta(days=1)
    return chunks


def floor_for(prefix: str, today: dt.date) -> dt.date:
    """各商品可回補的最早日：微台 = 上市日；小台 = 法人端點的近三年（今天往前 3 年，2/29 → 2/28）。"""
    if prefix == "tmf":
        return TMF_LISTING_DATE
    try:
        return today.replace(year=today.year - 3)
    except ValueError:
        return today.replace(year=today.year - 3, day=28)


def merge_updates(
    db_dates: list[dt.date], *maps: dict[dt.date, dict]
) -> tuple[list[tuple[dt.date, dict]], list[dt.date]]:
    """合併各來源的 {date: 欄位}；只保留 db_dates 內的日期（不 INSERT）。

    Returns:
        (updates：依日期排序、同日多個來源合併；extra：期交所有、DB 沒有的日期)
    """
    on_db = set(db_dates)
    merged: dict[dt.date, dict] = {}
    extra: set[dt.date] = set()
    for source in maps:
        for date, data in source.items():
            if date in on_db:
                merged.setdefault(date, {}).update(data)
            else:
                extra.add(date)
    return sorted(merged.items()), sorted(extra)


def load_missing_mini_columns(database_url: str) -> list[str]:
    """market_daily 還沒有的小台／微台欄位（DB 還沒跑過新版 init_schema 時非空）。"""
    with psycopg.connect(database_url) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'market_daily'"
        ).fetchall()
    existing = {r[0] for r in rows}
    return [c for c in _MINI_FUTURES_OI_COLS if c not in existing]


def _fmt(dates: list[dt.date], limit: int = 10) -> str:
    shown = "、".join(map(str, dates[:limit]))
    return shown + (f" …（共 {len(dates)} 天）" if len(dates) > limit else "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一次性：從期交所回補 market_daily 的小台／微台未平倉 6 欄（只 UPDATE 已有列）"
    )
    parser.add_argument("--apply", action="store_true", help="真的寫入（預設只模擬）")
    parser.add_argument(
        "--start", type=dt.date.fromisoformat, default=None,
        help="覆蓋起日（YYYY-MM-DD）；實際起日仍不早於各商品可回補的最早日",
    )
    args = parser.parse_args()

    # 已 export 的 DATABASE_URL 優先（load_dotenv 預設不覆寫既有環境變數）。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("沒有 DATABASE_URL")
    target = urlparse(database_url)
    print(f"目標 DB：{target.hostname}:{target.port}{target.path}")

    # 欄位由 tw-stock-rawdata 的 init_schema 建立；這支不建欄。先檢查，免得模擬顯示可寫、
    # --apply 打完幾十次期交所請求後才因欄位不存在失敗。
    missing = load_missing_mini_columns(database_url)
    if missing:
        raise SystemExit(
            f"market_daily 缺少欄位 {'、'.join(missing)}：先在這個 DB 跑一次新版 tw-stock-rawdata"
            "（啟動時的 init_schema 會建欄）再執行"
        )

    today = dt.datetime.now(TAIPEI_TZ).date()
    try:
        db_dates = sorted(load_market_dates_in_range(database_url, _EARLIEST, today))
        if not db_dates:
            print("market_daily 沒有任何列，略過")
            return
        session = build_session()

        all_updates: dict[dt.date, dict] = {}
        failures: list[str] = []
        for fut in MINI_FUTURES:
            start = max(floor_for(fut.prefix, today), db_dates[0], args.start or _EARLIEST)
            end = db_dates[-1]
            in_range = [d for d in db_dates if start <= d <= end]
            if not in_range:
                print(f"{fut.label}：{start} ~ {end} market_daily 無資料，略過")
                continue
            chunks = month_chunks(start, end)
            print(
                f"{fut.label}：{start} ~ {end}（market_daily {len(in_range)} 天；"
                f"法人 1 次、行情 {len(chunks)} 次請求）"
            )

            try:
                inst = prepare_taifex_inst_long_short(
                    fetch_taifex_fut_institutional(session, start, end, fut.inst_id),
                    fut.product, fut.prefix,
                )
            except (DataUnavailableError, requests.RequestException) as exc:
                failures.append(f"{fut.label} 法人 {start} ~ {end}")
                print(f"  ⚠ {fut.label}三大法人取得失敗，此商品不補：{exc}")
                continue

            mkt: dict[dt.date, dict] = {}
            for lo, hi in chunks:
                try:
                    mkt.update(prepare_taifex_market_oi(
                        fetch_taifex_fut_daily_report(session, fut.report_id, lo, hi),
                        fut.report_id, fut.prefix,
                    ))
                except (DataUnavailableError, requests.RequestException) as exc:
                    failures.append(f"{fut.label} 行情 {lo} ~ {hi}")
                    print(f"  ⚠ {fut.label}行情 {lo} ~ {hi} 取得失敗：{exc}")

            updates, extra = merge_updates(in_range, inst, mkt)
            print(f"  將寫入 {len(updates)} 天")
            no_inst = [d for d in in_range if d not in inst]
            no_mkt = [d for d in in_range if d not in mkt]
            if no_inst:
                print(f"  ⚠ market_daily 有、期交所法人沒有：{_fmt(no_inst)}")
            if no_mkt:
                print(f"  ⚠ market_daily 有、期交所行情沒有：{_fmt(no_mkt)}")
            if extra:
                print(f"  ℹ 期交所有、market_daily 沒有，未寫入（不新增列）：{_fmt(extra)}")
            for date, data in updates:
                all_updates.setdefault(date, {}).update(data)

        merged = sorted(all_updates.items())
        print(f"合計將更新 {len(merged)} 列；失敗 {len(failures)} 段" + (f"（{'、'.join(failures)}）" if failures else ""))
        if not args.apply:
            print("模擬結束，未寫入；確認目標 DB 後加 --apply")
            return
        n = update_market_mini_futures_oi_batch(database_url, merged)
        print(f"已更新 {n} 列")
    finally:
        close_pool()  # load_market_dates_in_range / update_* 走 db.get_pool 的連線池


if __name__ == "__main__":
    main()
