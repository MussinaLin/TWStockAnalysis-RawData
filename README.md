# TWStockAnalysis-RawData

每日抓取台股 raw data（OHLCV、三大法人、融資融券、發行股數、大盤行情），寫入 PostgreSQL，供下游 [`TWStockAnalysis`](https://github.com/MussinaLin/TWStockAnalysis) 分析使用。

## 職責邊界

本 repo 只負責：

- 從 TWSE / TPEX / MoneyDJ 抓 raw data
- 寫入 PostgreSQL：`stocks`、`stock_daily_raw`、`market_daily`

不負責：技術指標、選股、賣出警示、Telegram 通知（由下游 TWStockAnalysis 負責）。

## 安裝

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 設定

複製 `.env.example` 為 `.env`，填入 `DATABASE_URL`：

```bash
cp .env.example .env
# 編輯 .env，填入 PostgreSQL 連線字串
```

## CLI 指令

```bash
# 抓今天的 raw data（預設）
tw-stock-rawdata

# 指定日期
tw-stock-rawdata --date 2025-10-15

# 回補區間
tw-stock-rawdata --backfill-start 2025-08-01 --backfill-end 2025-10-15

# 回補指定股票
tw-stock-rawdata --backfill-stocks 2330,2317 --backfill-start 2025-08-01 --backfill-end 2025-10-15

# 強制覆蓋既有資料
tw-stock-rawdata --backfill-start ... --backfill-end ... --force

# 刷新 stocks.issued_shares
tw-stock-rawdata --update-shares
```

## Docker

PG infra 由下游 TWStockAnalysis repo 擁有：

```bash
# 1. 先在 TWStockAnalysis repo 啟動 PG
cd ../TWStockAnalysis && docker compose up -d postgres

# 2. 在本 repo 用 compose profile 跑
docker compose --profile app run --rm rawdata --date 2025-10-15
```

## 測試

```bash
pip install -e ".[test]"
pytest tests/unit/
```
