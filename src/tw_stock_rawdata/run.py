"""Main entry point for TW Stock RawData fetcher."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import time
from decimal import Decimal
from typing import NamedTuple, Protocol
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
    update_disposition_batch,
    update_prev_day_margin_batch,
    update_price_limits_batch,
    upsert_daily_raw,
    upsert_holder_percent,
    upsert_market_daily,
    upsert_stock_shares,
)
from .prepare import (
    expand_tpex_stock_day,
    expand_twse_stock_day,
    prepare_disposition,
    prepare_moneydj_holding_pct,
    prepare_moneydj_insti,
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
    fetch_tpex_disposition,
    fetch_tpex_margin,
    fetch_tpex_margin_v2,
    fetch_tpex_stock_day,
    fetch_twse_company_basic,
    fetch_twse_disposition,
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
        or args.backfill_disposition
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
        "--backfill-disposition", action="store_true",
        help="只回補 is_disposition / disposition_match_minutes"
             "（需搭配 --backfill-start / --backfill-end，可選 --backfill-stocks）",
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


# 處置公告查詢窗口要往前推的天數。處置期間最長 10 個營業日，公告日又早於期間起日，
# 只查當日會漏掉「正處在處置期間中段」的個股（公告可能是兩週前發的）。45 個日曆日
# 足以覆蓋，且端點吃區間查詢，窗口拉長不增加請求數。
_DISPOSITION_LOOKBACK_DAYS = 45

# 單次處置公告查詢的最長窗口（日曆日，≈ 6 個月）。設計階段實測到 6 個月為止
# 都沒有截斷（314/574 列），再長就沒驗證過了。而截斷的後果不是「缺 NULL」而是
# **寫錯**：漏掉的公告會讓 `DispositionData.resolve` 走到「該市場已成功取得」
# 的分支回 (False, 0)，用 COALESCE 覆蓋掉原本正確的處置註記（見 CLAUDE.md）。
# 回補 3 年切成 7 段、兩市場共 14 發，相對於整段約 50 發的預算可忽略。
_DISPOSITION_MAX_WINDOW_DAYS = 183


class DispositionData(NamedTuple):
    """處置名單：展開好的 date -> symbol -> 撮合分鐘數，加上各市場的取得狀態。

    ok_markets 記錄哪些市場的名單確實抓到了。沒抓到的市場不能把該市場個股寫成
    is_disposition=FALSE —— 那是在宣稱「已確認非處置」，實際上只是沒查到。
    """

    by_date: dict[dt.date, dict[str, int | None]]
    ok_markets: frozenset[str]

    def resolve(
        self,
        date: dt.date,
        symbol: str,
        market_type: str | None,
    ) -> tuple[bool | None, int | None]:
        """回傳該檔該日的 (is_disposition, disposition_match_minutes)。

        非處置回 (False, 0) 而非 (False, None)：upsert 的 COALESCE 不以 NULL 覆寫舊值，
        寫 None 會讓前一段處置留下的分鐘數永遠清不掉（見 db.py 的欄位註解）。
        名單未取得回 (None, None)，交給 COALESCE 保留 DB 既有值。
        """
        day = self.by_date.get(date)
        if day is not None and symbol in day:
            minutes = day[symbol]
            # 撮合頻率解析不到時同樣寫 0 而非 NULL（理由同上）。旁邊有
            # is_disposition=TRUE，0 不會被誤讀成「非處置」。
            return True, 0 if minutes is None else minutes
        if market_type in self.ok_markets:
            return False, 0
        # 市場別未知（--backfill-stocks 的個股可能查不到 market_type）時，
        # 只有兩市場都成功才敢斷言「不在名單內」。
        if market_type is None and len(self.ok_markets) == 2:
            return False, 0
        return None, None


def _disposition_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """把 [start, end] 切成連續、不重疊、每段 ≤ _DISPOSITION_MAX_WINDOW_DAYS 的窗口。"""
    windows: list[tuple[dt.date, dt.date]] = []
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=_DISPOSITION_MAX_WINDOW_DAYS - 1), end)
        windows.append((cur, stop))
        cur = stop + dt.timedelta(days=1)
    return windows


def _fetch_market_disposition_frames(
    session: requests.Session,
    market: str,
    fetcher,
    windows: list[tuple[dt.date, dt.date]],
) -> tuple[list[pd.DataFrame], bool]:
    """抓單一市場所有窗口的處置公告，回傳 (frames, market_ok)。

    market_ok 採「全部窗口都成功才算 ok」：只要有一段沒拿到，該市場當天就可能
    有公告沒被看到，這時若還把該市場標成 ok，沒查到的個股會被 resolve() 寫成
    「已確認非處置」(False, 0) 而不是 NULL —— 正是 CLAUDE.md 那段要防的事。
    已成功窗口的 frames 仍然回傳：它們只產生 is_disposition=TRUE 的註記，
    那些是確實看到公告才有的，永遠是對的。
    """
    frames: list[pd.DataFrame] = []
    market_ok = True
    for win_start, win_end in windows:
        try:
            frames.append(prepare_disposition(fetcher(session, win_start, win_end)))
        except (DataUnavailableError, requests.RequestException) as exc:
            print(f"處置股名單（{market} {win_start} ~ {win_end}）取得失敗：{exc}")
            market_ok = False
    return frames, market_ok


def _merge_disposition_minutes(slot: dict[str, int | None], symbol: str, minutes: int | None) -> None:
    """把一筆公告併進當日的 symbol -> 撮合分鐘數，同檔多筆取最小值。

    最小值＝當日實際生效的最嚴格撮合頻率（處置期間可能重疊）。
    minutes 為 None（有處置但分鐘數不明）時不覆寫已經取到的數值。
    """
    if symbol not in slot:
        slot[symbol] = minutes
    elif minutes is not None and (slot[symbol] is None or minutes < slot[symbol]):
        slot[symbol] = minutes


def _expand_disposition_frames(
    frames: list[pd.DataFrame],
    by_date: dict[dt.date, dict[str, int | None]],
    start: dt.date,
    end: dt.date,
) -> None:
    """把公告的 [start_date, end_date] 展開成逐日註記，就地寫進 by_date。

    展開後只保留 [start, end] 內的日期——查詢窗口往前推了 45 天，抓回來的
    公告會涵蓋區間外的日子。
    """
    for frame in frames:
        for _, row in frame.iterrows():
            symbol = row["symbol"]
            minutes = row["match_minutes"]
            minutes = None if minutes is None or pd.isna(minutes) else int(minutes)
            day = max(row["start_date"], start)
            last = min(row["end_date"], end)
            while day <= last:
                _merge_disposition_minutes(by_date.setdefault(day, {}), symbol, minutes)
                day += dt.timedelta(days=1)


def _print_disposition_summary(
    ok_markets: set[str],
    by_date: dict[dt.date, dict[str, int | None]],
    n_windows: int,
    start: dt.date,
    end: dt.date,
) -> None:
    """印出處置名單取得結果；兩市場皆失敗時明說該欄位本次不寫入。"""
    if not ok_markets:
        print("處置股名單：兩市場皆取得失敗，該欄位本次不寫入（保留 DB 既有值）")
        return
    n_days = len(by_date)
    n_symbols = len({s for day in by_date.values() for s in day})
    print(
        f"處置股名單：{'／'.join(sorted(ok_markets))} 取得成功"
        f"（每市場 {n_windows} 段查詢），"
        f"{start} ~ {end} 內 {n_days} 天 / {n_symbols} 檔標的在處置期間"
    )


def _fetch_disposition(
    session: requests.Session,
    start: dt.date,
    end: dt.date,
) -> DispositionData:
    """抓兩市場處置公告並展開成 date -> symbol -> 撮合分鐘數。

    查詢窗口自動往前推 _DISPOSITION_LOOKBACK_DAYS（見常數說明），展開後只保留
    [start, end] 內的日期。任一市場失敗只影響該市場，不中斷另一邊。

    長區間會切成 ≤ _DISPOSITION_MAX_WINDOW_DAYS 的多個窗口分別查、再合併，
    避免踩到未驗證過的截斷行為（見該常數說明）。合併時 `ok_markets` 採
    **全部窗口都成功才算 ok**：只要有一段沒拿到，該市場當天就可能有公告沒被
    看到，這時若還把該市場標成 ok，沒查到的個股會被寫成「已確認非處置」
    (False, 0) 而不是 NULL —— 正是 CLAUDE.md 那段要防的事。
    已成功窗口的資料仍然保留：它們只會產生 is_disposition=TRUE 的註記，那些是
    確實看到公告才有的，永遠是對的。

    同一 (symbol, date) 可能對應多筆公告（例如處置期間重疊），取最小分鐘數
    ＝當日實際生效的最嚴格撮合頻率。
    """
    fetch_start = start - dt.timedelta(days=_DISPOSITION_LOOKBACK_DAYS)
    windows = _disposition_windows(fetch_start, end)
    if not windows:
        # start > end（呼叫端沒正規化）：一段都查不到就不能宣稱任何市場 ok，
        # 否則 resolve() 會把整段寫成「已確認非處置」(False, 0)。
        print(f"處置股名單：查詢區間無效（{fetch_start} > {end}），該欄位本次不寫入")
        return DispositionData({}, frozenset())

    by_date: dict[dt.date, dict[str, int | None]] = {}
    ok_markets: set[str] = set()

    for market, fetcher in (
        ("twse", fetch_twse_disposition),
        ("tpex", fetch_tpex_disposition),
    ):
        frames, market_ok = _fetch_market_disposition_frames(
            session, market, fetcher, windows
        )
        if market_ok:
            ok_markets.add(market)
        _expand_disposition_frames(frames, by_date, start, end)

    _print_disposition_summary(ok_markets, by_date, len(windows), start, end)
    return DispositionData(by_date, frozenset(ok_markets))


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


def _backfill_disposition_command(
    session: requests.Session,
    config: AppConfig,
    args: argparse.Namespace,
) -> None:
    """--backfill-disposition：只回補 is_disposition / disposition_match_minutes。

    與 --backfill-limits 的差別是這裡「支援」--backfill-stocks：處置是逐檔註記，
    限縮到特定股票不會有副作用（漲跌停那邊忽略它是因為那條路徑本來就整批算）。
    """
    if args.date:
        print("警告：--backfill-disposition 已啟用，--date 將被忽略")

    if not args.backfill_start or not args.backfill_end:
        print("錯誤：--backfill-disposition 需搭配 --backfill-start 和 --backfill-end")
        return

    dates = _build_date_range(
        _parse_date(args.backfill_start), _parse_date(args.backfill_end)
    )

    only_symbols: set[str] | None = None
    if args.backfill_stocks:
        only_symbols = {s.strip() for s in args.backfill_stocks.split(",") if s.strip()}
        if not only_symbols:
            print("錯誤：--backfill-stocks 未指定任何股票代號")
            return
    scope = f"（限 {len(only_symbols)} 檔）" if only_symbols else ""
    print(f"回補處置股 {len(dates)} 天：{dates[0]} ~ {dates[-1]}{scope}")

    # 端點吃日期區間，整段只查一次公告即可，不必逐日打 API。
    disposition = _fetch_disposition(session, dates[0], dates[-1])
    if not disposition.ok_markets:
        print("錯誤：兩市場處置名單皆取得失敗，不回補")
        return

    market_types = load_market_types(config.database_url)
    total = 0
    for date in dates:
        if date.weekday() >= 5:
            continue
        # 同 --backfill-limits：先問 DB 該日有哪些 symbol，只更新已存在的列。
        existing = load_symbols_for_date(config.database_url, date)
        if only_symbols is not None:
            existing &= only_symbols
        if not existing:
            print(f"{date.isoformat()} DB 無該日資料，略過")
            continue

        updates: list[tuple[str, dt.date, bool, int]] = []
        for symbol in sorted(existing):
            flag, minutes = disposition.resolve(date, symbol, market_types.get(symbol))
            if flag is None:
                continue
            updates.append((symbol, date, flag, minutes))
        if not updates:
            print(f"{date.isoformat()} 無可回補資料")
            continue

        n_updated = update_disposition_batch(config.database_url, updates)
        total += n_updated
        n_disposed = sum(1 for u in updates if u[2])
        print(f"{date.isoformat()} 更新 {n_updated} 檔（其中處置中 {n_disposed} 檔）")

    print(f"處置股回補完成，共更新 {total} 列")


class RowSourceProvider(Protocol):
    """`_build_daily_rows` 取得「該檔該日」原始資料的唯一管道。

    有兩種實作，差別只在資料從哪來，組列邏輯共用同一條路徑（不產生雙軌）：
    - `BatchSourceProvider`：全市場批次來源（daily / 全市場 backfill）
    - `PerSymbolRangeProvider`：per-stock 區間來源（`--backfill-stocks`）

    **單位契約：`ohlcv().volume` 與 `insti()` 回傳的三個值一律是「股」。**
    per-symbol 來源拿到的是「張」，一律在 provider 內 × 1000 還原，讓
    `_build_daily_rows` 尾端的 `// 1000` 不需要任何 per-mode 分支。
    """

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult: ...

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]: ...

    def insti_ok(self, symbol: str, market_type: str | None) -> bool: ...


class BatchSourceProvider:
    """全市場批次來源的 provider（daily / 全市場 backfill 用）。

    純委派給既有的自由函式，**零行為變更**。自由函式刻意保留在模組層級，
    既有的 `test_run_ohlcv_source_order` / `test_run_stock_sources` 因此不需改動。
    """

    def __init__(
        self,
        *,
        session: requests.Session | None,
        twse_3insti: pd.DataFrame,
        twse_day_all: pd.DataFrame | None,
        twse_mi_index: pd.DataFrame | None,
        tpex_quotes: pd.DataFrame,
        tpex_3insti: pd.DataFrame,
        twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
        twse_insti_ok: bool = True,
        tpex_insti_ok: bool = True,
    ) -> None:
        self._session = session
        self._twse_3insti = twse_3insti
        self._twse_day_all = twse_day_all
        self._twse_mi_index = twse_mi_index
        self._tpex_quotes = tpex_quotes
        self._tpex_3insti = tpex_3insti
        self._twse_month_cache = twse_month_cache
        self._twse_insti_ok = twse_insti_ok
        self._tpex_insti_ok = tpex_insti_ok

        if not tpex_quotes.empty and "symbol" in tpex_quotes.columns:
            self._tpex_symbols = set(tpex_quotes["symbol"].astype(str).str.strip())
        else:
            self._tpex_symbols = set()

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult:
        # 全部具名傳遞：`twse_mi_index` 與 `tpex_quotes` 的順序若對調不會報錯，
        # 但會改變 fallback 鏈的行為（包含 change 由誰供應）。
        return _fetch_ohlcv_with_fallback(
            session=self._session,
            date=date,
            symbol=symbol,
            twse_day_all=self._twse_day_all,
            twse_mi_index=self._twse_mi_index,
            tpex_quotes=self._tpex_quotes,
            twse_month_cache=self._twse_month_cache,
            market_type=market_type,
        )

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]:
        return _get_institutional_data(symbol, self._twse_3insti, self._tpex_3insti)

    def insti_ok(self, symbol: str, market_type: str | None) -> bool:
        return _stock_sources_ok(
            is_tpex=self._is_tpex(symbol, market_type),
            twse_insti_ok=self._twse_insti_ok,
            tpex_insti_ok=self._tpex_insti_ok,
        )

    def _is_tpex(self, symbol: str, market_type: str | None) -> bool:
        """batch 模式用「該檔今天有沒有出現在 tpex_quotes」判定。

        這反映的是「這檔今天的價格是誰供應的」，必須跟著當日實際來源走，
        刻意**不**用 `stocks.market_type`（那反映的是「本質上屬於哪個市場」，
        用途不同，見 `_build_daily_rows` docstring）。

        只服務本類別的 `insti_ok()`，不是 `RowSourceProvider` 介面的一員——
        另一個 provider 用完全不同的方式回答 `insti_ok()`，沒有「市場別」這個
        中間概念。
        """
        return symbol in self._tpex_symbols


class PerSymbolRangeProvider:
    """per-stock 區間來源的 provider（`--backfill-stocks` 用）。

    建構時把整段區間的資料一次抓完，之後全部是記憶體查表、零 HTTP：
    - OHLCV + change：逐月抓 TWSE STOCK_DAY / TPEX 個股月表（每檔每月 1 發）
    - 三大法人 + 外資/法人持股佔比：MoneyDJ zcl，**整段只打 1 發**——兩者從同一次
      fetch 回來的同一份 HTML 解析出來（見 `build()`），持股佔比因此零額外請求。

    單位契約與 BatchSourceProvider 相同：`ohlcv().volume` 與 `insti()` 一律回「股」。
    per-symbol 來源給的是「張」，已在 `expand_tpex_stock_day` /
    `prepare_moneydj_insti` 內 × 1000 還原。
    """

    def __init__(
        self,
        *,
        ohlcv_by_symbol: dict[str, dict[dt.date, OhlcvResult]],
        insti_by_symbol: dict[str, dict[dt.date, tuple[int | None, int | None, int | None]]],
        insti_ok_by_symbol: dict[str, bool],
        resolved_market_types: dict[str, str],
        holding_pct_cache: dict[str, dict[dt.date, dict]],
        failed_months_by_symbol: dict[str, list[dt.date]] | None = None,
    ) -> None:
        self._ohlcv = ohlcv_by_symbol
        self._insti = insti_by_symbol
        self._insti_ok = insti_ok_by_symbol
        self.resolved_market_types = resolved_market_types
        # 給收尾摘要用（見 main() 的 --backfill-stocks 分支）：整段跑完後，
        # 逐日輸出已經捲過幾百行，操作者需要一行看得到哪些檔／哪些月沒寫進去。
        self.failed_months_by_symbol = failed_months_by_symbol or {}
        # 外資/法人持股佔比：與三大法人在 build() 內同一次 MoneyDJ zcl fetch 解析出來。
        # 形狀對齊 `_prefetch_holding_pct_cache` 的回傳值（symbol -> date -> dict），
        # 呼叫端直接拿去當 `_build_daily_rows` 的 holding_pct_cache 用即可——
        # **不要**再呼叫 `_prefetch_holding_pct_cache`，那會是對同一頁的第二次請求。
        self.holding_pct_cache = holding_pct_cache

    @classmethod
    def build(
        cls,
        session: requests.Session,
        symbols: list[str],
        market_types: dict[str, str],
        start: dt.date,
        end: dt.date,
    ) -> PerSymbolRangeProvider:
        """對每檔預取整段的 OHLCV、三大法人與持股佔比。

        先抓 MoneyDJ（整段 1 發，同一份 HTML 同時解析出三大法人與持股佔比），
        再用三大法人的日期集合當「該檔哪些日子有交易」的外部證據，交給
        `_prefetch_symbol_ohlcv` 區分「月表回空」是沒交易還是被限流。順序不可對調。

        MoneyDJ 失敗的個股**直接跳過月表**：該檔的每一天都不會通過 `insti_ok()`
        gating，抓回來的價格一列也寫不進去；而且 traded_dates 是空的，限流判準
        整個失效（回空一律當成沒交易）。回補 3 年就是白打 36 發沒有判準保護的
        請求——MoneyDJ 會失敗，往往正代表這次連線已經有狀況。
        """
        ohlcv_by_symbol: dict[str, dict[dt.date, OhlcvResult]] = {}
        insti_by_symbol: dict[str, dict[dt.date, tuple]] = {}
        insti_ok_by_symbol: dict[str, bool] = {}
        holding_pct_by_symbol: dict[str, dict[dt.date, dict]] = {}
        failed_months_by_symbol: dict[str, list[dt.date]] = {}
        resolved: dict[str, str] = {}

        total = len(symbols)
        for idx, symbol in enumerate(symbols, start=1):
            print(f"  預取個股區間資料 {idx}/{total} {symbol}")

            # 1) MoneyDJ zcl：三大法人 + 持股佔比（整段 1 發，同一頁 HTML 解析兩份）
            insti_map: dict[dt.date, tuple] = {}
            holding_pct_map: dict[dt.date, dict] = {}
            insti_ok = False
            try:
                raw = fetch_moneydj_holding_pct(session, symbol, start, end)

                insti_df = prepare_moneydj_insti(raw)
                for _, row in insti_df.iterrows():
                    row_date = row["date"]
                    if not isinstance(row_date, dt.date):
                        continue
                    insti_map[row_date] = (
                        row.get("foreign_net"),
                        row.get("trust_net"),
                        row.get("dealer_net"),
                    )

                holding_pct_df = prepare_moneydj_holding_pct(raw)
                for _, row in holding_pct_df.iterrows():
                    row_date = row["date"]
                    if not isinstance(row_date, dt.date):
                        continue
                    holding_pct_map[row_date] = {
                        "foreign_holding_pct": row.get("foreign_holding_pct"),
                        "insti_holding_pct": row.get("insti_holding_pct"),
                    }

                insti_ok = True
            except (DataUnavailableError, requests.RequestException) as exc:
                print(f"    {symbol} MoneyDJ 三大法人取得失敗：{exc}")

            insti_by_symbol[symbol] = insti_map
            insti_ok_by_symbol[symbol] = insti_ok
            holding_pct_by_symbol[symbol] = holding_pct_map

            if not insti_ok:
                # 見 docstring：MoneyDJ 失敗 → 這檔整段都寫不進去，且限流判準失效，
                # 月表不必再打。
                ohlcv_by_symbol[symbol] = {}
                print(f"    {symbol} 三大法人缺漏，整檔跳過（不再抓月表）")
                continue

            # 2) 月表：OHLCV + change（每月 1 發），用 MoneyDJ 日期當限流判準
            fetched = _prefetch_symbol_ohlcv(
                session=session,
                symbol=symbol,
                market_type=market_types.get(symbol),
                start=start,
                end=end,
                traded_dates=set(insti_map),
            )
            ohlcv_by_symbol[symbol] = fetched.by_date
            if fetched.market_type is not None:
                resolved[symbol] = fetched.market_type
            if fetched.failed_months:
                failed_months_by_symbol[symbol] = list(fetched.failed_months)
                months = "、".join(f"{m:%Y-%m}" for m in fetched.failed_months)
                print(f"    ⚠ {symbol} 以下月份判定為取得失敗、未寫入：{months}")

        return cls(
            ohlcv_by_symbol=ohlcv_by_symbol,
            insti_by_symbol=insti_by_symbol,
            insti_ok_by_symbol=insti_ok_by_symbol,
            resolved_market_types=resolved,
            holding_pct_cache=holding_pct_by_symbol,
            failed_months_by_symbol=failed_months_by_symbol,
        )

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult:
        found = self._ohlcv.get(symbol, {}).get(date)
        if found is not None:
            return found
        # 該日不在月表（沒交易 / 停牌 / 該月判定取得失敗）→ 全 None，
        # `_build_daily_rows` 會因為無價格而跳過該檔該日。
        return OhlcvResult(
            open=None, close=None, high=None, low=None, volume=None, change=None
        )

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]:
        """回傳該檔該日的三大法人買賣超（股）。

        與 BatchSourceProvider.insti 不同，這裡**確實**鍵在 date 上 —— per-symbol
        來源一次持有整段區間的資料，date 是查表用的必要參數，不是形式參數。
        """
        return self._insti.get(symbol, {}).get(date, (None, None, None))

    def insti_ok(self, symbol: str, market_type: str | None) -> bool:
        """該檔的 MoneyDJ 是否取得成功。

        整段失敗時該檔**每一天**都不通過 gating，等同整檔跳過不寫——與現行
        「逐檔跳過半套資料」的不變量一致，留待重跑補上。
        """
        return self._insti_ok.get(symbol, False)

    @property
    def insti_failed_symbols(self) -> list[str]:
        """MoneyDJ 整段取得失敗、因而整檔一列都沒寫入的個股（收尾摘要用）。"""
        return [symbol for symbol, ok in self._insti_ok.items() if not ok]


def _to_lots(value):
    """股數轉張數。None 原樣回傳（缺值不推測），NaN 由 // 自然傳遞。"""
    return value // 1000 if value is not None else None


