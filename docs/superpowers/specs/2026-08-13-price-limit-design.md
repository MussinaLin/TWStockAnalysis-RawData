# 漲停 / 跌停價欄位 — 設計

日期：2026-08-13
狀態：已與使用者確認設計

## 目的

`stock_daily_raw` 新增 `limit_up` / `limit_down` 兩欄，記錄該檔該日的漲停價與跌停價，
供下游 `TWStockAnalysis` 以 `close = limit_up` / `close = limit_down` 直接判定漲停跌停，
或以 `high = limit_up` 判定盤中曾觸及。下游不需重算，也不需自行維護升降單位級距表。

## 判定規則

```
參考價   = 當日收盤價 − 當日漲跌價差
漲停價   = 參考價 × 1.1，無條件捨去到最近的升降單位
跌停價   = 參考價 × 0.9，無條件進位到最近的升降單位
```

升降單位（普通股）：

| 價格區間 | 升降單位 |
|---|---|
| < 10 | 0.01 |
| 10 ≤ p < 50 | 0.05 |
| 50 ≤ p < 100 | 0.1 |
| 100 ≤ p < 500 | 0.5 |
| 500 ≤ p < 1000 | 1 |
| ≥ 1000 | 5 |

**級距由「算出來的漲/跌停價」決定，不是由參考價決定。**

不需要 ETF 的升降單位分支：`stocks` 來自上市 / 上櫃**公司**基本資料
（`t187ap03_L` / `mopsfin_t187ap03_O`），標的池全為普通股（含 KY、TDR），不含 ETF。

### 公式驗證

以 2026-08-12 上櫃 888 檔普通股，比對台灣證券交易櫃檯買賣中心公告的
「次日漲停價 / 次日跌停價」，**887 檔完全吻合**。唯一不吻合的 6485 源自
公告的「次日參考價」本身經過四捨五入，非公式問題。

## 前提（已實測確認）

- 四個 OHLCV 來源全部提供漲跌價差欄位：
  - `STOCK_DAY_ALL`（OpenAPI）：`Change`，已帶正負號
  - `STOCK_DAY`（逐檔月表）：`漲跌價差`，格式 `+1.50` / `-2.00` / 除權息日 `X0.00`
  - `MI_INDEX`：`漲跌價差` 為絕對值，正負號在獨立欄位 `漲跌(+/-)`
  - TPEX v2 `dailyQuotes`：`漲跌`，格式 `+0.46`
- `MI_INDEX` 的 `漲跌(+/-)` 去除 HTML 標籤後只有四種值：
  `+`(773) / `-`(464) / 空字串(130，皆為價差 0.00) / `X`(12，除權息日)
- **`STOCK_DAY_ALL` 在除權息日的 `Change` 給 `0.0000`，不帶任何標記**（2026-08-12
  實測：MI_INDEX 標 `X` 的 12 檔，在 STOCK_DAY_ALL 全部是 `0.0000`）。
  其餘三個來源都能正確表達「無漲跌價差」：`MI_INDEX` 的 `X`、`STOCK_DAY` 的 `X0.00`、
  TPEX 的中文字串 `除息` / `除權息`（2026-08-05 ~ 08-12 實測 21 筆案例皆為此格式）。
- **`STOCK_DAY_ALL` 無視 `date` 參數**，永遠回傳最後交易日，故不可用於歷史回補。
- `MI_INDEX?date=` 與 TPEX v2 `dailyQuotes?date=` 皆可取歷史日資料。
- 現有 `_clean_number('X0.00')` 已回傳 `None`（`float()` 拋 `ValueError`），
  除權息日不需新增分支。

## 資料表變更

`db.py` 的 `_SCHEMA_SQL`：

```sql
-- stock_daily_raw 新增
limit_up    NUMERIC(12,2),
limit_down  NUMERIC(12,2),
```

既有資料庫的線上 migration，沿用 `stock_holder_percent.retail_ratio` 的模式：

```sql
ALTER TABLE stock_daily_raw ADD COLUMN IF NOT EXISTS limit_up   NUMERIC(12,2);
ALTER TABLE stock_daily_raw ADD COLUMN IF NOT EXISTS limit_down NUMERIC(12,2);
```

