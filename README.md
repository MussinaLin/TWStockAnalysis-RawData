# TWStockAnalysis-RawData

每日抓取台股 raw data（OHLCV、三大法人、融資融券、發行股數、大盤行情），寫入 PostgreSQL，供下游 [`TWStockAnalysis`](https://github.com/MussinaLin/TWStockAnalysis) 分析使用。

## 職責邊界

本 repo 只負責：

- 從 TWSE / TPEX / MoneyDJ / TDCC 抓 raw data
- 寫入 PostgreSQL：`stocks`、`stock_daily_raw`、`market_daily`、`stock_holder_percent`

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

可選環境變數：

- `TWSE_MIN_INTERVAL`（秒，預設 `1.0`）：對 `www.twse.com.tw` 的請求最小間隔。
  該站對同 IP 高頻請求會限流，回 HTTP 200 + `{"stat":"很抱歉，沒有符合條件的資料!"}`
  的空殼（與真休市同字串），導致被誤判成沒資料而跳過。加最小間隔可從源頭避免觸發；
  若仍偶發，可調大（例如 `1.5`）。僅影響 `www.twse.com.tw`，openapi / TPEX 不受影響。
  真正的關鍵是**壓低請求總數**，見「OHLCV 來源順序」。

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

# 只回補 limit_up / limit_down（不重打法人、融資融券、持股，比全量回補快很多）
tw-stock-rawdata --backfill-limits --backfill-start 2025-01-01 --backfill-end 2026-08-12

# 強制覆蓋既有資料
tw-stock-rawdata --backfill-start ... --backfill-end ... --force

# 刷新 stocks.issued_shares
tw-stock-rawdata --update-shares

# 更新大戶/散戶持股佔比（TDCC 集保戶股權分散表，每週一次；只更新此資料，其他不動）
# 預設只抓最新一筆週資料
tw-stock-rawdata --dahu

# 只更新特定股票
tw-stock-rawdata --dahu --stocks 2330,2303

# 更新區間內所有週資料日（資料每週一次，會對應到區間內的週五結算日）
tw-stock-rawdata --dahu --from 2026-05-01 --to 2026-05-31
```

> 大戶持股佔比 = 持股 400 張（> 400,000 股）以上占集保庫存數比例，存於 `stock_holder_percent.major_ratio`（小數，如 0.7572）。
>
> 散戶持股佔比 = 持股小於 20 張（<= 20,000 股）占集保庫存數比例，存於 `stock_holder_percent.retail_ratio`（小數，如 0.1520）。兩者皆以 TDCC 分級下界判定，故以「整個級距」分類。

### 漲跌停價（limit_up / limit_down）

`stock_daily_raw` 的 `limit_up` / `limit_down` 記錄該檔該日的漲停價與跌停價，
下游可直接以 `close = limit_up` 判定收盤漲停、`high = limit_up` 判定盤中曾觸及。

- 計算方式：`參考價 = 收盤價 − 漲跌價差`；漲停 = 參考價 × 1.1 無條件捨去到升降單位，
  跌停 = 參考價 × 0.9 無條件進位到升降單位。
- **`NULL` 表該日推不出參考價**（除權息日交易所不提供漲跌價差，或該檔無成交），
  下游應跳過該檔該日的漲跌停判定，**不要**自行用前一交易日收盤價推算 —— 除權息日
  的正確基準是除權息參考價，用前日收盤會算出看不出來的錯值。
- 算出的區間若與當日實際成交價矛盾（`high` 高於漲停價或 `low` 低於跌停價），
  代表該日這檔沒有漲跌幅限制，區間是假的，一律改寫 `NULL`。

> **已知限制：新上市櫃前五日**。這些個股無漲跌幅限制（櫃買以「次日漲停價 9995 /
> 跌停價 0.01」表示），但交易所仍提供漲跌價差，照 ±10% 會算出不適用的區間。
> 上面的自我否證只擋得掉成交價真的衝出區間的情形；若當日波動剛好落在假區間內，
> 仍會留下看不出來的假值（實測 2025 年 6 檔新掛牌、15 筆首五日資料中擋下 4 筆）。
> 下游若在意，可自行排除個股上市櫃後的前五個交易日。

歷史資料可用 `--backfill-limits` 回補：每個交易日只打 `MI_INDEX`（上市）與 TPEX
`dailyQuotes`（上櫃）兩個批量行情 API，不重打三大法人 / 融資融券 / 持股。
它只 `UPDATE` 已存在的 row，不會新增 row；推不出參考價的個股整檔跳過，
不會把既有值蓋成 `NULL`。結果冪等，可重複執行，不需 `--force`，
也不支援 `--backfill-stocks`（同時傳入會被忽略，並印警告）。

> 注意：重跑只能修正「修正後仍算得出數值」的錯值。若要把既有非 `NULL` 的值改回
> `NULL`（例如日後排除新上市櫃前五日），因 upsert 採 `COALESCE`、回補對算出
> `None` 的個股直接跳過，兩條寫入路徑都無法把已寫入的值清成 `NULL`，需手動
> `UPDATE stock_daily_raw SET limit_up = NULL, limit_down = NULL WHERE ...`。

### OHLCV 來源順序（逐檔請求最小化）

`_fetch_ohlcv_with_fallback` 的 fallback 鏈是：

```
STOCK_DAY_ALL → MI_INDEX → TPEX quotes → STOCK_DAY 月表
  (整批)         (整批)      (整批)        (逐檔 HTTP，最後手段)
```

前三個都是每日各抓一次的全市場批次資料，逐檔組列時只是記憶體查表、零額外請求；
只有 `STOCK_DAY` 月表是**每檔各打一次** `www.twse.com.tw`。順序的重點就是把它墊底：

- **上櫃股（`stocks.market_type = 'tpex'`）完全跳過 `STOCK_DAY`。** 那支 API 只有上市
  資料，對上櫃代號必定回「很抱歉，沒有符合條件的資料!」，打了純粹消耗限流配額。
- `market_type` 未知（`NULL`，或 `--backfill-stocks` 查不到該代號）時維持既有行為往下
  打，不誤殺。`--backfill-stocks` 會另外查 DB 補上市場別。
- `market_type` 欄由下游 TWStockAnalysis repo 維護，本 repo 只讀不寫；`db.py` 只保留
  `ADD COLUMN IF NOT EXISTS` 讓全新 DB 也建得起來。

**為什麼順序重要**：`STOCK_DAY` 原本排在 `MI_INDEX` 前面，只要 `STOCK_DAY_ALL` 沒補滿
五個欄位就會觸發。2026-08-19 它「無法解析日期」而整批棄用，結果 217 檔全部各打一次
API（`逐檔組列` 階段耗時 224 秒 ≈ 217 × `TWSE_MIN_INTERVAL`），數百次請求足以踩到
TWSE 限流——而限流回應與「真的沒資料」是同一個字串，無法區分。`MI_INDEX` 本來就涵蓋
全部上市股且同樣有五欄，提前之後同一情境的逐檔請求從 217 次降到 1 次。

### 休市開關（config.is_trading_day）

「純 daily 模式」（不帶任何參數）啟動時，會先讀共用 `config` 表（由下游
TWStockAnalysis repo 擁有，本 repo 只讀不寫）中 `key = 'is_trading_day'` 的值：

- `false` / `0` / `no`（不分大小寫）→ 印出休市訊息後直接結束（exit 0），不做任何抓取。
- `true` / `1` / `yes` → 照常執行。
- 讀不到（表或 key 不存在、值無法辨識、DB 錯誤）→ **fail-open**：印警告後照常執行。

手動操作（`--date`、`--backfill-*`、`--update-shares`、`--dahu`）**不受**此開關影響，隨時可跑。

### 階段計時 log

每日流程在逐檔進度（`YYYY-MM-DD N/總數 symbol 名稱`）出現之前，還要跑完 DB 連線、
schema、休市檢查、發行股數與數組整批抓取；這些階段原本在 happy path 上完全不印東西，
慢下來時無從歸因。現在每個階段都會輸出起訖與耗時：

```
[階段] DB 連線與 schema 開始
[階段] DB 連線與 schema 完成 0.4s
[階段] 2026-08-17 TWSE 三大法人 開始
[階段] 2026-08-17 TWSE 三大法人 完成 1.2s
[階段] 2026-08-17 TPEX 整批（日行情＋三大法人） 開始
[階段] 2026-08-17 TPEX 整批（日行情＋三大法人） 失敗 56.3s（DataUnavailableError）
[階段] 2026-08-17 外資/法人持股佔比（逐檔 216 檔） 開始
[階段] 2026-08-17 外資/法人持股佔比（逐檔 216 檔） 完成 31.7s
```

- 進入階段時就印「開始」——階段若卡住不返回，至少定位得到卡在哪一段。
- 階段拋例外時印「失敗」與耗時（例外照樣往外拋，不影響既有錯誤處理）。整批抓取包在
  長窗口 retry 裡（`RETRY_ATTEMPTS=6`，backoff 上限約 56 秒），最貴的階段往往正是
  重試到放棄的那個，所以失敗也必須計時。
- 涵蓋的階段：DB 連線與 schema、休市檢查、載入啟用個股、載入發行股數、TWSE 三大法人 /
  STOCK_DAY_ALL / MI_INDEX、TPEX 整批、TWSE 與 TPEX 融資融券、外資/法人持股佔比逐檔、
  逐檔組列、寫入 `stock_daily_raw`、大盤行情。

## Docker

容器以 `PYTHONUNBUFFERED=1` 執行。容器內 stdout 不是 TTY，Python 預設走 8KB 區塊緩衝，
輸出會累積到緩衝區滿才一次沖出——在 Railway 上看起來就像「啟動後靜默數分鐘」，而且同批
沖出的行時間戳只差微秒，無法用來判斷各階段實際耗時。**不要拿掉這個環境變數**，否則上面
的階段計時會失去意義。

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