def _insti_total_lots(foreign_lots, trust_lots, dealer_lots):
    """三大法人合計（張）。三者皆缺時回 None，不當成 0。"""
    if foreign_lots is None and trust_lots is None and dealer_lots is None:
        return None
    return (foreign_lots or 0) + (trust_lots or 0) + (dealer_lots or 0)


def _turnover_rate(volume, issued_shares: dict[str, int] | None, symbol: str):
    """成交量 / 發行股數。發行股數缺席或非正數時回 None。

    條件寫成正向的 `shares > 0` 而非 `not (shares <= 0)`：兩者對 NaN 不等價
    （NaN 的 `>` 與 `<=` 同時為 False），取補集會讓 NaN 漏進除法。
    """
    if not issued_shares or volume is None:
        return None
    shares = issued_shares.get(symbol)
    if shares and shares > 0:
        return round(volume / shares, 6)
    return None


def _short_margin_ratio(margin_balance, short_balance):
    """券資比。融資餘額非正數（含缺值 / NaN）時回 None，不做除法。

    條件寫成正向的 `margin_balance > 0`，理由同 _turnover_rate：
    NaN 的 `>` 與 `<=` 同時為 False，寫成補集會讓 NaN 漏進除法算出 NaN。
    """
    if margin_balance is not None and margin_balance > 0 and short_balance is not None:
        return round(short_balance / margin_balance, 6)
    return None


