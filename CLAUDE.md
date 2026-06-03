# CLAUDE.md

## Workflow

- **所有涉及 coding、架構規劃、寫程式的任務，一律請先設計完架構並釐清所有實作細節，有疑問的地方提出討論，沒問題再開始實作。** 不可以未經討論就直接動手寫 code。

- **每次改動必須同步更新 `README.md`，讓 `README.md` 保持最新狀態。** 包括但不限於：新增/修改 CLI 參數、新增/修改資料抓取邏輯、新增/修改資料表。

## 專案概述

每日抓取台股 raw data（OHLCV、三大法人、融資融券、外資/法人持股、大盤行情），
寫入 PostgreSQL，供下游 `TWStockAnalysis` repo 分析使用。
本 repo **只抓 raw data + 寫 DB**；技術指標、選股、賣出警示、通知都不在此。

## Commands

```bash
# 安裝（Python >= 3.13）
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"

# 跑抓取
tw-stock-rawdata                      # 今天
tw-stock-rawdata --date 2025-10-15    # 指定日
tw-stock-rawdata --backfill-start 2025-08-01 --backfill-end 2025-10-15
tw-stock-rawdata --backfill-stocks 2330,2317 --backfill-start ... --backfill-end ...
tw-stock-rawdata --backfill-start ... --backfill-end ... --force  # 強制覆蓋
tw-stock-rawdata --update-shares      # 只刷新 stocks.issued_shares

# 測試
pytest tests/unit/
```

## 架構（src/tw_stock_rawdata/）

- `run.py`     — CLI 入口、每日抓取編排（`_run_for_date` 為主流程）
- `sources.py` — 對外抓資料（TWSE / TPEX / MoneyDJ），含 retry 機制
- `prepare.py` — 把各來源回傳的 DataFrame normalize 成標準欄位
- `db_utils.py`— upsert 邏輯（stocks / daily_raw / market_daily）
- `db.py`      — 連線池、schema 建立、年度 partition
- `config.py`  — 從 .env 讀 `DATABASE_URL` / `USE_DB`

## 資料表

PG infra（docker compose）由下游 `TWStockAnalysis` repo 擁有；本 repo 只擁有
`stock_daily_raw` 的年度 partition。三張表：

- `stocks` — 個股 master（symbol PK、issued_shares、enabled、alpha_pick_enabled）
- `stock_daily_raw` — 每日逐檔 raw（PK `(symbol, trade_date)`，**按年 RANGE partition**）
- `market_daily` — 大盤每日行情（trade_date PK）

## Gotchas（重要不變量，改動前必讀）

- **upsert 用 COALESCE**：`upsert_daily_raw` / `upsert_market_daily` 的 ON CONFLICT 是
  `col = COALESCE(EXCLUDED.col, table.col)` — 新值是 NULL 時**不覆寫**舊值，避免某個
  子來源失敗把已寫好的好資料蓋成 NULL。不要改回直接 `EXCLUDED.col`。
- **逐檔跳過半套資料**：個股若該日缺關鍵來源（見 `_stock_sources_ok`），整檔跳過
  不寫，避免寫入半套 raw。
- **retry profile 分兩種**（`sources.py`）：長窗口 `RETRY_ATTEMPTS=6` 給每日整批抓取；
  短窗口 `PER_SYMBOL_*` 給「每檔個股各打一次」的 `fetch_twse_stock_day`。MoneyDJ /
  TWSE / TPEX 暫時性 5xx（如 520）靠 `_retry_on_transient` 吸收。
- **`--backfill-stocks` 不動 `market_daily`**：逐檔回補只寫 `stock_daily_raw`，避免對
  共用大盤表造成非預期副作用。
