"""DB utility functions for RawData side."""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from .db import ensure_partition, get_pool


# ---------------------------------------------------------------------------
# DataFrame column lists — must match stock_daily_raw schema column order
# ---------------------------------------------------------------------------

# Columns in stock_daily_raw (excluding created_time/updated_time)
_RAW_COLUMNS = [
    "symbol", "trade_date", "name", "open", "close", "high", "low", "volume",
    "turnover_rate", "foreign_net", "trust_net", "dealer_net",
    "institutional_investors_net", "margin_buy", "margin_sell",
    "margin_balance", "margin_change", "short_sell", "short_buy",
    "short_balance", "short_change", "short_margin_ratio",
    "foreign_holding_pct", "insti_holding_pct",
]

# Mapping from DataFrame column names to raw DB columns
_RAW_DF_COLS = [
    "symbol", "name", "open", "close", "high", "low", "volume",
    "turnover_rate", "foreign_net", "trust_net", "dealer_net",
    "institutional_investors_net", "margin_buy", "margin_sell",
    "margin_balance", "margin_change", "short_sell", "short_buy",
    "short_balance", "short_change", "short_margin_ratio",
    "foreign_holding_pct", "insti_holding_pct",
]


def _safe(val):
    """Convert pandas NA / NaN / numpy types to Python native or None."""
    if val is None:
        return None
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if pd.isna(val):
        return None
    # Convert numpy types to native Python types
    if hasattr(val, "item"):
        return val.item()
    return val


# ---------------------------------------------------------------------------
# stocks
# ---------------------------------------------------------------------------


def upsert_stocks(
    database_url: str,
    enabled_symbols: list[str],
) -> None:
    """Enable only the given symbols in stocks table.

    1. Set enabled=false for all stocks.
    2. Set enabled=true for symbols in enabled_symbols.
    """
    pool = get_pool(database_url)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE stocks SET enabled = FALSE")
            if enabled_symbols:
                cur.executemany(
                    """
                    INSERT INTO stocks (symbol, name, enabled)
                    VALUES (%s, '', TRUE)
                    ON CONFLICT (symbol) DO UPDATE SET enabled = TRUE
                    """,
                    [(s,) for s in enabled_symbols],
                )
        conn.commit()


def upsert_stock_shares(
    database_url: str,
    df: pd.DataFrame,
) -> None:
    """Upsert issued shares data into stocks table.

    Expected columns: symbol, name, issued_shares.
    """
    if df.empty:
        return

    total = len(df)
    pool = get_pool(database_url)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            params = []
            for _, row in df.iterrows():
                symbol = str(row.get("symbol", "")).strip()
                if not symbol:
                    continue
                name = str(row.get("name", "")).strip()
                if name.lower() == "nan":
                    name = ""
                issued = _safe(row.get("issued_shares"))
                params.append((symbol, name, issued))
            cur.executemany(
                """
                INSERT INTO stocks (symbol, name, issued_shares)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    name = CASE WHEN EXCLUDED.name != '' THEN EXCLUDED.name
                                ELSE stocks.name END,
                    issued_shares = EXCLUDED.issued_shares
                """,
                params,
            )
            print(f"  DB upsert {len(params)}/{total} 筆完成", flush=True)
        conn.commit()


def load_stock_names(database_url: str) -> dict[str, str]:
    """Load stock name mapping from stocks table."""
    pool = get_pool(database_url)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT symbol, name FROM stocks WHERE name != ''"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def load_stock_shares(database_url: str) -> dict[str, int]:
    """Load issued shares from DB.

    Returns dict mapping symbol to issued shares count.
    """
    pool = get_pool(database_url)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT symbol, issued_shares FROM stocks"
            " WHERE issued_shares IS NOT NULL"
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def get_enabled_stocks(
    database_url: str,
) -> list[tuple[str, str, int | None, str]]:
    """Return list of (symbol, name, industry_type, industry_desc) for enabled stocks."""
    pool = get_pool(database_url)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT symbol, name, industry_type, industry_desc"
            " FROM stocks WHERE enabled = TRUE ORDER BY symbol"
        ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# ---------------------------------------------------------------------------
# stock_daily_raw
# ---------------------------------------------------------------------------