def _resolve_margin_data(
    symbol: str,
    date: dt.date,
    margin_cache: dict[str, dict[dt.date, dict]] | None,
    twse_margin: pd.DataFrame | None,
    tpex_margin: pd.DataFrame | None,
) -> dict:
    """融資融券：優先用逐檔預抓的 cache，沒有才回頭查當日整批。"""
    if margin_cache is not None and symbol in margin_cache and date in margin_cache[symbol]:
        return margin_cache[symbol][date]
    return _get_margin_data(symbol, twse_margin, tpex_margin)


def _resolve_holding_pct(
    symbol: str,
    date: dt.date,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None,
) -> dict:
    """外資 / 法人持股比例：只從逐檔預抓的 cache 取，沒有就回空 dict。"""
    if holding_pct_cache is not None and symbol in holding_pct_cache:
        return holding_pct_cache[symbol].get(date, {})
    return {}


def _build_daily_rows(
    *,
    date: dt.date,
    holdings: pd.DataFrame,
    provider: RowSourceProvider,
    issued_shares: dict[str, int] | None = None,
    twse_margin: pd.DataFrame | None = None,
    tpex_margin: pd.DataFrame | None = None,
    margin_cache: dict[str, dict[dt.date, dict]] | None = None,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None = None,
    name_map: dict[str, str] | None = None,
    disposition: DispositionData | None = None,
) -> pd.DataFrame:
    """Build raw daily rows for stock_daily_raw (no indicators/statistics).

    逐檔跳過（不寫該檔列）條件：
    1. 無價格（open/close 皆 None）：OHLC 來源對該檔失敗或當天無交易。
    2. 該檔市場別的三大法人來源 fetch 失敗（見 _stock_sources_ok）。
    跳過的個股留待重跑 / backfill 補上（搭配 upsert 的 COALESCE）。

    三大法人的 gating 與 OHLCV fallback 是兩個獨立訊號，用途不同、不要互相取代：
    - gating 一律走 `provider.insti_ok()`，本函式不自己判市場別。兩個 provider
      回答的方式完全不同：`BatchSourceProvider` 先用「該檔是否出現在當日
      tpex_quotes」推出該檔今天的價格是誰供應的（見其私有的 `_is_tpex()`），
      再看該市場的三大法人整批有沒有抓成功；`PerSymbolRangeProvider` 則直接回答
      「這檔的 MoneyDJ 整段有沒有抓成功」，根本沒有市場別這個中間概念。
    - OHLCV fallback 用 holdings 的 `stocks.market_type`（見
      `_fetch_ohlcv_with_fallback`），反映「這檔本質上屬於哪個市場」，不能依賴
      當日 tpex_quotes 是否抓成功 —— 否則 TPEX 整批失敗時，上櫃股又會退回去打
      註定沒資料的 TWSE 月表。
    """
    rows: list[dict] = []
    total = len(holdings)
    skipped = 0
    if name_map is None:
        name_map = {}

    for idx, item in holdings.iterrows():
        symbol = str(item["symbol"]).strip()
        name = name_map.get(symbol, "")
        display_name = f" {name}" if name else ""
        print(f"{date.isoformat()} {idx + 1}/{total} {symbol}{display_name}")

        market_type = _row_market_type(item)

        ohlcv = provider.ohlcv(symbol, date, market_type)
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
        if not provider.insti_ok(symbol, market_type):
            skipped += 1
            continue

        # 處置註記。市場別用 stocks.market_type（該檔本質屬於哪個市場），
        # 不用當日 tpex_quotes —— 後者反映的是「今天價格誰供應的」，處置名單
        # 是否可信取決於該市場的公告有沒有抓到，兩者語意不同。
        if disposition is None:
            is_disposition, disposition_match_minutes = None, None
        else:
            is_disposition, disposition_match_minutes = disposition.resolve(
                date, symbol, market_type
            )

        foreign_net, trust_net, dealer_net = provider.insti(symbol, date)
        margin_data = _resolve_margin_data(
            symbol, date, margin_cache, twse_margin, tpex_margin
        )

        volume_lots = _to_lots(volume)
        foreign_net_lots = _to_lots(foreign_net)
        trust_net_lots = _to_lots(trust_net)
        dealer_net_lots = _to_lots(dealer_net)
        insti_total_lots = _insti_total_lots(
            foreign_net_lots, trust_net_lots, dealer_net_lots
        )

        turnover_rate = _turnover_rate(volume, issued_shares, symbol)

        margin_balance = margin_data.get("margin_balance")
        short_balance = margin_data.get("short_balance")
        short_margin_ratio = _short_margin_ratio(margin_balance, short_balance)

        holding_pct = _resolve_holding_pct(symbol, date, holding_pct_cache)

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
            "is_disposition": is_disposition,
            "disposition_match_minutes": disposition_match_minutes,
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


