"""Database connection pool management and schema initialization (RawData side)."""

from __future__ import annotations

import datetime as dt

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_partition_years: set[int] = set()


def get_pool(database_url: str) -> ConnectionPool:
    """Return the singleton connection pool, creating it if needed."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(database_url, min_size=1, max_size=5)
        _pool.wait()
    return _pool


def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_partition(conn: psycopg.Connection, trade_date: dt.date) -> None:
    """Create yearly partition for stock_daily_raw if missing.

    RawData side only owns stock_daily_raw partitions.
    """
    year = trade_date.year
    if year in _partition_years:
        return

    start = f"{year}-01-01"
    end = f"{year + 1}-01-01"

    part_name = f"stock_daily_raw_{year}"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {part_name} PARTITION OF stock_daily_raw"
        f" FOR VALUES FROM ('{start}') TO ('{end}')"
    )
    _partition_years.add(year)


_SCHEMA_SQL = """
-- Function: auto-update updated_time
-- 注意：此 function 在 TWStockAnalysis repo 也定義一份，必須兩邊字面一致。
CREATE OR REPLACE FUNCTION update_updated_time()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_time = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- stocks
CREATE TABLE IF NOT EXISTS stocks (
    symbol             VARCHAR(10)  PRIMARY KEY,
    name               VARCHAR(50)  NOT NULL DEFAULT '',
    industry_type      INTEGER      DEFAULT NULL,
    industry_desc      VARCHAR(100) NOT NULL DEFAULT '',
    volume_type        SMALLINT     NOT NULL DEFAULT 0,
    issued_shares      BIGINT       DEFAULT NULL,
    enabled            BOOLEAN      NOT NULL DEFAULT FALSE,
    alpha_pick_enabled BOOLEAN      NOT NULL DEFAULT TRUE,
    created_time       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_time       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_stocks_enabled ON stocks (enabled) WHERE enabled = TRUE;

-- stock_daily_raw (partitioned by year)
CREATE TABLE IF NOT EXISTS stock_daily_raw (
    symbol                      VARCHAR(10)  NOT NULL,
    trade_date                  DATE         NOT NULL,
    name                        VARCHAR(50),
    open                        NUMERIC(12,2),
    close                       NUMERIC(12,2),
    high                        NUMERIC(12,2),
    low                         NUMERIC(12,2),
    volume                      BIGINT,
    turnover_rate               NUMERIC(10,6),
    foreign_net                 BIGINT,
    trust_net                   BIGINT,
    dealer_net                  BIGINT,
    institutional_investors_net BIGINT,
    margin_buy                  BIGINT,
    margin_sell                 BIGINT,
    margin_balance              BIGINT,
    margin_change               BIGINT,
    short_sell                  BIGINT,
    short_buy                   BIGINT,
    short_balance               BIGINT,
    short_change                BIGINT,
    short_margin_ratio          NUMERIC(8,6),
    foreign_holding_pct         NUMERIC(8,4),
    insti_holding_pct           NUMERIC(8,4),
    created_time                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
) PARTITION BY RANGE (trade_date);

CREATE INDEX IF NOT EXISTS idx_raw_trade_date ON stock_daily_raw (trade_date);
CREATE INDEX IF NOT EXISTS idx_raw_symbol ON stock_daily_raw (symbol);

-- market_daily (大盤每日交易行情)
CREATE TABLE IF NOT EXISTS market_daily (
    trade_date            DATE PRIMARY KEY,
    taiex_open            NUMERIC(12,2),
    taiex_high            NUMERIC(12,2),
    taiex_low             NUMERIC(12,2),
    taiex_close           NUMERIC(12,2),
    total_volume          BIGINT,
    margin_balance        BIGINT,
    margin_balance_change BIGINT,
    foreign_net           BIGINT,
    created_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- stock_holder_percent (大戶 / 散戶持股佔比)
-- 資料來源：TDCC 集保戶股權分散表，每週更新一次（trade_date 為週五結算日）。
-- major_ratio：400 張（> 400,000 股）以上占集保庫存數比例，如 0.7572。
-- retail_ratio：小於 20 張（<= 20,000 股）占集保庫存數比例，如 0.1520。
CREATE TABLE IF NOT EXISTS stock_holder_percent (
    symbol        VARCHAR(10) NOT NULL,
    trade_date    DATE        NOT NULL,
    name          VARCHAR(50),
    major_ratio   NUMERIC(8,6),
    retail_ratio  NUMERIC(8,6),
    created_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);
-- 既有資料庫的線上 migration：補上後加的 retail_ratio 欄（idempotent）。
ALTER TABLE stock_holder_percent ADD COLUMN IF NOT EXISTS retail_ratio NUMERIC(8,6);
CREATE INDEX IF NOT EXISTS idx_holder_percent_trade_date
    ON stock_holder_percent (trade_date);

-- Triggers for updated_time
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_stocks_updated'
    ) THEN
        CREATE TRIGGER trg_stocks_updated BEFORE UPDATE ON stocks
            FOR EACH ROW EXECUTE FUNCTION update_updated_time();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_raw_updated'
    ) THEN
        CREATE TRIGGER trg_raw_updated BEFORE UPDATE ON stock_daily_raw
            FOR EACH ROW EXECUTE FUNCTION update_updated_time();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_market_daily_updated'
    ) THEN
        CREATE TRIGGER trg_market_daily_updated BEFORE UPDATE ON market_daily
            FOR EACH ROW EXECUTE FUNCTION update_updated_time();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_holder_percent_updated'
    ) THEN
        CREATE TRIGGER trg_holder_percent_updated BEFORE UPDATE ON stock_holder_percent
            FOR EACH ROW EXECUTE FUNCTION update_updated_time();
    END IF;
END $$;
"""


def init_schema(pool: ConnectionPool) -> None:
    """Execute DDL to create RawData side tables, indexes, triggers."""
    with pool.connection() as conn:
        conn.execute(_SCHEMA_SQL)
        conn.commit()
    print("RawData schema 初始化完成")