## 新模組 `price_limit.py`

純函式、無 I/O、不依賴 pandas：

```python
def tick_size(price: Decimal) -> Decimal
def calc_limits(ref: Decimal | None) -> tuple[Decimal | None, Decimal | None]
```

一律用 `Decimal` 不用 `float`：`2415 * 1.1` 在浮點下是 `2656.5000000000005`，
無條件捨去到 5 元檔會踩在邊界上出錯。

`calc_limits(None)` 回傳 `(None, None)`。

## 每日路徑改動

1. **`sources.py` 不用動。**
2. **`prepare.py`**
   - `prepare_twse_day_all` / `prepare_tpex_quotes`：`_find_columns` spec 加 `change`，
     值走 `_clean_number`。
   - `prepare_twse_mi_index`：新增 `_merge_change_sign(sign_text, magnitude) -> float | None`
     — 去除 HTML 標籤後，`X` 回 `None`，`-` 回負值，其餘（`+` 與空字串）回正值。
3. **`run.py`**
   - `_fetch_ohlcv_with_fallback` 的回傳值由 5-tuple 改為 `NamedTuple`
     （欄位 `open` / `close` / `high` / `low` / `volume` / `change`），只有一個 caller。
   - `change` 依上述獨立優先序取得，**跳過 `STOCK_DAY_ALL` 區塊**；OHLCV 其餘欄位
     的逐欄補洞行為完全不動。
   - `_build_daily_rows`：算出參考價後呼叫 `calc_limits`，兩欄寫入 row dict。
     **轉換用 `Decimal(str(x))` 不可用 `Decimal(x)`** —— 上游 `_clean_number` 回的是 `float`，
     `Decimal(2415.0)` 會把浮點誤差整包帶進來，`Decimal(str(2415.0))` 才是乾淨的 `2415.0`。
4. **`db_utils.py`**：`_RAW_COLUMNS` 與 `_RAW_DF_COLS` 各加 `limit_up` / `limit_down`。
   `upsert_daily_raw` 的 COALESCE 語意不動。

## 不變量：change 不可取自 STOCK_DAY_ALL

`change` **不跟隨 OHLCV 的 fallback 順序**，只從兩個批量來源取：

```
MI_INDEX（上市全市場） / TPEX quotes（上櫃全市場）
```

這兩個來源合起來涵蓋 100% 標的池，且在 `_run_for_date` 中都是每日必抓，
故不需要第三順位。逐檔 `STOCK_DAY` 月表雖然也帶 `X0.00` 標記可安全使用，
但它只在「MI_INDEX 取得失敗**且**該檔 OHLCV 不完整」的窄縫才會被觸發，
收益極小卻要改動 `sources.py` 的 `find_twse_ohlcv`，故不採用。

**`STOCK_DAY_ALL` 永不供應 `change`。** 它在除權息日給 `Change = 0.0000` 且不帶任何標記，
而它是 OHLCV fallback 鏈的第一順位；若讓它供應 change，除權息日會算出
`參考價 = 收盤`、`漲停價 = 收盤 × 1.1` 這種看起來合理但錯誤的值，
正好繞過「推不出參考價就寫 NULL」的設計。

跨來源取值在此是安全的：`_run_for_date` 已對每個來源驗證 `*_date == date`，
所有來源都指向同一交易日；且上市股不會出現在 TPEX quotes、上櫃股不會出現在 MI_INDEX，
優先序天然依市場別分流。`MI_INDEX` 在 `_run_for_date` 中是**無條件抓取**
（非等 OHLCV fallback 觸發才抓），故上市股必定取得到。

兩個來源都取不到時 `change` 為 `None`，漲跌停寫 NULL —— 符合「不猜」的設計。

此條需寫入 `CLAUDE.md` 的 Gotchas 章節。

## 推不出參考價時寫 NULL

`change` 為 `None`（除權息日的 `X`、無成交的 `---`）或 `close` 為 `None` 時，
`limit_up` / `limit_down` 一律寫 `None`，**不以前一交易日收盤價推測**。