def _build_raw_rows(
    trade_date: dt.date,
    df: pd.DataFrame,
) -> list[list]:
    """Build parameter rows for stock_daily_raw from a DataFrame."""
    rows = []
    for _, row in df.iterrows():
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        values = [symbol, trade_date]
        for col in _RAW_DF_COLS:
            if col == "symbol":
                continue
            values.append(_safe(row.get(col)))
        rows.append(values)
    return rows


def upsert_daily_raw(
    database_url: str,
    trade_date: dt.date,
    df: pd.DataFrame,
) -> None:
    """Upsert daily raw trading data from a DataFrame."""
    if df.empty:
        return

    rows = _build_raw_rows(trade_date, df)
    if not rows:
        return

    update_cols = [c for c in _RAW_COLUMNS if c not in ("symbol", "trade_date")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    placeholders = ", ".join(["%s"] * len(_RAW_COLUMNS))
    insert_cols = ", ".join(_RAW_COLUMNS)
    sql = (
        f"INSERT INTO stock_daily_raw ({insert_cols})"
        f" VALUES ({placeholders})"
        f" ON CONFLICT (symbol, trade_date) DO UPDATE SET {set_clause}"
    )

    pool = get_pool(database_url)
    with pool.connection() as conn:
        ensure_partition(conn, trade_date)
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()


_PREV_MARGIN_COLS = [
    "margin_buy", "margin_sell", "margin_balance", "margin_change",
    "short_sell", "short_buy", "short_balance", "short_change",
    "short_margin_ratio",
]


def find_consensus_prev_trade_date(
    database_url: str, current_date: dt.date
) -> dt.date | None:
    """找 stock_daily_raw 與 market_daily 兩邊都同意的 D-1。

    若兩邊 MAX(trade_date) < current_date 不一致（任一邊缺日），回傳 None。
    呼叫端在 None 時應跳過修正，避免 gap 情境下把資料寫到錯誤的歷史 row。
    """
    pool = get_pool(database_url)
    with pool.connection() as conn, conn.cursor() as cur:
        return _consensus_prev_trade_date(cur, current_date)


def update_prev_day_margin_batch(
    database_url: str,
    updates: list[tuple[str, dt.date, dict]],
) -> int:
    """批次覆寫 stock_daily_raw 的 margin/short 欄位。

    Args:
        database_url: PostgreSQL connection string.
        updates: list of (symbol, trade_date, data_dict)。
                 data_dict 鍵：margin_buy / margin_sell / margin_balance / margin_change
                 / short_sell / short_buy / short_balance / short_change /
                 short_margin_ratio。MoneyDJ 為權威來源，None 直接覆寫 NULL。

    Returns:
        實際 UPDATE 成功的 row 數合計（不存在的 (symbol, trade_date) 不算）。
    """
    if not updates:
        return 0

    set_clause = ", ".join(f"{c} = %s" for c in _PREV_MARGIN_COLS)
    sql = (
        f"UPDATE stock_daily_raw SET {set_clause}"
        f" WHERE symbol = %s AND trade_date = %s"
    )

    n_updated = 0
    pool = get_pool(database_url)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for symbol, trade_date, data in updates:
                params = [_safe(data.get(c)) for c in _PREV_MARGIN_COLS]
                params.extend([symbol, trade_date])
                cur.execute(sql, params)
                n_updated += cur.rowcount
        conn.commit()
    return n_updated


# ---------------------------------------------------------------------------
# market_daily
# ---------------------------------------------------------------------------


def upsert_market_daily(database_url: str, trade_date: dt.date, data: dict) -> None:
    """Upsert a single row into market_daily table.

    Args:
        database_url: PostgreSQL connection string.
        trade_date: The trading date.
        data: dict with keys: taiex_open, taiex_high, taiex_low, taiex_close,
              total_volume, margin_balance, margin_balance_change, foreign_net.
    """
    pool = get_pool(database_url)
    row = {"trade_date": trade_date, **data}
    cols = [
        "trade_date", "taiex_open", "taiex_high", "taiex_low", "taiex_close",
        "total_volume", "margin_balance", "margin_balance_change", "foreign_net",
    ]
    # Only include columns that exist in data
    row = {c: row.get(c) for c in cols}
    placeholders = ", ".join(f"%({c})s" for c in cols)
    updates = ", ".join(
        f"{c} = COALESCE(EXCLUDED.{c}, market_daily.{c})"
        for c in cols if c != "trade_date"
    )
    sql = (
        f"INSERT INTO market_daily ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (trade_date) DO UPDATE SET {updates}"
    )
    with pool.connection() as conn:
        conn.execute(sql, row)
        conn.commit()


def _consensus_prev_trade_date(cur, before_date: dt.date) -> dt.date | None:
    """找 stock_daily_raw 與 market_daily 都同意的前一個交易日。

    任一邊缺日（兩邊 MAX(trade_date) < before_date 不一致）就回傳 None，
    避免在 gap 情境下把資料寫到錯誤的歷史 row。
    """
    cur.execute(
        "SELECT MAX(trade_date) FROM stock_daily_raw WHERE trade_date < %s",
        (before_date,),
    )
    row = cur.fetchone()
    sd_prev = row[0] if row else None
    cur.execute(
        "SELECT MAX(trade_date) FROM market_daily WHERE trade_date < %s",
        (before_date,),
    )
    row = cur.fetchone()
    md_prev = row[0] if row else None
    if sd_prev is None or md_prev is None or sd_prev != md_prev:
        return None
    return sd_prev


def correct_prev_margin_balance(
    database_url: str, current_date: dt.date, api_prev_balance: int
) -> tuple[dt.date, int | None, int, int | None, int | None] | None:
    """Reconcile DB 上 D-1 的 margin_balance / margin_balance_change 與 D 日 API
    回報的「前日餘額」。

    流程：
    1. 找真正的 D-1：stock_daily_raw 與 market_daily 兩邊獨立查 MAX(trade_date),
       必須一致；任一邊缺日就放棄修正（避免在 gap 情境下寫到錯的歷史 row）。
    2. 算出新的 (balance, change)：
       - new_balance = api_prev_balance
       - new_change = api_prev_balance − D-2.margin_balance；D-2 同樣須兩邊共識，
         若 D-2 不存在共識或 D-2.margin_balance 為 NULL → new_change = NULL
         （誠實表達 delta 未知）。
    3. 若新值與現值任一欄位不同（含 NULL），就一併 UPDATE。即使 balance 已一致、
       但 change 仍 stale（例如先前 D-2 缺料時 change 存成 NULL，現在 D-2 已補齊）
       也會被修復。

    Args:
        database_url: PostgreSQL connection string.
        current_date: D 日（剛 fetch 的這天）。
        api_prev_balance: D 日 API row[4] × 1000（TWSE 當下回報的前日餘額，元）。

    Returns:
        (prev_trade_date, old_balance, new_balance, old_change, new_change) 若有更新；
        None 若找不到 D-1 共識日、market_daily 缺該日、或新舊值完全一致。
    """
    pool = get_pool(database_url)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            prev_trade_date = _consensus_prev_trade_date(cur, current_date)
            if prev_trade_date is None:
                return None

            cur.execute(
                "SELECT margin_balance, margin_balance_change FROM market_daily "
                "WHERE trade_date = %s",
                (prev_trade_date,),
            )
            row = cur.fetchone()
            if row is None:
                # _consensus_prev_trade_date 已確認 market_daily 有此日，這裡是防禦性檢查。
                return None
            prev_balance, prev_change = row

            prev_prev_date = _consensus_prev_trade_date(cur, prev_trade_date)
            prev_prev_balance: int | None = None
            if prev_prev_date is not None:
                cur.execute(
                    "SELECT margin_balance FROM market_daily WHERE trade_date = %s",
                    (prev_prev_date,),
                )
                row = cur.fetchone()
                if row is not None:
                    prev_prev_balance = row[0]
            new_change = (
                api_prev_balance - prev_prev_balance
                if prev_prev_balance is not None
                else None
            )

            if prev_balance == api_prev_balance and prev_change == new_change:
                return None

            cur.execute(
                "UPDATE market_daily SET margin_balance = %s, margin_balance_change = %s "
                "WHERE trade_date = %s",
                (api_prev_balance, new_change, prev_trade_date),
            )
            conn.commit()
            return (prev_trade_date, prev_balance, api_prev_balance, prev_change, new_change)
