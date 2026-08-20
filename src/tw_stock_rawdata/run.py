"""Main entry point for TW Stock RawData fetcher."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import time
from decimal import Decimal
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import requests
from dotenv import load_dotenv

from .config import AppConfig
from .db import close_pool, get_pool, init_schema
from .db_utils import (
    correct_prev_margin_balance,
    find_consensus_prev_trade_date,
    get_config_value,
    get_enabled_stocks,
    load_market_types,
    load_stock_names,
    load_stock_shares,
    load_symbols_for_date,
    update_prev_day_margin_batch,
    update_price_limits_batch,
    upsert_daily_raw,
    upsert_holder_percent,
    upsert_market_daily,
    upsert_stock_shares,
)
from .prepare import (
    prepare_moneydj_holding_pct,
    prepare_moneydj_margin,
    prepare_tdcc_major_ratio,
    prepare_tdcc_retail_ratio,
    prepare_tpex_3insti,
    prepare_tpex_issued_shares,
    prepare_tpex_margin,
    prepare_tpex_margin_v2,
    prepare_tpex_quotes,
    prepare_twse_3insti,
    prepare_twse_day_all,
    prepare_twse_issued_shares,
    prepare_twse_margin,
    prepare_twse_mi_index,
)
from .price_limit import calc_limits
from .sources import (
    DataUnavailableError,
    build_session,
    fetch_moneydj_holding_pct,
    fetch_moneydj_margin,
    fetch_tdcc_distribution,
    fetch_tdcc_token_and_dates,
    fetch_tpex_3insti_v2,
    fetch_tpex_company_basic,
    fetch_tpex_daily_quotes_v2,
    fetch_tpex_margin,
    fetch_tpex_margin_v2,
    fetch_twse_company_basic,
    fetch_twse_foreign_net,
    fetch_twse_margin,
    fetch_twse_market_margin,
    fetch_twse_market_volume,
    fetch_twse_mi_index,
    fetch_twse_stock_day,
    fetch_twse_stock_day_all,
    fetch_twse_t86,
    fetch_twse_taiex_ohlc,
    find_twse_ohlcv,
)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


@contextlib.contextmanager
def _phase(label: str):
    """為一個執行階段印出起訖與耗時，用來歸因啟動到第一行進度之間的空白。

    每日流程在逐檔迴圈之前串了 DB 連線、schema、休市檢查與數組整批抓取，
    happy path 上全部不印任何東西；整批抓取又各自包在長窗口 retry 裡
    （RETRY_ATTEMPTS=6，backoff 上限約 56 秒），慢下來時無從得知慢在哪一段。

    進入時就印「開始」——階段若卡住不返回，至少定位得到是哪一段。
    例外原樣往外拋，只是順帶把耗時印出來（最貴的階段往往正是重試失敗那個）。
    輸出一律 flush：容器裡 stdout 是區塊緩衝，不 flush 就看不到即時進度。
    """
    print(f"[階段] {label} 開始", flush=True)
    started = time.monotonic()
    try:
        yield
    except BaseException as exc:
        print(
            f"[階段] {label} 失敗 {time.monotonic() - started:.1f}s"
            f"（{type(exc).__name__}）",
            flush=True,
        )
        raise
    else:
        print(f"[階段] {label} 完成 {time.monotonic() - started:.1f}s", flush=True)

# Cache for issued shares (doesn't change often)
_issued_shares_cache: dict[str, int] = {}


def _fetch_issued_shares_from_api(session: requests.Session) -> pd.DataFrame:
    """Fetch issued shares for all TWSE and TPEX stocks from API.

    Returns DataFrame with columns: symbol, name, issued_shares
    """
    import time

    frames: list[pd.DataFrame] = []

    # Fetch TWSE listed companies
    try:
        print("正在取得 TWSE 上市公司資料...", flush=True)
        t0 = time.monotonic()
        twse_basic = fetch_twse_company_basic(session)
        twse_shares = prepare_twse_issued_shares(twse_basic)
        frames.append(twse_shares)
        print(f"已取得 {len(twse_shares)} 筆上市公司發行股數 ({time.monotonic() - t0:.1f}s)")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"取得 TWSE 公司發行股數失敗：{exc}")

    # Fetch TPEX OTC companies
    try:
        print("正在取得 TPEX 上櫃公司資料...", flush=True)
        t0 = time.monotonic()
        tpex_basic = fetch_tpex_company_basic(session)
        tpex_shares = prepare_tpex_issued_shares(tpex_basic)
        frames.append(tpex_shares)
        print(f"已取得 {len(tpex_shares)} 筆上櫃公司發行股數 ({time.monotonic() - t0:.1f}s)")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"取得 TPEX 公司發行股數失敗：{exc}")

    if not frames:
        return pd.DataFrame(columns=["symbol", "name", "issued_shares"])

    return pd.concat(frames, ignore_index=True)


def _get_issued_shares(
    session: requests.Session,
    config: AppConfig,
) -> dict[str, int]:
    """Get issued shares, loading from DB or fetching from API.

    Priority: in-memory cache → DB → API (then upsert to DB).
    """
    global _issued_shares_cache
    if _issued_shares_cache:
        return _issued_shares_cache

    _issued_shares_cache = load_stock_shares(config.database_url)
    if _issued_shares_cache:
        print(f"已從 DB 載入 {len(_issued_shares_cache)} 筆發行股數")
        return _issued_shares_cache

    print("正在從 API 取得發行股數...")
    df = _fetch_issued_shares_from_api(session)
    if not df.empty:
        upsert_stock_shares(config.database_url, df)
        print(f"已寫入 {len(df)} 筆發行股數至 DB")
        for _, row in df.iterrows():
            symbol = str(row["symbol"]).strip()
            issued = row["issued_shares"]
            if symbol and pd.notna(issued):
                _issued_shares_cache[symbol] = int(issued)

    return _issued_shares_cache


def _update_shares_command(
    session: requests.Session,
    config: AppConfig,
) -> None:
    """Command to update issued shares to DB."""
    import time

    t_start = time.monotonic()
    print("正在從 API 取得發行股數...")
    df = _fetch_issued_shares_from_api(session)
    if df.empty:
        print("無法取得發行股數資料")
        return
    print(f"API 取得完成，共 {len(df)} 筆 ({time.monotonic() - t_start:.1f}s)")
    t_db = time.monotonic()
    print("正在寫入 DB...", flush=True)
    upsert_stock_shares(config.database_url, df)
    print(f"已更新 {len(df)} 筆發行股數至 DB ({time.monotonic() - t_db:.1f}s)")
    print(f"update-shares 總耗時 {time.monotonic() - t_start:.1f}s")


# TDCC 單支查詢的重試設定：暫時性網路錯誤或 token 過期（回「查無此資料」）時，
# 換新 token 後再試，避免單次異常永久漏掉某 (symbol, date)。
_TDCC_MAX_ATTEMPTS = 3
_TDCC_RETRY_DELAY = 1.5


def _resolve_dahu_dates(
    available_dates: list[dt.date],
    from_date: dt.date | None,
    to_date: dt.date | None,
) -> list[dt.date]:
    """決定 --dahu 要更新哪些 TDCC 週資料日期。

    - 有 --from/--to：取 available_dates 中落在 [from, to] 區間者（含端點）。
      只給單邊時，另一邊不設限。
    - 都沒給：只取最新一筆（available_dates 由新到舊排序，取第一筆）。

    回傳由舊到新排序，方便依序回補。
    """
    if from_date is None and to_date is None:
        # 明確取最新一週，不依賴 TDCC 頁面 option 的排列順序。
        return [max(available_dates)] if available_dates else []

    # 與 _build_date_range 一致：兩邊都給且顛倒時自動對調，避免吞掉合法區間。
    if from_date is not None and to_date is not None and from_date > to_date:
        from_date, to_date = to_date, from_date

    lo = from_date or dt.date.min
    hi = to_date or dt.date.max
    selected = [d for d in available_dates if lo <= d <= hi]
    return sorted(selected)


def _fetch_tdcc_with_retry(
    session: requests.Session,
    token: str,
    symbol: str,
    date: dt.date,
) -> tuple[pd.DataFrame | None, str, Exception | None]:
    """以重試包裝 fetch_tdcc_distribution。

    暫時性網路錯誤、或 token 過期導致的「查無此資料」，換新 token 後再試，
    避免單次上游異常永久漏掉某 (symbol, date)。

    Returns:
        (distribution_or_None, token, last_exc)。成功時 distribution 非 None、
        token 為最新可用 token；全部嘗試失敗時 distribution 為 None。
    """
    import time

    last_exc: Exception | None = None
    for attempt in range(_TDCC_MAX_ATTEMPTS):
        try:
            # token 為單次有效，鏈接每次回應回傳的新 token。
            dist, token = fetch_tdcc_distribution(session, token, symbol, date)
            return dist, token, None
        except (DataUnavailableError, requests.RequestException) as exc:
            last_exc = exc
            # 失敗時手上的 token 已消耗/狀態未知，換一個新的再試。
            try:
                token, _ = fetch_tdcc_token_and_dates(session)
            except (DataUnavailableError, requests.RequestException):
                pass
            if attempt < _TDCC_MAX_ATTEMPTS - 1:
                time.sleep(_TDCC_RETRY_DELAY * (attempt + 1))

    return None, token, last_exc


def _dahu_command(
    session: requests.Session,
    config: AppConfig,
    args: argparse.Namespace,
    today: dt.date,
) -> None:
    """--dahu：更新大戶持股佔比（TDCC 集保戶股權分散表），其他資料不更新。"""
    db_url = config.database_url

    # 決定目標股票
    if args.stocks:
        symbols = [s.strip() for s in args.stocks.split(",") if s.strip()]
        if not symbols:
            print("錯誤：--stocks 未指定任何股票代號")
            return
        name_map = load_stock_names(db_url)
        holdings = [(s, name_map.get(s, "")) for s in symbols]
    else:
        enabled_rows = get_enabled_stocks(db_url)
        if not enabled_rows:
            print("錯誤：資料庫中無啟用的股票（stocks.enabled = TRUE）")
            return
        holdings = [(r[0], r[1]) for r in enabled_rows]

    # 解析區間（若有）
    from_date = _parse_date(args.from_date) if args.from_date else None
    to_date = _parse_date(args.to_date) if args.to_date else None

    # 取得 TDCC token 與可查日期
    try:
        token, available_dates = fetch_tdcc_token_and_dates(session)
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"取得 TDCC 頁面失敗：{exc}")
        return

    target_dates = _resolve_dahu_dates(available_dates, from_date, to_date)
    if not target_dates:
        print("區間內無可查的 TDCC 週資料日期，未更新")
        return

    print(
        f"更新大戶持股佔比：{len(holdings)} 檔 × {len(target_dates)} 個日期"
        f"（{target_dates[0]} ~ {target_dates[-1]}）"
    )

    total_stocks = len(holdings)
    for date in target_dates:
        rows: list[tuple[str, str | None, float | None, float | None]] = []
        n_failed = 0
        for idx, (symbol, name) in enumerate(holdings):
            print(f"  {date.isoformat()} {idx + 1}/{total_stocks} {symbol}")
            dist, token, last_exc = _fetch_tdcc_with_retry(session, token, symbol, date)
            if dist is None:
                n_failed += 1
                print(
                    f"    {symbol} TDCC 取得失敗（重試 {_TDCC_MAX_ATTEMPTS} 次）：{last_exc}"
                )
                continue

            ratio = prepare_tdcc_major_ratio(dist)
            if ratio is None:
                n_failed += 1
                print(f"    {symbol} 無法解析大戶持股佔比")
                continue
            # 散戶比例以同一份分散表解析；偶發 None 不擋寫（COALESCE 保護歷史值）。
            retail = prepare_tdcc_retail_ratio(dist)
            rows.append((symbol, name or None, ratio, retail))

        n_written = upsert_holder_percent(db_url, date, rows)
        print(
            f"  {date.isoformat()} 大戶/散戶持股佔比已寫入 {n_written} 筆，失敗 {n_failed} 筆"
        )


def _is_daily_mode(args: argparse.Namespace) -> bool:
    """是否為「純 daily 模式」（無任何模式參數，抓今天）。

    只有此模式檢查 config.is_trading_day 休市開關；手動操作
    （--date / --backfill-* / --update-shares / --dahu）不受開關影響，隨時可跑。
    """
    return not (
        args.date
        or args.backfill_start
        or args.backfill_end
        or args.backfill_stocks
        or args.backfill_limits
        or args.update_shares
        or args.dahu
    )


def _parse_trading_day(value: str | None) -> bool:
    """解析 config.is_trading_day 的值。

    fail-open：讀不到（None）或無法辨識的值一律視為 True 照常執行，
    開關只是輔助，缺了不能影響原本抓資料流程。
    """
    if value is None:
        print("警告：config 表查無 is_trading_day，視為交易日照常執行")
        return True

    normalized = value.strip().lower()
    if normalized in ("false", "0", "no"):
        return False
    if normalized in ("true", "1", "yes"):
        return True

    print(f"警告：config.is_trading_day 值無法辨識（{value!r}），視為交易日照常執行")
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="台股每日 raw data 抓取")
    parser.add_argument("--date", type=str, help="指定日期 (YYYY-MM-DD)")
    parser.add_argument("--backfill-start", type=str, default=None, help="回補起始日")
    parser.add_argument("--backfill-end", type=str, default=None, help="回補結束日")
    parser.add_argument(
        "--backfill-stocks", type=str, default=None,
        help="回補特定股票（逗號分隔）",
    )
    parser.add_argument(
        "--backfill-limits", action="store_true",
        help="只回補 limit_up / limit_down（需搭配 --backfill-start / --backfill-end）",
    )
    parser.add_argument(
        "--update-shares", action="store_true",
        help="更新發行股數至資料庫",
    )
    parser.add_argument(
        "--dahu", action="store_true",
        help="只更新大戶持股佔比（TDCC 集保戶股權分散表，每週一次），其他資料不更新",
    )
    parser.add_argument(
        "--stocks", type=str, default=None,
        help="搭配 --dahu：只更新特定股票（逗號分隔，例：2330,2303）",
    )
    parser.add_argument(
        "--from", dest="from_date", type=str, default=None,
        help="搭配 --dahu：更新區間起始日（YYYY-MM-DD，對應到區間內的週資料日）",
    )
    parser.add_argument(
        "--to", dest="to_date", type=str, default=None,
        help="搭配 --dahu：更新區間結束日（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="強制覆蓋已存在的資料",
    )
    return parser.parse_args()


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _build_date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    if start > end:
        start, end = end, start
    days = (end - start).days
    return [start + dt.timedelta(days=offset) for offset in range(days + 1)]


def _fetch_tpex_sources(
    session: requests.Session,
    date: dt.date,
) -> tuple[pd.DataFrame | None, dt.date | None, pd.DataFrame | None, dt.date | None]:
    """Fetch and prepare TPEX data sources."""
    tpex_quotes_raw, tpex_quotes_date = fetch_tpex_daily_quotes_v2(session, date)
    tpex_quotes = prepare_tpex_quotes(tpex_quotes_raw)

    tpex_3insti_raw, tpex_3insti_date = fetch_tpex_3insti_v2(session, date)
    tpex_3insti = prepare_tpex_3insti(tpex_3insti_raw)

    if tpex_quotes_date != date:
        tpex_quotes = None
    if tpex_3insti_date != date:
        tpex_3insti = None

    return tpex_quotes, tpex_quotes_date, tpex_3insti, tpex_3insti_date


def _fetch_twse_3insti(session: requests.Session, date: dt.date) -> pd.DataFrame:
    """Fetch and prepare TWSE institutional investors data."""
    twse_t86 = fetch_twse_t86(session, date)
    return prepare_twse_3insti(twse_t86)


def _row_market_type(item) -> str | None:
    """從 holdings 的一列取出正規化後的 market_type（'twse' / 'tpex' / None）。

    holdings 可能根本沒有這欄（`--backfill-stocks` 由 CLI 代號組出來），
    有欄時 pandas 也會把缺值變成 NaN，兩種都要收斂成 None。
    """
    value = item.get("market_type")
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().lower()
    return text or None


def _stock_sources_ok(
    *,
    is_tpex: bool,
    twse_insti_ok: bool,
    tpex_insti_ok: bool,
) -> bool:
    """單檔個股所依賴的「必要」來源是否都成功（失敗則該檔跳過不寫）。

    必要來源 = OHLC（價格，由呼叫端的無價格判斷另行處理）＋ 三大法人（依市場別）。
    融資融券「不」納入必要：個股可能不開放融資融券，資料本就可能缺，
    讓它阻擋會把合法無券資的個股也跳掉。融資融券靠 fetch 層 retry ＋
    upsert COALESCE（不以 NULL 覆寫舊值）處理，不在此 gating。
    """
    return tpex_insti_ok if is_tpex else twse_insti_ok


def _collect_limit_updates(
    session: requests.Session,
    date: dt.date,
) -> list[tuple[str, dt.date, Decimal, Decimal]]:
    """抓單日兩市場行情，算出可寫入的 (symbol, date, limit_up, limit_down)。

    只用 MI_INDEX（上市）與 TPEX v2 dailyQuotes（上櫃）—— STOCK_DAY_ALL 無視 date
    參數永遠回最後交易日，不能用於回補。任一市場失敗只影響該市場，不中斷另一邊。
    推不出參考價的個股整檔跳過，不寫入也不覆蓋既有值。
    """
    frames: list[pd.DataFrame] = []

    try:
        mi_raw, mi_date = fetch_twse_mi_index(session, date)
        if mi_date == date:
            frames.append(prepare_twse_mi_index(mi_raw))
        else:
            print(f"{date.isoformat()} TWSE MI_INDEX 日期不匹配：{mi_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{date.isoformat()} TWSE MI_INDEX 取得失敗：{exc}")

    try:
        tpex_raw, tpex_date = fetch_tpex_daily_quotes_v2(session, date)
        if tpex_date == date:
            frames.append(prepare_tpex_quotes(tpex_raw))
        else:
            print(f"{date.isoformat()} TPEX 日行情日期不匹配：{tpex_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{date.isoformat()} TPEX 日行情取得失敗：{exc}")

    updates: list[tuple[str, dt.date, Decimal, Decimal]] = []
    for frame in frames:
        for _, row in frame.iterrows():
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            limit_up, limit_down = _price_limits(
                row.get("close"), row.get("change"), row.get("high"), row.get("low")
            )
            if limit_up is None or limit_down is None:
                continue
            updates.append((symbol, date, limit_up, limit_down))
    return updates


def _backfill_limits_command(
    session: requests.Session,
    config: AppConfig,
    args: argparse.Namespace,
) -> None:
    """--backfill-limits：只回補 stock_daily_raw 的 limit_up / limit_down。"""
    if args.backfill_stocks or args.date:
        ignored = [
            name
            for name, val in [("--backfill-stocks", args.backfill_stocks), ("--date", args.date)]
            if val
        ]
        print(f"警告：--backfill-limits 已啟用，{'、'.join(ignored)} 將被忽略")

    if not args.backfill_start or not args.backfill_end:
        print("錯誤：--backfill-limits 需搭配 --backfill-start 和 --backfill-end")
        return

    dates = _build_date_range(
        _parse_date(args.backfill_start), _parse_date(args.backfill_end)
    )
    print(f"回補漲跌停 {len(dates)} 天：{dates[0]} ~ {dates[-1]}")

    total = 0
    for date in dates:
        if date.weekday() >= 5:
            continue
        # 先問 DB 該日有哪些 symbol。交易所公布的是全市場標的（含權證等，單日約
        # 6700 筆），本 repo 只存 enabled 個股的兩百多列；不過濾就整批送上去，
        # 96% 會命中 0 列，純粹浪費網路往返。DB 該日無列時連 API 都不用打。
        existing = load_symbols_for_date(config.database_url, date)
        if not existing:
            print(f"{date.isoformat()} DB 無該日資料，略過")
            continue
        updates = [u for u in _collect_limit_updates(session, date) if u[0] in existing]
        if not updates:
            print(f"{date.isoformat()} 無可回補資料")
            continue
        n_updated = update_price_limits_batch(config.database_url, updates)
        total += n_updated
        print(f"{date.isoformat()} 更新 {n_updated} 檔")

    print(f"漲跌停回補完成，共更新 {total} 列")


def _build_daily_rows(
    session: requests.Session,
    date: dt.date,
    holdings: pd.DataFrame,
    twse_3insti: pd.DataFrame,
    twse_day_all: pd.DataFrame | None,
    twse_mi_index: pd.DataFrame | None,
    tpex_quotes: pd.DataFrame,
    tpex_3insti: pd.DataFrame,
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
    issued_shares: dict[str, int] | None = None,
    twse_margin: pd.DataFrame | None = None,
    tpex_margin: pd.DataFrame | None = None,
    margin_cache: dict[str, dict[dt.date, dict]] | None = None,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None = None,
    name_map: dict[str, str] | None = None,
    twse_insti_ok: bool = True,
    tpex_insti_ok: bool = True,
) -> pd.DataFrame:
    """Build raw daily rows for stock_daily_raw (no indicators/statistics).

    逐檔跳過（不寫該檔列）條件：
    1. 無價格（open/close 皆 None）：OHLC 來源對該檔失敗或當天無交易。
    2. 該檔市場別的三大法人來源 fetch 失敗（見 _stock_sources_ok）。
    跳過的個股留待重跑 / backfill 補上（搭配 upsert 的 COALESCE）。

    市場別有兩個獨立訊號，用途不同、不要互相取代：
    - 條件 2 的 gating 用「該檔是否出現在當日 tpex_quotes」判定，反映的是
      「這檔今天的價格是誰供應的」，必須跟著當日實際來源走。
    - OHLCV fallback 用 holdings 的 `stocks.market_type`（見 _fetch_ohlcv_with_fallback），
      反映的是「這檔本質上屬於哪個市場」，不能依賴當日 tpex_quotes 是否抓成功
      —— 否則 TPEX 整批失敗時，上櫃股又會退回去打註定沒資料的 TWSE 月表。
    """
    rows: list[dict] = []
    total = len(holdings)
    skipped = 0
    if name_map is None:
        name_map = {}

    if not tpex_quotes.empty and "symbol" in tpex_quotes.columns:
        tpex_symbols = set(tpex_quotes["symbol"].astype(str).str.strip())
    else:
        tpex_symbols = set()

    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        name = name_map.get(symbol, "")
        display_name = f" {name}" if name else ""
        print(f"{date.isoformat()} {idx + 1}/{total} {symbol}{display_name}")

        ohlcv = _fetch_ohlcv_with_fallback(
            session, date, symbol, twse_day_all, twse_mi_index,
            tpex_quotes, twse_month_cache,
            market_type=_row_market_type(item),
        )
        open_price = ohlcv.open
        close_price = ohlcv.close
        high_price = ohlcv.high
        low_price = ohlcv.low
        volume = ohlcv.volume

        limit_up, limit_down = _price_limits(
            close_price, ohlcv.change, high_price, low_price
        )

        # 逐檔跳過 1：無價格（OHLC 來源對該檔失敗或當天無交易）
        if close_price is None and open_price is None:
            skipped += 1
            continue

        # 逐檔跳過 2：該檔市場別的三大法人來源失敗（融資融券例外，不 gating）
        is_tpex = symbol in tpex_symbols
        if not _stock_sources_ok(
            is_tpex=is_tpex,
            twse_insti_ok=twse_insti_ok,
            tpex_insti_ok=tpex_insti_ok,
        ):
            skipped += 1
            continue

        foreign_net, trust_net, dealer_net = _get_institutional_data(
            symbol, twse_3insti, tpex_3insti,
        )

        if margin_cache is not None and symbol in margin_cache and date in margin_cache[symbol]:
            margin_data = margin_cache[symbol][date]
        else:
            margin_data = _get_margin_data(symbol, twse_margin, tpex_margin)

        # Convert volume to lots (張)
        volume_lots = volume // 1000 if volume is not None else None

        # Convert institutional flows to lots
        foreign_net_lots = foreign_net // 1000 if foreign_net is not None else None
        trust_net_lots = trust_net // 1000 if trust_net is not None else None
        dealer_net_lots = dealer_net // 1000 if dealer_net is not None else None
        insti_total_lots = (
            None
            if foreign_net_lots is None and trust_net_lots is None and dealer_net_lots is None
            else (foreign_net_lots or 0) + (trust_net_lots or 0) + (dealer_net_lots or 0)
        )

        # turnover_rate (volume / issued_shares)
        turnover_rate = None
        if issued_shares and volume is not None:
            shares = issued_shares.get(symbol)
            if shares and shares > 0:
                turnover_rate = round(volume / shares, 6)

        # short_margin_ratio
        margin_balance = margin_data.get("margin_balance")
        short_balance = margin_data.get("short_balance")
        short_margin_ratio = None
        if margin_balance is not None and margin_balance > 0:
            if short_balance is not None:
                short_margin_ratio = round(short_balance / margin_balance, 6)

        # holding_pct
        holding_pct = {}
        if holding_pct_cache is not None and symbol in holding_pct_cache:
            holding_pct = holding_pct_cache[symbol].get(date, {})

        rows.append({
            "symbol": symbol,
            "name": name,
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": volume_lots,
            "turnover_rate": turnover_rate,
            "foreign_net": foreign_net_lots,
            "trust_net": trust_net_lots,
            "dealer_net": dealer_net_lots,
            "institutional_investors_net": insti_total_lots,
            "margin_buy": margin_data.get("margin_buy"),
            "margin_sell": margin_data.get("margin_sell"),
            "margin_balance": margin_balance,
            "margin_change": margin_data.get("margin_change"),
            "short_sell": margin_data.get("short_sell"),
            "short_buy": margin_data.get("short_buy"),
            "short_balance": short_balance,
            "short_change": margin_data.get("short_change"),
            "short_margin_ratio": short_margin_ratio,
            "foreign_holding_pct": holding_pct.get("foreign_holding_pct"),
            "insti_holding_pct": holding_pct.get("insti_holding_pct"),
            "limit_up": limit_up,
            "limit_down": limit_down,
        })

    if skipped:
        print(
            f"{date.isoformat()} 逐檔跳過 {skipped}/{total} 檔"
            f"（無價格或三大法人來源失敗），不寫入待重跑/回補"
        )

    return pd.DataFrame(rows)


class OhlcvResult(NamedTuple):
    """單檔單日的 OHLCV 與漲跌價差。

    change 有自己的來源規則（見 _fetch_ohlcv_with_fallback），不跟隨 OHLCV 補洞。
    """

    open: float | None
    close: float | None
    high: float | None
    low: float | None
    volume: int | None
    change: float | None


def _reference_price(close, change) -> Decimal | None:
    """由收盤價與漲跌價差推當日參考價；任一缺值即回 None，不以前日收盤推測。

    pandas 會把含 None 的 float 欄位轉成 NaN，故 None 與 NaN 都要擋。
    轉換一律走 Decimal(str(x))：Decimal(2415.0) 會把 float 誤差整包帶進來。
    """
    if close is None or change is None:
        return None
    if pd.isna(close) or pd.isna(change):
        return None
    return Decimal(str(close)) - Decimal(str(change))


def _price_limits(close, change, high=None, low=None):
    """算當日漲跌停價；區間若無拘束力則回 (None, None)。

    新上市櫃前五日等標的無漲跌幅限制（櫃買以「次日漲停價 9995 / 跌停價 0.01」表示），
    但交易所照樣給漲跌價差，照 ±10% 算出來的區間是假的。實際成交價落在區間外就是
    鐵證：有漲跌幅限制時不可能成交在區間外，所以這種列一律寫 NULL 而不是留假值。

    收盤價剛好等於漲停/跌停價是漲停跌停，不是區間失效，不可誤殺。
    """
    limit_up, limit_down = calc_limits(_reference_price(close, change))
    if limit_up is None or limit_down is None:
        return None, None
    for price in (high, low, close):
        if price is None or pd.isna(price):
            continue
        if Decimal(str(price)) > limit_up or Decimal(str(price)) < limit_down:
            return None, None
    return limit_up, limit_down


def _fetch_ohlcv_with_fallback(
    session: requests.Session,
    date: dt.date,
    symbol: str,
    twse_day_all: pd.DataFrame | None,
    twse_mi_index: pd.DataFrame | None,
    tpex_quotes: pd.DataFrame,
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
    market_type: str | None = None,
) -> OhlcvResult:
    """Fetch OHLCV with fallback chain: DAY_ALL -> MI_INDEX -> TPEX -> STOCK_DAY.

    順序的重點是「全市場批次來源優先，逐檔 HTTP 墊底」：前三個來源都是
    `_run_for_date` 每日各抓一次的整批資料（記憶體查表，零額外請求），只有
    STOCK_DAY 月表是每檔各打一次 www.twse.com.tw。把它排到最後，正常日子就完全
    不會觸發 —— 之前它排在 MI_INDEX 前面，只要 STOCK_DAY_ALL 有任何一欄沒補上
    （例如 2026-08-19 它無法解析日期而整批棄用），全部個股就會各打一次 API，
    幾百次請求足以踩到 TWSE 限流，而限流回應與「沒資料」無法區分。

    market_type 為 "tpex" 時完全跳過 STOCK_DAY：那支 API 只有上市資料，對上櫃股
    必定回「很抱歉，沒有符合條件的資料!」，打了純粹浪費限流配額。未知（None）時
    維持既有行為往下打，不誤殺 —— `--backfill-stocks` 直接給代號時就沒有市場別。
    """
    open_price = close_price = high_price = low_price = volume = None
    change = None

    # 注意：change 刻意不從 STOCK_DAY_ALL 取 —— 它在除權息日給 Change=0.0000
    # 且不帶任何標記，會算出「參考價 = 收盤」的錯值。change 只從 MI_INDEX /
    # TPEX quotes 取，見本函式末尾的獨立區塊。
    # Try TWSE STOCK_DAY_ALL（全市場批次）
    if twse_day_all is not None:
        row = twse_day_all.loc[twse_day_all["symbol"] == symbol]
        if not row.empty:
            open_price = row.iloc[0]["open"]
            close_price = row.iloc[0]["close"]
            high_price = row.iloc[0].get("high")
            low_price = row.iloc[0].get("low")
            volume = row.iloc[0].get("volume")

    # Try TWSE MI_INDEX（全市場批次，涵蓋全部上市）
    if any(v is None for v in [open_price, close_price, high_price, low_price, volume]):
        if twse_mi_index is not None:
            row = twse_mi_index.loc[twse_mi_index["symbol"] == symbol]
            if not row.empty:
                if open_price is None:
                    open_price = row.iloc[0]["open"]
                if close_price is None:
                    close_price = row.iloc[0]["close"]
                if high_price is None:
                    high_price = row.iloc[0].get("high")
                if low_price is None:
                    low_price = row.iloc[0].get("low")
                if volume is None:
                    volume = row.iloc[0].get("volume")

    # Try TPEX quotes（全市場批次，涵蓋全部上櫃）
    if open_price is None and close_price is None:
        row = tpex_quotes.loc[tpex_quotes["symbol"] == symbol]
        if not row.empty:
            open_price = row.iloc[0]["open"]
            close_price = row.iloc[0]["close"]
            high_price = row.iloc[0].get("high")
            low_price = row.iloc[0].get("low")
            volume = row.iloc[0].get("volume")

    # Try TWSE STOCK_DAY (monthly) —— 唯一的逐檔 HTTP，最後手段。
    # 上櫃股直接跳過：TWSE 月表沒有上櫃資料，打了也只是消耗限流配額。
    if market_type != "tpex" and any(
        v is None for v in [open_price, close_price, high_price, low_price, volume]
    ):
        month_start = date.replace(day=1)
        cache_key = (symbol, month_start)
        twse_day = twse_month_cache.get(cache_key)

        if twse_day is None:
            try:
                twse_day = fetch_twse_stock_day(session, symbol, date)
                twse_month_cache[cache_key] = twse_day
            except DataUnavailableError:
                pass

        if twse_day is not None:
            ohlcv = find_twse_ohlcv(twse_day, date)
            if open_price is None:
                open_price = ohlcv[0]
            if high_price is None:
                high_price = ohlcv[1]
            if low_price is None:
                low_price = ohlcv[2]
            if close_price is None:
                close_price = ohlcv[3]
            if volume is None:
                volume = ohlcv[4]

    # change（漲跌價差）獨立取得：不受 OHLCV 是否齊全影響，也不觸發逐檔 HTTP。
    # MI_INDEX 涵蓋全部上市、TPEX quotes 涵蓋全部上櫃，兩者在 _run_for_date 都是
    # 每日必抓；兩邊都沒有時留 None，由呼叫端寫成 NULL（不以前日收盤推測）。
    if change is None and twse_mi_index is not None:
        row = twse_mi_index.loc[twse_mi_index["symbol"] == symbol]
        if not row.empty:
            change = row.iloc[0].get("change")
    # 注意：用 `is None`、不是 `pd.isna`。MI_INDEX 除權息日回的 change 是 NaN
    # 不是 None，所以上一個 block 賦值後，這裡的 `change is None` 不會為 True，
    # 不會誤把上市股（不在 TPEX）的 NaN 又拿 TPEX 的資料覆蓋一次 —— 只是這個
    # 「不會誤觸發」目前是靠「上市股不在 TPEX quotes 裡」這個外部事實撐住，
    # 不是程式碼本身保證的。日後若再加第三個 change 來源（尤其若它的「找不到」
    # 用 None 表示），這裡要重新檢視，否則可能把已取到的 NaN 又蓋一次。
    if change is None and not tpex_quotes.empty:
        row = tpex_quotes.loc[tpex_quotes["symbol"] == symbol]
        if not row.empty:
            change = row.iloc[0].get("change")

    return OhlcvResult(
        open=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume=volume,
        change=change,
    )


def _get_institutional_data(
    symbol: str,
    twse_3insti: pd.DataFrame,
    tpex_3insti: pd.DataFrame,
) -> tuple[int | None, int | None, int | None]:
    """Get institutional investors net buy/sell data."""
    foreign_net = trust_net = dealer_net = None

    row = twse_3insti.loc[twse_3insti["symbol"] == symbol]
    if not row.empty:
        foreign_net = row.iloc[0]["foreign_net"]
        trust_net = row.iloc[0]["trust_net"]
        dealer_net = row.iloc[0]["dealer_net"]
    else:
        row = tpex_3insti.loc[tpex_3insti["symbol"] == symbol]
        if not row.empty:
            foreign_net = row.iloc[0]["foreign_net"]
            trust_net = row.iloc[0]["trust_net"]
            dealer_net = row.iloc[0]["dealer_net"]

    return foreign_net, trust_net, dealer_net


def _get_margin_data(
    symbol: str,
    twse_margin: pd.DataFrame | None,
    tpex_margin: pd.DataFrame | None,
) -> dict[str, int | float | None]:
    """Get margin trading data for a single stock.

    Returns dict with keys: margin_buy, margin_sell, margin_balance, margin_change,
                            short_sell, short_buy, short_balance, short_change,
                            short_margin_ratio
    Units: lots (張), short_margin_ratio is ratio (1% = 0.01)
    """
    result = {
        "margin_buy": None,
        "margin_sell": None,
        "margin_balance": None,
        "margin_change": None,
        "short_sell": None,
        "short_buy": None,
        "short_balance": None,
        "short_change": None,
        "short_margin_ratio": None,
    }

    # Try TWSE margin first
    if twse_margin is not None and not twse_margin.empty:
        row = twse_margin.loc[twse_margin["symbol"] == symbol]
        if not row.empty:
            for key in result.keys():
                if key in row.columns:
                    val = row.iloc[0][key]
                    if pd.notna(val):
                        # short_margin_ratio is a float (ratio), others are int
                        if key == "short_margin_ratio":
                            result[key] = float(val)
                        else:
                            result[key] = int(val)
            return result

    # Try TPEX margin
    if tpex_margin is not None and not tpex_margin.empty:
        row = tpex_margin.loc[tpex_margin["symbol"] == symbol]
        if not row.empty:
            for key in result.keys():
                if key in row.columns:
                    val = row.iloc[0][key]
                    if pd.notna(val):
                        # short_margin_ratio is a float (ratio), others are int
                        if key == "short_margin_ratio":
                            result[key] = float(val)
                        else:
                            result[key] = int(val)

    return result


def _prefetch_margin_cache(
    session: requests.Session,
    holdings: pd.DataFrame,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[str, dict[dt.date, dict]]:
    """Pre-fetch margin data for all stocks in date range.

    Args:
        session: HTTP session
        holdings: DataFrame with stock symbols
        start_date: Start date of backfill range
        end_date: End date of backfill range

    Returns:
        Dict mapping symbol -> date -> margin_data_dict
        margin_data_dict contains: margin_buy, margin_sell, margin_balance,
        margin_change, short_sell, short_buy, short_balance, short_change,
        short_margin_ratio
    """
    cache: dict[str, dict[dt.date, dict]] = {}
    total = len(holdings)

    # Add buffer days before start_date to ensure we have data
    fetch_start = start_date - dt.timedelta(days=10)

    print(f"預取融資融券資料 {start_date} ~ {end_date}...")

    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        print(f"  預取融資融券 {idx + 1}/{total} {symbol}")

        cache[symbol] = {}
        try:
            raw = fetch_moneydj_margin(session, symbol, fetch_start, end_date)
            df = prepare_moneydj_margin(raw)

            for _, row in df.iterrows():
                row_date = row["date"]
                if not isinstance(row_date, dt.date):
                    continue
                cache[symbol][row_date] = {
                    "margin_buy": row.get("margin_buy"),
                    "margin_sell": row.get("margin_sell"),
                    "margin_balance": row.get("margin_balance"),
                    "margin_change": row.get("margin_change"),
                    "short_sell": row.get("short_sell"),
                    "short_buy": row.get("short_buy"),
                    "short_balance": row.get("short_balance"),
                    "short_change": row.get("short_change"),
                    "short_margin_ratio": row.get("short_margin_ratio"),
                }
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"    {symbol} 融資融券取得失敗：{exc}")

    print(f"融資融券預取完成，共 {len(cache)} 檔股票")
    return cache


def _prefetch_holding_pct_cache(
    session: requests.Session,
    holdings: pd.DataFrame,
    start_date: dt.date,
    end_date: dt.date,
) -> dict[str, dict[dt.date, dict]]:
    """Pre-fetch institutional holding percentage for all stocks in date range.

    Returns:
        Dict mapping symbol -> date -> {"foreign_holding_pct": x, "insti_holding_pct": y}
    """
    cache: dict[str, dict[dt.date, dict]] = {}
    total = len(holdings)

    print(f"預取法人持股比重資料 {start_date} ~ {end_date}...")

    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        print(f"  預取法人持股 {idx + 1}/{total} {symbol}")

        cache[symbol] = {}
        try:
            raw = fetch_moneydj_holding_pct(session, symbol, start_date, end_date)
            df = prepare_moneydj_holding_pct(raw)

            for _, row in df.iterrows():
                row_date = row["date"]
                if not isinstance(row_date, dt.date):
                    continue
                cache[symbol][row_date] = {
                    "foreign_holding_pct": row.get("foreign_holding_pct"),
                    "insti_holding_pct": row.get("insti_holding_pct"),
                }
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"    {symbol} 法人持股取得失敗：{exc}")

    print(f"法人持股預取完成，共 {len(cache)} 檔股票")
    return cache


def _run_for_date(
    session: requests.Session,
    date: dt.date,
    holdings: pd.DataFrame,
    sheet_names: set[str],
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
    config: AppConfig,
    today: dt.date,
    skip_existing: bool = False,
    issued_shares: dict[str, int] | None = None,
    margin_cache: dict[str, dict[dt.date, dict]] | None = None,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None = None,
    name_map: dict[str, str] | None = None,
    write_market_daily: bool = True,
) -> bool:
    """Process data for a single date.

    write_market_daily: 是否寫入全市場大盤資料 (market_daily)。market_daily 以
    trade_date 為鍵、與個股無關；--backfill-stocks（特定股票回補）會傳 False，
    避免對共用的大盤表產生非預期副作用。一般日期範圍回補與 daily 模式維持 True。
    """
    sheet_name = date.isoformat()
    print(f"開始處理日期 {sheet_name}")

    # Skip weekends
    if date.weekday() >= 5:
        print(f"{sheet_name} 週末休市，略過寫入")
        return False

    # Skip existing sheets in backfill mode
    if skip_existing and sheet_name in sheet_names:
        print(f"已存在 {sheet_name}，略過回補。")
        return False

    # Fetch TWSE 3-institutional data
    try:
        with _phase(f"{sheet_name} TWSE 三大法人"):
            twse_3insti = _fetch_twse_3insti(session, date)
    except DataUnavailableError as exc:
        print(f"{sheet_name} TWSE 資料尚未公告或取得失敗：{exc}")
        twse_3insti = pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"])
    except requests.RequestException as exc:
        print(f"{sheet_name} TWSE 網路連線失敗：{exc}")
        return False

    # Fetch TWSE STOCK_DAY_ALL (today only)
    twse_day_all = None
    twse_day_all_date = None
    if date == today:
        try:
            with _phase(f"{sheet_name} TWSE STOCK_DAY_ALL"):
                twse_day_all_raw, twse_day_all_date = fetch_twse_stock_day_all(session)
            if twse_day_all_date is None:
                print(f"{sheet_name} TWSE STOCK_DAY_ALL 無法解析日期，略過使用")
            elif twse_day_all_date != date:
                print(f"{sheet_name} TWSE STOCK_DAY_ALL 日期不匹配：{twse_day_all_date} != {date}")
            else:
                twse_day_all = prepare_twse_day_all(twse_day_all_raw)
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TWSE STOCK_DAY_ALL 取得失敗：{exc}")

    # Fetch TWSE MI_INDEX
    twse_mi_index = None
    twse_mi_index_date = None
    try:
        with _phase(f"{sheet_name} TWSE MI_INDEX"):
            twse_mi_index_raw, twse_mi_index_date = fetch_twse_mi_index(session, date)
        if twse_mi_index_date is None and not twse_mi_index_raw.empty and date == today:
            twse_mi_index_date = date
        if twse_mi_index_date == date:
            twse_mi_index = prepare_twse_mi_index(twse_mi_index_raw)
        elif twse_mi_index_date is not None:
            print(f"{sheet_name} TWSE MI_INDEX 日期不匹配：{twse_mi_index_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{sheet_name} TWSE MI_INDEX 取得失敗：{exc}")

    # Check if TWSE data is available
    twse_confirmed = (
        (twse_day_all_date == date)
        or (twse_mi_index_date == date)
        or (not twse_3insti.empty)
    )
    if not twse_confirmed:
        print(f"{sheet_name} TWSE 資料不足，視為休市，略過寫入")
        return False

    # Fetch TPEX data
    try:
        with _phase(f"{sheet_name} TPEX 整批（日行情＋三大法人）"):
            tpex_quotes, tpex_quotes_date, tpex_3insti, tpex_3insti_date = _fetch_tpex_sources(
                session, date
            )
        if tpex_quotes_date and tpex_quotes_date != date:
            print(f"{sheet_name} TPEX 日行情日期不匹配：{tpex_quotes_date} != {date}")
        if tpex_3insti_date and tpex_3insti_date != date:
            print(f"{sheet_name} TPEX 三大法人日期不匹配：{tpex_3insti_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{sheet_name} TPEX 資料取得失敗：{exc}")
        tpex_quotes = None
        tpex_3insti = None

    if tpex_quotes is None:
        tpex_quotes = pd.DataFrame(columns=["symbol", "name", "open", "close", "high", "low", "volume"])
    if tpex_3insti is None:
        tpex_3insti = pd.DataFrame(columns=["symbol", "name", "foreign_net", "trust_net", "dealer_net"])

    # Fetch margin trading data
    twse_margin = None
    tpex_margin = None

    if margin_cache is not None:
        # Use pre-fetched cache (backfill mode with cache)
        # margin_cache will be used directly in _build_daily_rows
        pass
    elif date == today:
        # Use dated MI_MARGN report for today's data (all stocks at once)
        try:
            with _phase(f"{sheet_name} TWSE 融資融券"):
                twse_margin_raw, twse_margin_date = fetch_twse_margin(session, date)
            # 嚴格驗證資料日期 == 當日；不符（TWSE 尚未發布或回舊資料）就不寫，
            # 缺值由 D+1 的 MoneyDJ 修正機制補，避免 D-1 值被誤標成 D。
            if twse_margin_date == date:
                twse_margin = prepare_twse_margin(twse_margin_raw)
            else:
                print(f"{sheet_name} TWSE 融資融券日期不匹配：{twse_margin_date} != {date}")
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TWSE 融資融券取得失敗：{exc}")

        # Try V2 first (supports date parameter), fallback to OpenAPI
        try:
            with _phase(f"{sheet_name} TPEX 融資融券 V2"):
                tpex_margin_raw, tpex_margin_date = fetch_tpex_margin_v2(session, date)
            if tpex_margin_date is None or tpex_margin_date == date:
                tpex_margin = prepare_tpex_margin_v2(tpex_margin_raw)
            else:
                print(f"{sheet_name} TPEX V2 融資融券日期不匹配：{tpex_margin_date} != {date}")
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TPEX V2 融資融券取得失敗：{exc}")

        if tpex_margin is None:
            try:
                with _phase(f"{sheet_name} TPEX 融資融券 OpenAPI（V2 回退）"):
                    tpex_margin_raw, tpex_margin_date = fetch_tpex_margin(session)
                if tpex_margin_date is None or tpex_margin_date == date:
                    tpex_margin = prepare_tpex_margin(tpex_margin_raw)
                else:
                    print(
                        f"{sheet_name} TPEX OpenAPI 融資融券日期不匹配："
                        f"{tpex_margin_date} != {date}"
                    )
            except (DataUnavailableError, requests.RequestException) as exc2:
                print(f"{sheet_name} TPEX 融資融券取得失敗：{exc2}")
    else:
        # Use MoneyDJ for historical data (per-stock, build combined DataFrame)
        # This path is only used when margin_cache is not provided (single date backfill)
        margin_rows = []
        fetch_start = date - dt.timedelta(days=10)
        fetch_end = date
        for _, item in holdings.iterrows():
            symbol = str(item["symbol"]).strip()
            try:
                moneydj_raw = fetch_moneydj_margin(session, symbol, fetch_start, fetch_end)
                moneydj_df = prepare_moneydj_margin(moneydj_raw)
                # Find row for target date
                row = moneydj_df.loc[moneydj_df["date"] == date]
                if not row.empty:
                    row_data = row.iloc[0].to_dict()
                    row_data["symbol"] = symbol
                    margin_rows.append(row_data)
            except (DataUnavailableError, requests.RequestException):
                # Silently skip - margin data not critical
                pass
        if margin_rows:
            # Combine into a single DataFrame that works like twse_margin
            twse_margin = pd.DataFrame(margin_rows)

    # Fetch holding percentage data (per-stock, when not using cache)
    # daily 模式不帶 cache，故此處是逐檔對 MoneyDJ 各打一次；失敗一律吞掉，
    # 沒有計時就完全看不出它在整段啟動時間裡佔多少。
    if holding_pct_cache is None:
        holding_pct_cache = {}
        with _phase(f"{sheet_name} 外資/法人持股佔比（逐檔 {len(holdings)} 檔）"):
            for _, item in holdings.iterrows():
                symbol = str(item["symbol"]).strip()
                try:
                    raw = fetch_moneydj_holding_pct(session, symbol, date, date)
                    df = prepare_moneydj_holding_pct(raw)
                    holding_pct_cache[symbol] = {}
                    for _, row in df.iterrows():
                        row_date = row["date"]
                        if isinstance(row_date, dt.date):
                            holding_pct_cache[symbol][row_date] = {
                                "foreign_holding_pct": row.get("foreign_holding_pct"),
                                "insti_holding_pct": row.get("insti_holding_pct"),
                            }
                except (DataUnavailableError, requests.RequestException):
                    pass

    # 三大法人來源健康度（已過 twse_confirmed，交易日下「空」＝該來源 fetch 失敗）。
    # 逐檔寫入時用來決定該市場個股是否跳過（融資融券非必要，不納入）。
    twse_insti_ok = not twse_3insti.empty
    tpex_insti_ok = not tpex_3insti.empty

    # Build daily data
    with _phase(f"{sheet_name} 逐檔組列（{len(holdings)} 檔）"):
        output_df = _build_daily_rows(
            session=session,
            date=date,
            holdings=holdings,
            twse_3insti=twse_3insti,
            twse_day_all=twse_day_all,
            twse_mi_index=twse_mi_index,
            tpex_quotes=tpex_quotes,
            tpex_3insti=tpex_3insti,
            twse_month_cache=twse_month_cache,
            issued_shares=issued_shares,
            twse_margin=twse_margin,
            tpex_margin=tpex_margin,
            margin_cache=margin_cache,
            holding_pct_cache=holding_pct_cache,
            name_map=name_map,
            twse_insti_ok=twse_insti_ok,
            tpex_insti_ok=tpex_insti_ok,
        )

    if output_df.empty:
        print(f"{sheet_name} 找不到任何成份股資料。")
        return False

    if output_df["close"].isna().all():
        print(f"{sheet_name} 當天價格資料尚未公告，未寫入。")
        return False

    sheet_names.add(sheet_name)

    with _phase(f"{sheet_name} 寫入 stock_daily_raw（{len(output_df)} 列）"):
        upsert_daily_raw(config.database_url, date, output_df)

    # Fetch and upsert market daily data (大盤行情)。market_daily 與個股無關，
    # 特定股票回補 (--backfill-stocks) 不需更新，避免對共用大盤表的非預期副作用。
    if write_market_daily:
        with _phase(f"{sheet_name} 大盤行情 market_daily"):
            _fetch_and_upsert_market_daily(session, date, config)

    # Daily 模式（非 backfill）下，用 MoneyDJ 修正 D-1 個股融資融券
    # backfill 已透過 _prefetch_margin_cache 預取修正版，不需再修
    if margin_cache is None:
        _refresh_prev_day_margin(session, holdings, date, config)

    return True


_PREV_MARGIN_FIELDS = [
    "margin_buy", "margin_sell", "margin_balance", "margin_change",
    "short_sell", "short_buy", "short_balance", "short_change",
    "short_margin_ratio",
]


def _expected_prev_trade_date(current_date: dt.date) -> dt.date:
    """日曆上的 D-1，只處理週末（不處理 holiday）。

    - 週一 → 上週五（-3）
    - 週日 → 上週五（-2，防呆）
    - 週六 → 週五（-1，防呆）
    - 其他 → -1
    """
    weekday = current_date.weekday()
    if weekday == 0:
        return current_date - dt.timedelta(days=3)
    if weekday == 6:
        return current_date - dt.timedelta(days=2)
    return current_date - dt.timedelta(days=1)


def _refresh_prev_day_margin(
    session: requests.Session,
    holdings: pd.DataFrame,
    current_date: dt.date,
    config: AppConfig,
) -> None:
    """用 MoneyDJ 修正 stock_daily_raw 上 D-1 的融資融券欄位。

    TWSE/TPEX 個股當日 OpenAPI 拿到的 margin/short 為速報版，隔天會被修正；
    MoneyDJ 提供修正後的最終版本。本 function 在 daily 模式下執行，
    依 holdings 對每支股票打 MoneyDJ 拿 D-1 資料並覆寫。

    跳過條件（任一）：
    - DB 找不到 D-1 row（completely empty / brand new install）
    - holdings 為空
    """
    if holdings.empty:
        return

    prev_date = find_consensus_prev_trade_date(config.database_url, current_date)
    if prev_date is None:
        print(
            f"  無 D-1 共識交易日（stock_daily_raw 與 market_daily 兩邊不一致或缺日），"
            f"略過 {current_date} 的融資融券修正"
        )
        return

    expected_prev = _expected_prev_trade_date(current_date)
    if prev_date != expected_prev:
        print(
            f"  DB D-1 ({prev_date}) 不等於 {current_date} 的日曆 D-1 ({expected_prev})，"
            f"略過融資融券修正（可能是部分回補狀態）"
        )
        return

    total = len(holdings)
    print(f"  開始修正 D-1 ({prev_date}) 融資融券資料（{total} 檔）...")

    # MoneyDJ 融資融券頁面對 c==d 的單日查詢只回 summary row、不含當日資料；
    # 必須用區間查詢再 filter 出 prev_date。
    fetch_start = prev_date - dt.timedelta(days=10)

    updates: list[tuple[str, dt.date, dict]] = []
    n_failed = 0
    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        try:
            raw = fetch_moneydj_margin(session, symbol, fetch_start, prev_date)
            df = prepare_moneydj_margin(raw)
        except (DataUnavailableError, requests.RequestException) as exc:
            n_failed += 1
            print(f"    {idx + 1}/{total} {symbol} MoneyDJ 取得失敗：{exc}")
            continue

        row = df.loc[df["date"] == prev_date]
        if row.empty:
            continue

        r = row.iloc[0]
        data: dict[str, object] = {}
        for col in _PREV_MARGIN_FIELDS:
            val = r.get(col) if col in r else None
            if val is None or pd.isna(val):
                data[col] = None
            elif col == "short_margin_ratio":
                data[col] = float(val)
            else:
                data[col] = int(val)
        updates.append((symbol, prev_date, data))

    n_updated = update_prev_day_margin_batch(config.database_url, updates)
    print(
        f"  D-1 ({prev_date}) 融資融券修正：更新 {n_updated} 筆，"
        f"取得 {len(updates)} 筆，失敗 {n_failed} 筆"
    )


def _fetch_and_upsert_market_daily(
    session: requests.Session, date: dt.date, config: AppConfig
) -> None:
    """Fetch TAIEX OHLC, volume, foreign net, margin and upsert to market_daily."""
    market_data: dict = {}

    try:
        ohlc_map = fetch_twse_taiex_ohlc(session, date)
        if date in ohlc_map:
            market_data.update(ohlc_map[date])
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"  大盤 OHLC 取得失敗：{exc}")

    try:
        vol_map = fetch_twse_market_volume(session, date)
        if date in vol_map:
            market_data["total_volume"] = vol_map[date]
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"  大盤成交金額取得失敗：{exc}")

    try:
        foreign = fetch_twse_foreign_net(session, date)
        if foreign is not None:
            market_data["foreign_net"] = foreign
    except requests.RequestException as exc:
        print(f"  大盤外資買賣超取得失敗：{exc}")

    prev_margin_balance: int | None = None
    try:
        margin = fetch_twse_market_margin(session, date)
        if margin:
            prev_margin_balance = margin.pop("prev_margin_balance", None)
            market_data.update(margin)
    except requests.RequestException as exc:
        print(f"  大盤融資餘額取得失敗：{exc}")

    if prev_margin_balance is not None:
        try:
            result = correct_prev_margin_balance(
                config.database_url, date, prev_margin_balance
            )
            if result is not None:
                prev_date, old_balance, new_balance, old_change, new_change = result
                old_bal_str = "NULL" if old_balance is None else f"{old_balance:,}"
                old_chg_str = "NULL" if old_change is None else f"{old_change:,}"
                new_chg_str = "NULL" if new_change is None else f"{new_change:,}"
                print(
                    f"  大盤 D-1 ({prev_date}) margin_balance 修正："
                    f"舊={old_bal_str} → 新={new_balance:,}；"
                    f"margin_balance_change：舊={old_chg_str} → 新={new_chg_str} "
                    "(TWSE 事後修正)"
                )
        except Exception as exc:
            print(f"  大盤 D-1 margin_balance 校正失敗：{exc}")

    if market_data:
        upsert_market_daily(config.database_url, date, market_data)
        print(f"  大盤行情已寫入 market_daily ({date})")


def _run_for_date_no_write(
    session: requests.Session,
    date: dt.date,
    holdings: pd.DataFrame,
    sheet_names: set[str],
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
    config: AppConfig,
    today: dt.date,
    issued_shares: dict[str, int] | None = None,
    margin_cache: dict[str, dict[dt.date, dict]] | None = None,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None = None,
    name_map: dict[str, str] | None = None,
) -> pd.DataFrame | None:
    """Process data for a single date, return DataFrame without writing.

    Returns None if no valid data (weekend, market closed, etc.).
    """
    sheet_name = date.isoformat()
    print(f"開始處理日期 {sheet_name}")

    if date.weekday() >= 5:
        print(f"{sheet_name} 週末休市，略過")
        return None

    try:
        twse_3insti = _fetch_twse_3insti(session, date)
    except DataUnavailableError as exc:
        print(f"{sheet_name} TWSE 資料尚未公告或取得失敗：{exc}")
        twse_3insti = pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"])
    except requests.RequestException as exc:
        print(f"{sheet_name} TWSE 網路連線失敗：{exc}")
        return None

    twse_day_all = None
    twse_day_all_date = None
    if date == today:
        try:
            twse_day_all_raw, twse_day_all_date = fetch_twse_stock_day_all(session)
            if twse_day_all_date is None:
                print(f"{sheet_name} TWSE STOCK_DAY_ALL 無法解析日期，略過使用")
            elif twse_day_all_date != date:
                print(f"{sheet_name} TWSE STOCK_DAY_ALL 日期不匹配：{twse_day_all_date} != {date}")
            else:
                twse_day_all = prepare_twse_day_all(twse_day_all_raw)
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TWSE STOCK_DAY_ALL 取得失敗：{exc}")

    twse_mi_index = None
    twse_mi_index_date = None
    try:
        twse_mi_index_raw, twse_mi_index_date = fetch_twse_mi_index(session, date)
        if twse_mi_index_date is None and not twse_mi_index_raw.empty and date == today:
            twse_mi_index_date = date
        if twse_mi_index_date == date:
            twse_mi_index = prepare_twse_mi_index(twse_mi_index_raw)
        elif twse_mi_index_date is not None:
            print(f"{sheet_name} TWSE MI_INDEX 日期不匹配：{twse_mi_index_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{sheet_name} TWSE MI_INDEX 取得失敗：{exc}")

    twse_confirmed = (
        (twse_day_all_date == date)
        or (twse_mi_index_date == date)
        or (not twse_3insti.empty)
    )
    if not twse_confirmed:
        print(f"{sheet_name} TWSE 資料不足，視為休市，略過")
        return None

    try:
        tpex_quotes, tpex_quotes_date, tpex_3insti, tpex_3insti_date = _fetch_tpex_sources(
            session, date
        )
        if tpex_quotes_date and tpex_quotes_date != date:
            print(f"{sheet_name} TPEX 日行情日期不匹配：{tpex_quotes_date} != {date}")
        if tpex_3insti_date and tpex_3insti_date != date:
            print(f"{sheet_name} TPEX 三大法人日期不匹配：{tpex_3insti_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{sheet_name} TPEX 資料取得失敗：{exc}")
        tpex_quotes = None
        tpex_3insti = None

    if tpex_quotes is None:
        tpex_quotes = pd.DataFrame(
            columns=["symbol", "name", "open", "close", "high", "low", "volume"],
        )
    if tpex_3insti is None:
        tpex_3insti = pd.DataFrame(
            columns=["symbol", "name", "foreign_net", "trust_net", "dealer_net"],
        )

    twse_margin = None
    tpex_margin = None
    if margin_cache is not None:
        pass
    elif date == today:
        try:
            twse_margin_raw, twse_margin_date = fetch_twse_margin(session, date)
            if twse_margin_date == date:
                twse_margin = prepare_twse_margin(twse_margin_raw)
        except (DataUnavailableError, requests.RequestException):
            pass
        try:
            tpex_margin_raw, tpex_margin_date = fetch_tpex_margin_v2(session, date)
            if tpex_margin_date is None or tpex_margin_date == date:
                tpex_margin = prepare_tpex_margin_v2(tpex_margin_raw)
        except (DataUnavailableError, requests.RequestException):
            pass
        if tpex_margin is None:
            try:
                tpex_margin_raw, tpex_margin_date = fetch_tpex_margin(session)
                if tpex_margin_date is None or tpex_margin_date == date:
                    tpex_margin = prepare_tpex_margin(tpex_margin_raw)
            except (DataUnavailableError, requests.RequestException):
                pass

    if holding_pct_cache is None:
        holding_pct_cache = {}

    # 與 _run_for_date 一致：三大法人來源健康度，逐檔跳過用（融資融券非必要，不納入）。
    twse_insti_ok = not twse_3insti.empty
    tpex_insti_ok = not tpex_3insti.empty

    output_df = _build_daily_rows(
        session=session,
        date=date,
        holdings=holdings,
        twse_3insti=twse_3insti,
        twse_day_all=twse_day_all,
        twse_mi_index=twse_mi_index,
        tpex_quotes=tpex_quotes,
        tpex_3insti=tpex_3insti,
        twse_month_cache=twse_month_cache,
        issued_shares=issued_shares,
        twse_margin=twse_margin,
        tpex_margin=tpex_margin,
        margin_cache=margin_cache,
        holding_pct_cache=holding_pct_cache,
        name_map=name_map,
        twse_insti_ok=twse_insti_ok,
        tpex_insti_ok=tpex_insti_ok,
    )

    if output_df.empty:
        print(f"{sheet_name} 找不到任何成份股資料。")
        return None

    if output_df["close"].isna().all():
        print(f"{sheet_name} 當天價格資料尚未公告。")
        return None

    print(f"{sheet_name} 取得 {len(output_df)} 筆資料")
    return output_df


def main() -> None:
    """Main entry point."""
    load_dotenv()
    config = AppConfig.from_env()
    args = _parse_args()
    today = dt.datetime.now(TAIPEI_TZ).date()
    target_date = _parse_date(args.date) if args.date else today

    if not config.use_db or not config.database_url:
        print("錯誤：需設定 USE_DB=true 和 DATABASE_URL")
        return

    with _phase("DB 連線與 schema"):
        pool = get_pool(config.database_url)
        init_schema(pool)

    try:
        _main_inner(config, args, today, target_date)
    finally:
        close_pool()


def _main_inner(
    config: AppConfig,
    args: argparse.Namespace,
    today: dt.date,
    target_date: dt.date,
) -> None:
    """Inner main logic for RawData."""
    db_url = config.database_url

    # 休市開關：只擋純 daily 模式（排程用）；讀不到一律 fail-open 照常執行
    if _is_daily_mode(args):
        try:
            with _phase("休市檢查"):
                value = get_config_value(db_url, "is_trading_day")
        except psycopg.Error as exc:
            print(f"警告：讀取 config.is_trading_day 失敗（{exc}），視為交易日照常執行")
        else:
            if not _parse_trading_day(value):
                print("config.is_trading_day = false，今日休市，結束執行")
                return

    # build_session 對 www.twse.com.tw 加最小請求間隔，避免限流回空殼被誤判成沒資料。
    session = build_session()

    # --update-shares mode
    if args.update_shares:
        _update_shares_command(session, config)
        return

    # --dahu mode：只更新大戶持股佔比，其他資料不更新
    if args.dahu:
        _dahu_command(session, config, args, today)
        return

    # --backfill-limits mode：只回補漲跌停兩欄
    if args.backfill_limits:
        _backfill_limits_command(session, config, args)
        return

    # --backfill-stocks mode
    if args.backfill_stocks:
        if not args.backfill_start or not args.backfill_end:
            print("錯誤：--backfill-stocks 需搭配 --backfill-start 和 --backfill-end")
            return

        stock_list = [s.strip() for s in args.backfill_stocks.split(",") if s.strip()]
        if not stock_list:
            print("錯誤：--backfill-stocks 未指定任何股票代號")
            return

        # 市場別查 DB（CLI 只給代號）；查不到的留 None，退回既有 fallback 行為。
        market_types = load_market_types(db_url)
        stocks_holdings = pd.DataFrame([
            {"symbol": s, "name": "", "market_type": market_types.get(s)}
            for s in stock_list
        ])
        start_date = _parse_date(args.backfill_start)
        end_date = _parse_date(args.backfill_end)
        backfill_dates = _build_date_range(start_date, end_date)
        print(
            f"回補特定股票 {','.join(stock_list)}"
            f" ({len(backfill_dates)} 天：{start_date} ~ {end_date})"
        )

        print("載入發行股數...")
        issued_shares = _get_issued_shares(session, config)
        print("載入股票名稱...")
        name_map = load_stock_names(db_url)
        twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame] = {}

        margin_cache = _prefetch_margin_cache(session, stocks_holdings, start_date, end_date)
        holding_pct_cache = _prefetch_holding_pct_cache(
            session, stocks_holdings, start_date, end_date,
        )

        sheet_names = set()  # 不需 dedup（force 模式忽略，沒 force 也沒 skip 邏輯）
        any_written = False
        for date in backfill_dates:
            if _run_for_date(
                session, date, stocks_holdings, sheet_names, twse_month_cache,
                config, today, skip_existing=False,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
                write_market_daily=False,
            ):
                any_written = True
        # 至少寫入一天時才修正 backfill 左邊界前一天的 margin/short（範圍內已是 MoneyDJ
        # 修正版，但實際左邊界前一天不在 prefetch 範圍內，可能仍是 provisional）
        # 使用 backfill_dates[0]（_build_date_range 已正規化 reversed/one-sided 輸入）
        if any_written:
            _refresh_prev_day_margin(session, stocks_holdings, backfill_dates[0], config)
        return

    # Load enabled stocks from DB
    with _phase("載入啟用個股"):
        enabled_rows = get_enabled_stocks(db_url)
    if not enabled_rows:
        print("錯誤：資料庫中無啟用的股票（stocks.enabled = TRUE）")
        return

    name_map = {r[0]: r[1] for r in enabled_rows}
    holdings = pd.DataFrame([
        {"symbol": r[0], "name": r[1], "market_type": r[4]} for r in enabled_rows
    ])

    with _phase("載入發行股數"):
        issued_shares = _get_issued_shares(session, config)
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame] = {}

    # Backfill mode
    if args.backfill_start or args.backfill_end:
        if args.backfill_start:
            start_date = _parse_date(args.backfill_start)
        else:
            start_date = target_date
        end_date = _parse_date(args.backfill_end) if args.backfill_end else target_date
        backfill_dates = _build_date_range(start_date, end_date)
        force_msg = "（強制覆蓋）" if args.force else ""
        print(f"回補 {len(backfill_dates)} 天：{backfill_dates[0]} ~ {backfill_dates[-1]}{force_msg}")

        margin_cache = _prefetch_margin_cache(session, holdings, start_date, end_date)
        holding_pct_cache = _prefetch_holding_pct_cache(session, holdings, start_date, end_date)

        sheet_names = set()
        any_written = False
        for date in backfill_dates:
            if _run_for_date(
                session, date, holdings, sheet_names, twse_month_cache,
                config, today,
                skip_existing=not args.force,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
            ):
                any_written = True
        # 至少寫入一天時才修正 backfill 左邊界前一天的 margin/short（範圍內已是 MoneyDJ
        # 修正版，但實際左邊界前一天不在 prefetch 範圍內，可能仍是 provisional）
        # 使用 backfill_dates[0]（_build_date_range 已正規化 reversed/one-sided 輸入）
        if any_written:
            _refresh_prev_day_margin(session, holdings, backfill_dates[0], config)
        return

    # Single date mode (today / --date)
    sheet_names = set()
    _run_for_date(
        session, target_date, holdings, sheet_names, twse_month_cache,
        config, today,
        skip_existing=False,
        issued_shares=issued_shares,
        name_map=name_map,
    )


if __name__ == "__main__":
    main()