def _find_symbol_row(df: pd.DataFrame | None, symbol: str) -> pd.Series | None:
    """在整批來源裡找出該 symbol 的列；來源缺席、空表、查無此檔都回 None。"""
    if df is None or df.empty:
        return None
    row = df.loc[df["symbol"] == symbol]
    if row.empty:
        return None
    return row.iloc[0]


def _ohlcv_from_row(row: pd.Series) -> tuple:
    """由整批來源的一列取出 (open, close, high, low, volume)。

    open / close 用直接索引，缺欄代表來源格式異常，應該當場炸；
    high / low / volume 用 .get()，部分來源本來就沒有這幾欄。
    """
    return (row["open"], row["close"], row.get("high"), row.get("low"), row.get("volume"))


def _fill_missing_ohlcv(current: tuple, row: pd.Series) -> tuple:
    """只補 current 裡為 None 的欄位；已有值的不覆寫，也不重新求值。"""
    open_price, close_price, high_price, low_price, volume = current
    if open_price is None:
        open_price = row["open"]
    if close_price is None:
        close_price = row["close"]
    if high_price is None:
        high_price = row.get("high")
    if low_price is None:
        low_price = row.get("low")
    if volume is None:
        volume = row.get("volume")
    return (open_price, close_price, high_price, low_price, volume)


