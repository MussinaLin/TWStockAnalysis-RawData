"""Main entry point for TW Stock RawData fetcher."""

from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

from .config import AppConfig
from .db import close_pool, get_pool, init_schema
from .db_utils import (
    correct_prev_margin_balance,
    get_enabled_stocks,
    load_stock_names,
    load_stock_shares,
    upsert_daily_raw,
    upsert_market_daily,
    upsert_stock_shares,
)
from .prepare import (
    prepare_moneydj_holding_pct,
    prepare_moneydj_margin,
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
from .sources import (
    DataUnavailableError,
    fetch_moneydj_holding_pct,
    fetch_moneydj_margin,
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
        "--update-shares", action="store_true",
        help="更新發行股數至資料庫",
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
) -> pd.DataFrame:
    """Build raw daily rows for stock_daily_raw (no indicators/statistics)."""
    rows: list[dict] = []
    total = len(holdings)
    if name_map is None:
        name_map = {}

    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        name = name_map.get(symbol, "")
        display_name = f" {name}" if name else ""
        print(f"{date.isoformat()} {idx + 1}/{total} {symbol}{display_name}")

        ohlcv = _fetch_ohlcv_with_fallback(
            session, date, symbol, twse_day_all, twse_mi_index,
            tpex_quotes, twse_month_cache,
        )
        open_price, close_price, high_price, low_price, volume = ohlcv

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
        })

    return pd.DataFrame(rows)


def _fetch_ohlcv_with_fallback(
    session: requests.Session,
    date: dt.date,
    symbol: str,
    twse_day_all: pd.DataFrame | None,
    twse_mi_index: pd.DataFrame | None,
    tpex_quotes: pd.DataFrame,
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
) -> tuple[float | None, float | None, float | None, float | None, int | None]:
    """Fetch OHLCV data with fallback chain: DAY_ALL -> STOCK_DAY -> MI_INDEX -> TPEX."""
    open_price = close_price = high_price = low_price = volume = None

    # Try TWSE STOCK_DAY_ALL
    if twse_day_all is not None:
        row = twse_day_all.loc[twse_day_all["symbol"] == symbol]
        if not row.empty:
            open_price = row.iloc[0]["open"]
            close_price = row.iloc[0]["close"]
            high_price = row.iloc[0].get("high")
            low_price = row.iloc[0].get("low")
            volume = row.iloc[0].get("volume")

    # Try TWSE STOCK_DAY (monthly)
    if any(v is None for v in [open_price, close_price, high_price, low_price, volume]):
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

    # Try TWSE MI_INDEX
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

    # Try TPEX quotes
    if open_price is None and close_price is None:
        row = tpex_quotes.loc[tpex_quotes["symbol"] == symbol]
        if not row.empty:
            open_price = row.iloc[0]["open"]
            close_price = row.iloc[0]["close"]
            high_price = row.iloc[0].get("high")
            low_price = row.iloc[0].get("low")
            volume = row.iloc[0].get("volume")

    return open_price, close_price, high_price, low_price, volume


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
) -> bool:
    """Process data for a single date."""
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
        # Use OpenAPI for today's data (all stocks at once)
        try:
            twse_margin_raw, twse_margin_date = fetch_twse_margin(session)
            # TWSE OpenAPI doesn't return date, assume today when None
            if twse_margin_date is None or twse_margin_date == date:
                twse_margin = prepare_twse_margin(twse_margin_raw)
            else:
                print(f"{sheet_name} TWSE 融資融券日期不匹配：{twse_margin_date} != {date}")
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TWSE 融資融券取得失敗：{exc}")

        # Try V2 first (supports date parameter), fallback to OpenAPI
        try:
            tpex_margin_raw, tpex_margin_date = fetch_tpex_margin_v2(session, date)
            if tpex_margin_date is None or tpex_margin_date == date:
                tpex_margin = prepare_tpex_margin_v2(tpex_margin_raw)
            else:
                print(f"{sheet_name} TPEX V2 融資融券日期不匹配：{tpex_margin_date} != {date}")
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"{sheet_name} TPEX V2 融資融券取得失敗：{exc}")

        if tpex_margin is None:
            try:
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
    if holding_pct_cache is None:
        holding_pct_cache = {}
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

    # Build daily data
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
    )

    if output_df.empty:
        print(f"{sheet_name} 找不到任何成份股資料。")
        return False

    if output_df["close"].isna().all():
        print(f"{sheet_name} 當天價格資料尚未公告，未寫入。")
        return False

    sheet_names.add(sheet_name)

    upsert_daily_raw(config.database_url, date, output_df)

    # Fetch and upsert market daily data (大盤行情)
    _fetch_and_upsert_market_daily(session, date, config)

    return True


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
            twse_margin_raw, twse_margin_date = fetch_twse_margin(session)
            if twse_margin_date is None or twse_margin_date == date:
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

    session = requests.Session()
    session.headers.update({"User-Agent": "tw-stock-rawdata/0.1"})

    # --update-shares mode
    if args.update_shares:
        _update_shares_command(session, config)
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

        stocks_holdings = pd.DataFrame([{"symbol": s, "name": ""} for s in stock_list])
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
        for date in backfill_dates:
            _run_for_date(
                session, date, stocks_holdings, sheet_names, twse_month_cache,
                config, today, skip_existing=False,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
            )
        return

    # Load enabled stocks from DB
    enabled_rows = get_enabled_stocks(db_url)
    if not enabled_rows:
        print("錯誤：資料庫中無啟用的股票（stocks.enabled = TRUE）")
        return

    name_map = {r[0]: r[1] for r in enabled_rows}
    holdings = pd.DataFrame([
        {"symbol": r[0], "name": r[1]} for r in enabled_rows
    ])

    print("載入發行股數...")
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
        for date in backfill_dates:
            _run_for_date(
                session, date, holdings, sheet_names, twse_month_cache,
                config, today,
                skip_existing=not args.force,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
            )
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