理由：除權息日的正確參考價是除權息參考價而非前日收盤，用前日收盤會寫入看不出來的錯值。
實例 —— 3605 宏致 2026-08-12 除息，前日收 111.00、當日收 120.00 漲停鎖死
（最後揭示賣價 `--`、賣量 0）；以前日收盤推算會得到漲停價 122.0，實際為 120.0，
該檔漲停會被漏判。除權息日佔比低，寫 NULL 讓下游明確跳過即可。

下游語意：`limit_up IS NULL` 表示該檔該日不判定漲跌停。

## 回補指令 `--backfill-limits`

```bash
tw-stock-rawdata --backfill-limits --backfill-start 2025-01-01 --backfill-end 2026-08-12
```

- 每個交易日只打 2 個批量 API：`MI_INDEX?date=`（上市全市場）
  與 TPEX v2 `dailyQuotes?date=`（上櫃全市場）。沿用現有 `build_session()` 的 TWSE 節流。
- **寫入用 `UPDATE`，不可用 `upsert_daily_raw`**：回補只有這兩欄有值，
  走 upsert 會 `INSERT` 出一批僅 `limit_up` / `limit_down` 有值、其餘全 NULL 的新 row，
  正是 CLAUDE.md「逐檔跳過半套資料」要防的情況。

  ```sql
  UPDATE stock_daily_raw SET limit_up = %s, limit_down = %s
   WHERE symbol = %s AND trade_date = %s
  ```

  只更新已存在的 row；`(symbol, trade_date)` 不存在則不寫。以 `executemany` 批次執行。
- **算出 `None` 的個股整檔跳過不 UPDATE**，不會把已有值蓋成 NULL。
- **一律覆寫，不加 `--force`**：值是冪等的，日後若修正升降單位表直接重跑即可修好。
- **不支援 `--backfill-stocks`**：回補漲跌停是全市場的事，不做逐檔版本。

## 錯誤處理

- 回補時某日 TWSE 失敗但 TPEX 成功：只更新上櫃、印警告、**繼續下一天**，不中止整批。
- 兩邊都失敗：印警告後跳過該日。
- 非交易日：`MI_INDEX` 本就拋 `DataUnavailableError`，沿用現有處理。

## 測試（`tests/unit/`）

`test_price_limit.py`
- 升降單位級距邊界：`9.99 / 10 / 49.95 / 50 / 99.9 / 100 / 499.5 / 500 / 999 / 1000`
- 方向性：漲停無條件捨去、跌停無條件進位（寫反是最容易犯的錯）
- `calc_limits(None) == (None, None)`
- 真實回歸案例：`ref=2415 → (2655, 2175)`（2330）、`ref=120 → (132.0, 108.0)`
  （3605 於 2026-08-13 實際收 132.00 漲停）

`test_prepare.py`
- `_merge_change_sign`：`<p style= color:red>+</p>` + `20.00` → `+20.0`；
  `<p style= color:green>-</p>` + `5.00` → `-5.0`；`<p>X</p>` + `0.00` → `None`；
  空字串 + `0.00` → `0.0`

`test_run.py`
- **STOCK_DAY_ALL 不供應 change**：mock 一個 `twse_day_all` 含 `change` 欄且
  `twse_mi_index` 為 `None`，驗證回傳的 `change` 為 `None`（而非 day_all 的值），
  同時 `close` 仍正常取自 day_all。
- **MI_INDEX 供應 change**：`close` 取自 `twse_day_all`、`change` 取自 `twse_mi_index`，
  驗證兩者可跨來源組合。

## 文件

`README.md` 同步更新（CLAUDE.md 規定）：
- CLI 參數章節新增 `--backfill-limits` 用法與限制
- 資料表章節 `stock_daily_raw` 補上 `limit_up` / `limit_down` 欄位與 NULL 語意

`CLAUDE.md` 的 Gotchas 章節新增「change 與 close 必須同來源」。

## 明確不做

- 不處理除權息日的參考價補正（需另接 TWSE 除權除息計算結果表），該日寫 NULL。
- 不存 boolean 旗標，不存漲跌價差原值，不存參考價 —— 只存兩個價格欄位。
- 不處理無漲跌幅限制的標的（興櫃、上市櫃前五日、國外成分 ETF）：
  標的池為上市櫃普通股，不涵蓋興櫃與 ETF；新上市櫃前五日會算出不適用的值，
  屬已知限制，暫不處理。