def _fill_ohlcv_from_stock_day(
    session: requests.Session,
    date: dt.date,
    symbol: str,
    twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
    current: tuple,
) -> tuple:
    """STOCK_DAY 逐檔月表補值 —— 全函式唯一的逐檔 HTTP，最後手段。

    抓不到（DataUnavailableError）時原樣回傳 current，不讓例外往外擴散：
    這是 fallback 鏈的最後一環，缺值由呼叫端寫成 NULL。
    """
    month_start = date.replace(day=1)
    cache_key = (symbol, month_start)
    twse_day = twse_month_cache.get(cache_key)

    if twse_day is None:
        try:
            twse_day = fetch_twse_stock_day(session, symbol, date)
            twse_month_cache[cache_key] = twse_day
        except DataUnavailableError:
            return current

    # find_twse_ohlcv 的順序是 (open, high, low, close, volume)，
    # 與 _ohlcv_from_row 的 (open, close, high, low, volume) 不同，不可混用。
    ohlcv = find_twse_ohlcv(twse_day, date)
    open_price, close_price, high_price, low_price, volume = current
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
    return (open_price, close_price, high_price, low_price, volume)


def _lookup_change(
    symbol: str,
    twse_mi_index: pd.DataFrame | None,
    tpex_quotes: pd.DataFrame,
):
    """漲跌價差 —— 只從 MI_INDEX / TPEX quotes 取，兩者皆無時回 None。

    刻意不從 STOCK_DAY_ALL 取：它在除權息日給 Change=0.0000 且不帶任何標記，
    會算出「參考價 = 收盤」的錯值。兩邊都沒有時留 None，由呼叫端寫成 NULL
    （不以前日收盤推測）。
    """
    change = None
    row = _find_symbol_row(twse_mi_index, symbol)
    if row is not None:
        change = row.get("change")

    # 注意：用 `is None`、不是 `pd.isna`。MI_INDEX 除權息日回的 change 是 NaN
    # 不是 None，所以上一個 block 賦值後，這裡的 `change is None` 不會為 True，
    # 不會誤把上市股（不在 TPEX）的 NaN 又拿 TPEX 的資料覆蓋一次 —— 只是這個
    # 「不會誤觸發」目前是靠「上市股不在 TPEX quotes 裡」這個外部事實撐住，
    # 不是程式碼本身保證的。日後若再加第三個 change 來源（尤其若它的「找不到」
    # 用 None 表示），這裡要重新檢視，否則可能把已取到的 NaN 又蓋一次。
    if change is None:
        row = _find_symbol_row(tpex_quotes, symbol)
        if row is not None:
            change = row.get("change")
    return change


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

    各來源的觸發條件刻意寫在這裡而不是下沉到 helper：change 的取得（_lookup_change）
    必須維持獨立、不可併進 STOCK_DAY 區塊的 any(v is None ...) 條件，否則每檔都會
    多打一次 API。
    """
    ohlcv: tuple = (None, None, None, None, None)

    # TWSE STOCK_DAY_ALL（全市場批次）
    row = _find_symbol_row(twse_day_all, symbol)
    if row is not None:
        ohlcv = _ohlcv_from_row(row)

    # TWSE MI_INDEX（全市場批次，涵蓋全部上市）
    if any(v is None for v in ohlcv):
        row = _find_symbol_row(twse_mi_index, symbol)
        if row is not None:
            ohlcv = _fill_missing_ohlcv(ohlcv, row)

    # TPEX quotes（全市場批次，涵蓋全部上櫃）
    if ohlcv[0] is None and ohlcv[1] is None:
        row = _find_symbol_row(tpex_quotes, symbol)
        if row is not None:
            ohlcv = _ohlcv_from_row(row)

    # TWSE STOCK_DAY 月表 —— 唯一的逐檔 HTTP，最後手段。
    # 上櫃股直接跳過：TWSE 月表沒有上櫃資料，打了也只是消耗限流配額。
    if market_type != "tpex" and any(v is None for v in ohlcv):
        ohlcv = _fill_ohlcv_from_stock_day(session, date, symbol, twse_month_cache, ohlcv)

    open_price, close_price, high_price, low_price, volume = ohlcv
    return OhlcvResult(
        open=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume=volume,
        change=_lookup_change(symbol, twse_mi_index, tpex_quotes),
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


_MARGIN_FIELDS = (
    "margin_buy", "margin_sell", "margin_balance", "margin_change",
    "short_sell", "short_buy", "short_balance", "short_change",
    "short_margin_ratio",
)


def _fill_margin_from(
    result: dict[str, int | float | None],
    df: pd.DataFrame | None,
    symbol: str,
) -> bool:
    """由整批融資融券來源填入該檔的欄位；回傳是否命中該檔。

    NaN 與缺席欄位一律留 None（不推測）。short_margin_ratio 是比例存 float，
    其餘是張數存 int——兩者 DB 欄位型別不同，不可統一。
    """
    if df is None or df.empty:
        return False
    row = df.loc[df["symbol"] == symbol]
    if row.empty:
        return False
    for key in _MARGIN_FIELDS:
        if key not in row.columns:
            continue
        val = row.iloc[0][key]
        if pd.notna(val):
            result[key] = float(val) if key == "short_margin_ratio" else int(val)
    return True


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
    result: dict[str, int | float | None] = dict.fromkeys(_MARGIN_FIELDS)

    # TWSE 優先。命中即返回——即使該列有 NaN 也不去 TPEX 補：
    # 上市股不該拿上櫃資料補洞。
    if _fill_margin_from(result, twse_margin, symbol):
        return result

    _fill_margin_from(result, tpex_margin, symbol)
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


class SymbolOhlcv(NamedTuple):
    """單檔在整段區間的 OHLCV 預取結果。

    failed_months 是「判定為限流／取得失敗」的月份（月初日期）。這些月份的日期
    在 by_date 裡不存在，因此 `_build_daily_rows` 會因為無價格而跳過該檔該日
    ——與現行「無價格就不寫」的不變量一致，不需要新的 gating 分支。
    market_type 是定調後的市場別（輸入為 None 時由探測結果填入）。
    """

    by_date: dict[dt.date, OhlcvResult]
    market_type: str | None
    failed_months: list[dt.date]


def _month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    """回傳涵蓋 [start, end] 的所有月份的月初日期（含頭尾的不完整月）。"""
    months: list[dt.date] = []
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        months.append(cur)
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return months


def _month_end(month: dt.date) -> dt.date:
    """回傳該月最後一天（輸入為月初日期）。"""
    return (month + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)


def _fetch_month_ohlcv(
    session: requests.Session,
    symbol: str,
    market_type: str,
    month: dt.date,
) -> dict[dt.date, OhlcvResult]:
    """抓單檔單月的月表並展開。該月無資料時拋 DataUnavailableError。

    回傳只保留**該月**的日期。TPEX 那支有強制月份驗證（參數無效會靜默 fallback
    回當月），TWSE 則沒有；若回到別的月份，`_prefetch_symbol_ohlcv` 的
    `if month_rows:` 會誤判成「這個月抓到了」，把真正缺料的月份藏起來，讓
    MoneyDJ 交叉比對這道防線失效。過濾掉就不會有這種假陽性。
    """
    if market_type == "tpex":
        raw = fetch_tpex_stock_day(session, symbol, month)
        expanded = expand_tpex_stock_day(raw)
    else:
        raw = fetch_twse_stock_day(session, symbol, month)
        expanded = expand_twse_stock_day(raw)

    last = _month_end(month)
    return {
        date: OhlcvResult(
            open=vals["open"], close=vals["close"],
            high=vals["high"], low=vals["low"],
            volume=vals["volume"], change=vals["change"],
        )
        for date, vals in expanded.items()
        if month <= date <= last
    }


def _month_has_trading(month: dt.date, traded_dates: set[dt.date]) -> bool:
    """該月在 MoneyDJ 的交易日集合裡是否有任何一天。

    這是「月表回空到底是沒交易還是被限流」的唯一外部證據來源——
    www.twse.com.tw 兩種情況回同一個字串，無法從回應本身區分
    （見 memory/twse-rate-limit-ambiguous-response.md）。
    """
    return any(month <= d <= _month_end(month) for d in traded_dates)


def _prefetch_symbol_ohlcv(
    session: requests.Session,
    symbol: str,
    market_type: str | None,
    start: dt.date,
    end: dt.date,
    traded_dates: set[dt.date],
) -> SymbolOhlcv:
    """逐月抓單檔在 [start, end] 的 OHLCV + change。

    每月 1 發請求，取代原本「每個交易日 4 發全市場批次」的成本結構。

    market_type 為 None（`--backfill-stocks` 直接給代號、DB 查不到市場別）時，
    用第一個有資料的月份定調：先試 TWSE，回空再試 TPEX，之後整段沿用。

    **限流誤判防護**：`www.twse.com.tw` 被限流時回 HTTP 200 +
    「很抱歉，沒有符合條件的資料!」，與「該月真的沒資料」是同一個字串，無法從
    回應本身區分（見 memory/twse-rate-limit-ambiguous-response.md）。這裡用
    `traded_dates`（該檔在 MoneyDJ zcl 出現過的日期，整段只打一發、不經 TWSE）
    當外部證據：若 MoneyDJ 證明該月有交易而月表回空，就記進 failed_months 並
    警告，該月不寫；兩邊都沒有才當成「該檔那個月本來就沒交易」。

    **跨市場補救**：判定失敗之前會再試另一個市場的月表一次。上櫃轉上市每年約
    10~20 檔，`stocks.market_type` 只記得轉換後的市場，轉換前的月份在該市場的
    月表必定回空，而 MoneyDJ 不分市場、照樣有列 —— 沒有這一步就會被判成限流，
    叫操作者重跑一個永遠不會成功的區間（舊的批次 fallback 鏈是從 tpex_quotes
    透明拿到那些天的，不會有這個問題）。只在「回空且 MoneyDJ 有交易」時才多打
    一發，正常情況零成本；真被限流時兩邊都回空，判準的準確度不受影響。
    定調後的 `resolved` 不因此改變：它代表該檔現在屬於哪個市場，要拿去寫回
    holdings 供處置註記使用。

    **訊息紀律**：`DataUnavailableError`（該月沒資料）是預期情況——回補剛上市不久
    的個股，前面幾十個月都會是這樣——只在真的判定為失敗時才連同原因印出來，
    否則會用幾十行雜訊淹沒同一個輸出流裡的 ⚠ 限流警告。`RequestException`
    （連線／HTTP 層真的失敗）則一律印，包含探測市場別的那條路徑。
    """
    by_date: dict[dt.date, OhlcvResult] = {}
    failed_months: list[dt.date] = []
    resolved = market_type

    def fetch_month(candidate: str, month: dt.date) -> tuple[dict[dt.date, OhlcvResult], str]:
        """回傳 (該月資料, 失敗原因)。原因只在判定失敗時才會被印出來。"""
        try:
            return _fetch_month_ohlcv(session, symbol, candidate, month), ""
        except DataUnavailableError as exc:
            return {}, str(exc)
        except requests.RequestException as exc:
            print(f"    {symbol} {month:%Y-%m} {candidate} 月表請求失敗：{exc}")
            return {}, str(exc)

    def fetch_month_probing(month: dt.date):
        """回傳 (該月資料, 失敗原因, 定調後的市場別)。

        市場別未知時先 TWSE 後 TPEX，以先取得資料者定調；兩邊都空則維持未定調，
        並沿用最後一次嘗試的原因字串。
        """
        if resolved is not None:
            rows, reason = fetch_month(resolved, month)
            return rows, reason, resolved
        rows: dict[dt.date, OhlcvResult] = {}
        reason = ""
        for candidate in ("twse", "tpex"):
            rows, reason = fetch_month(candidate, month)
            if rows:
                return rows, reason, candidate
        return rows, reason, None

    def rescue_from_other_market(month: dt.date):
        """跨市場補救：該月這檔可能還在另一個市場（市場別轉換）。

        只在「回空且 MoneyDJ 有交易」時才多打這一發，正常情況零成本。
        不改變 resolved——它代表該檔現在屬於哪個市場，要寫回 holdings 供處置註記用。
        """
        if resolved is None:
            return None
        other = "tpex" if resolved == "twse" else "twse"
        rows, _ = fetch_month(other, month)
        if not rows:
            return None
        print(
            f"    {symbol} {month:%Y-%m} 改由 {other} 月表取得"
            "（該月的市場別與 stocks.market_type 不同，應為市場別轉換）"
        )
        return rows

    for month in _month_starts(start, end):
        month_rows, reason, resolved = fetch_month_probing(month)

        if month_rows:
            by_date.update(month_rows)
            continue

        # 月表沒給東西 —— 是「沒交易」還是「被限流」？用 MoneyDJ 當外部證據。
        if not _month_has_trading(month, traded_dates):
            continue

        rescued = rescue_from_other_market(month)
        if rescued:
            by_date.update(rescued)
            continue

        failed_months.append(month)
        detail = f"（{reason}）" if reason else ""
        print(
            f"    ⚠ {symbol} {month:%Y-%m} 月表回空{detail}，但 MoneyDJ 顯示該月有交易"
            "：判定為限流／取得失敗，該月不寫入（請稍後重跑此區間）"
        )

    return SymbolOhlcv(
        by_date=by_date, market_type=resolved, failed_months=failed_months
    )


def _print_backfill_stocks_summary(
    *,
    provider: PerSymbolRangeProvider,
    total_days: int,
    written_days: int,
) -> None:
    """`--backfill-stocks` 跑完後的收尾摘要。

    回補 1 檔 3 年會印出約 780 行幾乎一模一樣的逐日訊息；真正要看的
    「這檔三大法人整段沒抓到、所以一列都沒寫」只在最開頭出現一次，早就捲不見了。
    收尾時重印一次，讓操作者不必往回捲幾百行才知道這次到底寫進去什麼。
    """
    print("--- --backfill-stocks 摘要 ---")
    print(
        f"日期：共 {total_days} 天，寫入 {written_days} 天、"
        f"未寫入 {total_days - written_days} 天（未寫入含非交易日）"
    )

    insti_failed = provider.insti_failed_symbols
    if insti_failed:
        print(
            f"三大法人（MoneyDJ）取得失敗、整檔未寫入：{'、'.join(insti_failed)}"
            "（請稍後重跑這些個股）"
        )
    else:
        print("三大法人（MoneyDJ）：全部個股取得成功")

    failed_months = provider.failed_months_by_symbol
    if failed_months:
        n_months = sum(len(months) for months in failed_months.values())
        print(f"月表判定為取得失敗、未寫入的月份共 {n_months} 個（請稍後重跑這些區間）：")
        for symbol, months in failed_months.items():
            print(f"  {symbol}：{'、'.join(f'{m:%Y-%m}' for m in months)}")
    else:
        print("月表：沒有判定為取得失敗的月份")


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
    disposition: DispositionData | None = None,
    provider: RowSourceProvider | None = None,
) -> bool:
    """Process data for a single date.

    write_market_daily: 是否寫入全市場大盤資料 (market_daily)。market_daily 以
    trade_date 為鍵、與個股無關；--backfill-stocks（特定股票回補）會傳 False，
    避免對共用的大盤表產生非預期副作用。一般日期範圍回補與 daily 模式維持 True。

    provider: 已預取好的來源。傳入時**完全跳過所有全市場批次 HTTP**
    （T86 / MI_INDEX / TPEX quotes / TPEX 3insti）以及 twse_confirmed 判斷，
    直接用它組列——`--backfill-stocks` 走這條路，交易日由 provider 的月表資料
    決定（該日無價格就不寫該檔）。不傳時維持現行行為。
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

    # per-stock 模式：來源已整段預取完畢，跳過所有全市場批次 HTTP。
    # 交易日不再靠 twse_confirmed 判定 —— 該檔該日有沒有交易由月表資料決定
    # （provider.ohlcv 回全 None → _build_daily_rows 因無價格跳過該列）。
    if provider is not None:
        if disposition is None:
            with _phase(f"{sheet_name} 處置股名單"):
                disposition = _fetch_disposition(session, date, date)

        with _phase(f"{sheet_name} 逐檔組列（{len(holdings)} 檔）"):
            output_df = _build_daily_rows(
                date=date,
                holdings=holdings,
                provider=provider,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
                disposition=disposition,
            )

        if output_df.empty or output_df["close"].isna().all():
            print(f"{sheet_name} 無可寫入資料（該日無交易或來源取得失敗）。")
            return False

        sheet_names.add(sheet_name)
        with _phase(f"{sheet_name} 寫入 stock_daily_raw（{len(output_df)} 列）"):
            upsert_daily_raw(config.database_url, date, output_df)

        # market_daily 與個股無關，per-stock 回補不動它（維持既有不變量）。
        return True

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

    # 處置股名單。backfill 由呼叫端整段預取一次（見 _main_inner），daily / 單日模式
    # 在此自抓當日窗口，兩市場各一次 HTTP。刻意不納入 _stock_sources_ok：處置只是
    # 註記欄，抓不到不該讓整檔個股跳過不寫（失敗時該市場寫 NULL，保留既有值）。
    if disposition is None:
        with _phase(f"{sheet_name} 處置股名單"):
            disposition = _fetch_disposition(session, date, date)

    # 三大法人來源健康度（已過 twse_confirmed，交易日下「空」＝該來源 fetch 失敗）。
    # 逐檔寫入時用來決定該市場個股是否跳過（融資融券非必要，不納入）。
    twse_insti_ok = not twse_3insti.empty
    tpex_insti_ok = not tpex_3insti.empty

    # Build daily data
    with _phase(f"{sheet_name} 逐檔組列（{len(holdings)} 檔）"):
        provider = BatchSourceProvider(
            session=session,
            twse_3insti=twse_3insti,
            twse_day_all=twse_day_all,
            twse_mi_index=twse_mi_index,
            tpex_quotes=tpex_quotes,
            tpex_3insti=tpex_3insti,
            twse_month_cache=twse_month_cache,
            twse_insti_ok=twse_insti_ok,
            tpex_insti_ok=tpex_insti_ok,
        )
        output_df = _build_daily_rows(
            date=date,
            holdings=holdings,
            provider=provider,
            issued_shares=issued_shares,
            twse_margin=twse_margin,
            tpex_margin=tpex_margin,
            margin_cache=margin_cache,
            holding_pct_cache=holding_pct_cache,
            name_map=name_map,
            disposition=disposition,
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

    # --backfill-disposition mode：只回補處置股兩欄
    if args.backfill_disposition:
        _backfill_disposition_command(session, config, args)
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

        # 市場別查 DB（CLI 只給代號）；查不到的留 None，由 provider 探測定調。
        market_types = load_market_types(db_url)
        start_date = _parse_date(args.backfill_start)
        end_date = _parse_date(args.backfill_end)
        backfill_dates = _build_date_range(start_date, end_date)
        # 正規化後再往下傳：`_build_date_range` 會處理起訖顛倒，但下面的
        # `_prefetch_margin_cache` / `PerSymbolRangeProvider.build` 吃的是
        # start_date / end_date 本身。顛倒時 `_month_starts` 會回空 list，
        # 整段一發 OHLCV 都不抓，卻照樣印出「N 天」的抬頭，變成靜默的 no-op。
        start_date, end_date = backfill_dates[0], backfill_dates[-1]
        print(
            f"回補特定股票 {','.join(stock_list)}"
            f" ({len(backfill_dates)} 天：{start_date} ~ {end_date})"
        )

        print("載入發行股數...")
        issued_shares = _get_issued_shares(session, config)
        print("載入股票名稱...")
        name_map = load_stock_names(db_url)
        # per-stock 分支不打 STOCK_DAY 逐檔月表快取，但 _run_for_date 簽名仍要求
        # 這個位置參數；provider 分支內部不會用到它。
        twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame] = {}

        margin_holdings = pd.DataFrame([
            {"symbol": s, "name": "", "market_type": market_types.get(s)}
            for s in stock_list
        ])
        margin_cache = _prefetch_margin_cache(
            session, margin_holdings, start_date, end_date
        )
        # 處置名單整段預取一次（每市場切成 ≤ 6 個月的窗口，見 _fetch_disposition），
        # 避免每天各打一次。
        with _phase("預取處置股名單"):
            disposition = _fetch_disposition(session, start_date, end_date)

        # per-stock 區間來源：OHLCV 每檔每月 1 發、三大法人每檔整段 1 發。
        # 取代原本「每個交易日 4 發全市場批次」的成本結構。
        # 三大法人與外資/法人持股佔比來自同一次 MoneyDJ zcl fetch（見
        # PerSymbolRangeProvider.build），故**不再**呼叫 _prefetch_holding_pct_cache
        # ——那會是對同一頁的第二次請求，直接沿用 provider 解析好的結果即可。
        with _phase("預取個股區間 OHLCV／三大法人"):
            provider = PerSymbolRangeProvider.build(
                session=session,
                symbols=stock_list,
                market_types=market_types,
                start=start_date,
                end=end_date,
            )
        holding_pct_cache = provider.holding_pct_cache

        # 用探測定調後的市場別組 holdings，讓處置註記與 gating 拿到正確市場別。
        stocks_holdings = pd.DataFrame([
            {
                "symbol": s,
                "name": name_map.get(s, ""),
                "market_type": provider.resolved_market_types.get(s)
                or market_types.get(s),
            }
            for s in stock_list
        ])

        sheet_names = set()  # 不需 dedup（force 模式忽略，沒 force 也沒 skip 邏輯）
        written_days = 0
        for date in backfill_dates:
            if _run_for_date(
                session, date, stocks_holdings, sheet_names, twse_month_cache,
                config, today, skip_existing=False,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
                write_market_daily=False,
                disposition=disposition,
                provider=provider,
            ):
                written_days += 1
        # 至少寫入一天時才修正 backfill 左邊界前一天的 margin/short（範圍內已是 MoneyDJ
        # 修正版，但實際左邊界前一天不在 prefetch 範圍內，可能仍是 provisional）
        # 使用 backfill_dates[0]（_build_date_range 已正規化 reversed/one-sided 輸入）
        if written_days:
            _refresh_prev_day_margin(session, stocks_holdings, backfill_dates[0], config)

        _print_backfill_stocks_summary(
            provider=provider,
            total_days=len(backfill_dates),
            written_days=written_days,
        )
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
        # 處置名單整段預取一次（每市場切成 ≤ 6 個月的窗口，見 _fetch_disposition），
        # 避免每天各打一次。
        with _phase("預取處置股名單"):
            disposition = _fetch_disposition(session, start_date, end_date)

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
                disposition=disposition,
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
