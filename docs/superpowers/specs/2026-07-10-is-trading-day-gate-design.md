# is_trading_day 休市開關 — 設計

日期：2026-07-10
狀態：已與使用者確認設計

## 目的

每日排程模式啟動時，先讀 DB `config` 表中 `key = 'is_trading_day'` 的值：
false 則直接結束、不做任何抓取；true 則照原流程執行。
供外部（下游 repo 或排程）在休市日關閉每日抓取。

## 前提

- `config` 表已存在於共用 PostgreSQL（由下游 `TWStockAnalysis` repo 擁有），
  schema 為 `key VARCHAR(50) PK, value TEXT NOT NULL, created_time, updated_time`。
- `is_trading_day` 這筆 row 已由外部寫入與維護；本 repo **只讀不寫**。

## 生效範圍

只擋「純 daily 模式」：`--date`、`--backfill-start`、`--backfill-end`、
`--backfill-stocks`、`--update-shares`、`--dahu` 全部未指定時才檢查開關。
任何手動參數（含 `--date`）都不受影響，維持隨時可跑。

## 行為

1. `db_utils.py` 新增 `get_config_value(database_url, key) -> str | None`
   — `SELECT value FROM config WHERE key = %s`，查無 row 回傳 `None`。
2. `run.py` 新增純函式 `_parse_trading_day(value: str | None) -> bool`：
   - `"false"` / `"0"` / `"no"`（不分大小寫、去空白）→ `False`
   - `"true"` / `"1"` / `"yes"` → `True`
   - `None` 或無法辨識的值 → 印警告，回傳 `True`（fail-open）
3. `_main_inner` 開頭、確認為純 daily 模式後：
   - 讀開關；`psycopg.Error`（如 config 表不存在）→ 印警告，照常執行（fail-open）。
     不捕捉其他例外，避免吞掉真正的 bug。
   - 解析結果為 `False` → 印「config.is_trading_day = false，今日休市，結束執行」，
     正常結束（exit 0），不做任何抓取與寫入。
   - `True` → 照原流程執行。

## 測試

`tests/unit/` 新增 `_parse_trading_day` 測試：true/false 各種寫法、
None、空字串、無法辨識的值（fail-open 回 True）。

## 文件

README 資料抓取邏輯章節補充此開關：生效範圍、fail-open 行為、
config 表由下游 repo 擁有、本 repo 只讀。
