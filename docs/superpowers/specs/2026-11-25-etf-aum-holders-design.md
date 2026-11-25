# 熱門 ETF 規模與受益人數 設計

日期：2026-11-25　狀態：待核准

## 目的

記錄幾檔熱門上市 ETF 的**規模**（每天）與**受益人數**（每週），存進 DB 供使用者自己查看。
不給下游 `TWStockAnalysis` 做判斷。

## 範圍

**Goals**
- 起始清單 6 檔：0050、0056、00878、00919、00981A、00403A；清單放 DB，可用 SQL 增減，不需改程式或部署。
- 規模：daily 每天抓（精度到億元即可）。
- 受益人數：`--dahu` 每週抓；`feat/dahu_deployment` 也要有（快轉到 main，見「部署」）。
- 歷史：受益人數回補 TDCC 可查的 51 週；規模用一次性 script 回補 e添富「月規模」12 個月。

**Non-goals**
- 上櫃 ETF。
- 每日受益人數（來源只有每週）、每日規模的歷史（沒有現成來源）。
- 下游判斷、通知。

**Anti-goals**（技術上可行也算失敗）
- 改變既有個股流程的行為：`stock_daily_raw`、`stock_holder_percent` 的寫入結果不可變。
- daily 因 ETF 子步驟失敗而中斷或以 traceback 結束。

## 資料來源（2026-11-25 實測）

| 資料 | 來源 | 頻率 | 歷史 |
|---|---|---|---|
| 受益人數 | TDCC `qryStock` 集保戶股權分散表「合計」列的「人數」（`--dahu` 已在抓同一張表） | 每週結算日 | 最近 51 週（2025-11-28 起） |
| 規模 | TWSE MIS `https://mis.twse.com.tw/stock/data/all_etf.txt`（GET，1 次回全部 ETF，約 367 檔） | 每天 | 無，只有當下快照 |
| 月規模 | e添富 `https://www.twse.com.tw/zh/ETFortune/etfInfo/{symbol}` 頁面內嵌的 `chartData.totalMonthAv` | 每月 | 近 12 個月 |

實測細節：

- **受益人數 = TDCC 週資料**：e添富顯示的受益人次與 TDCC 11-20 那週「合計」人數完全相同
  （0050 = 4,038,137、00981A = 1,024,224），所以沒有每日版本。
- **MIS `all_etf.txt`**：JSON `{"a1":[{"msgArray":[...]}, ...]}`，每筆欄位
  `a` 代號、`b` 名稱、`c` 已發行受益權單位數、`d` 與前日單位差異、`e` 成交價、`f` 預估淨值、
  `g` 預估折溢價、`h` 前一營業日單位淨值、`i` 資料日期（YYYYMMDD）、`j` 時間。
  **數值有的是 JSON number、有的是字串**（`26433000000` vs `"13167709000"`、`"94.8800"`），解析要兩者都吃。
  驗證：0050 `c × h` = 264.33 億單位 × 94.88 = 25,079 億元，與 e添富「資產規模」25,079.4 一致，
  所以 e添富的規模就是 `已發行單位數 × 前一營業日淨值`。
- **e添富 `chartData`**：`<script>` 內 `let chartData = {...};`，可直接當 JSON 解析。
  `totalMonthAv` 12 筆 `{"count": 24556.07, "date": "Oct"}`，單位億元，**標籤只有英文月份、沒有年份**，
  依時間由舊到新。頁面 tooltip：「顯示過去12個月，每個月最後一個營業日的資產規模」。
  2026-11-25 抓到的是 Nov(2025) ~ Oct(2026)，即最後一筆是上個完整月份。

## 方案

- **A（採用）**：新增 `etfs`（清單）＋ `etf_daily`（規模與受益人數同一張表）。
  使用者只是要看數字，一張表一個查詢；不碰既有個股流程。
- **B（不採用）**：受益人數加欄到 `stock_holder_percent`、清單沿用 `stocks`。
  加進 `stocks` 的 ETF 會被 daily 抓個股行情與法人；看資料要 join 兩張表。

## Schema（`db.py` 的 `init_schema`，`CREATE TABLE IF NOT EXISTS`）

```sql
CREATE TABLE IF NOT EXISTS etfs (
    symbol        VARCHAR(10) PRIMARY KEY,
    name          VARCHAR(50) NOT NULL DEFAULT '',
    enabled       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_time  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_time  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS etf_daily (
    symbol        VARCHAR(10)   NOT NULL,
    trade_date    DATE          NOT NULL,
    units         BIGINT,         -- 已發行受益權單位數（MIS c）
    nav           NUMERIC(12,4),  -- 前一營業日單位淨值（MIS h）
    aum_100m      NUMERIC(12,2),  -- 規模（億元）= units × nav ÷ 1e8；月底回補列取 e添富月規模
    holders       INTEGER,        -- 受益人數（TDCC 分散表合計人數；只有週資料日有值）
    created_time  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_time  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, trade_date)
);
```

- 寫入一律 upsert + `COALESCE(EXCLUDED.col, etf_daily.col)`（同 repo 慣例）：daily 與 `--dahu` 寫同一列時互不蓋掉。
- `updated_time`：兩張表各加一個 `BEFORE UPDATE` trigger 呼叫既有的 `update_updated_time()`
  （同 `trg_market_daily_updated` 的寫法，用 `pg_trigger` 檢查避免重複建立）。
- 一列可能只有部分欄位：TDCC 週資料日在 daily 上線前只有 `holders`、月底回補列只有 `aum_100m`。

## 程式架構

| 檔案 | 新增 |
|---|---|
| `sources.py` | `fetch_mis_etf_snapshot(session) -> list[dict]`：GET `all_etf.txt`，攤平 `a1[*].msgArray`；格式不對丟 `DataUnavailableError` |
| `prepare.py` | `prepare_mis_etf_rows(entries, symbols, trade_date) -> list[dict]`：挑出清單內、`i == trade_date` 的筆，轉 `units` / `nav` / `aum_100m`（數字或字串都可）<br>`prepare_tdcc_holder_count(dist) -> int \| None`：取「合計」列人數 |
| `db_utils.py` | `get_enabled_etfs(url) -> list[tuple[str, str]]`、`upsert_etf_daily(url, rows) -> int`（COALESCE） |
| `db.py` | 上面兩張表的 DDL |
| `run.py` | daily 子步驟 `_update_etf_daily`；`_dahu_command` 加 ETF |
| `tools/backfill_etf_month_aum.py` | 一次性月規模回補 |

### daily：`_update_etf_daily(session, config, today)`

- 呼叫點與 `_settle_market_day_trade` 相同（`target_date == today`），但**不受 `written` 限制**：個股失敗不影響 ETF 快照。
  `--date <過去日>`、各種回補都不跑（MIS 只有當下快照，無法回補）。
- 讀 `etfs` 的啟用清單 → 1 次 MIS 請求 → 只寫 `i == today` 的筆；`i` 不是今天（例如休市日手動跑）→ 印警告、不寫。
- 清單內有、MIS 沒有的代號，或 `c` / `h` 不是正數（空值、`-`）→ 該檔不寫、印 `⚠`。
- 任何例外只印 log，不讓 daily 失敗（同其他收尾子步驟）。
- 列的日期 = MIS 資料日期 `i`；`nav` 是前一營業日淨值、`aum_100m = c × h`，與 e添富顯示的規模同口徑。

### `--dahu`：加 ETF 受益人數

- 目標代號：沒給 `--stocks` 時 = 啟用個股 ∪ 啟用 ETF；給了 `--stocks` 時 = 指定的代號（不變）。
- 每個代號每個日期只打 1 次 TDCC（00981A 同時在兩個清單也只打 1 次），再依身分寫：
  - 在 `etfs` → `etf_daily.holders`（`prepare_tdcc_holder_count`）。
  - **不是**「只在 `etfs`、不在 `stocks`」→ 照舊寫 `stock_holder_percent`（既有行為不變）。
- 名稱：ETF-only 代號取 `etfs.name`。
- 歷史回補用既有參數：
  `tw-stock-rawdata --dahu --stocks 0050,0056,00878,00919,00981A,00403A --from 2025-11-28 --to 2026-11-20`
  （6 檔 × 51 週 = 306 次 TDCC 請求；00981A 的 `stock_holder_percent` 會以相同值重寫）。

### `tools/backfill_etf_month_aum.py`（一次性）

- 對每檔啟用 ETF GET e添富頁面（`build_session` 對 www.twse.com.tw 限速；6 檔 = 6 次請求），解析 `chartData.totalMonthAv`。
- 月份→年份：最後一筆對應「今天之前最近的、月份名稱相符的月份」，往前逐月倒推，並驗證標籤連續；不連續就中止。
- 月份→日期：`market_daily` 該月最大的 `trade_date`（最後一個營業日）；該月沒有列就略過並印出。
- `count <= 0`（上市前的月份）略過。
- 只補 `aum_100m IS NULL`（含新增列），不覆蓋 daily 寫的值。
- 預設模擬、`--apply` 才寫；先印目標 DB host；結束 `close_pool()`（同其他 tools）。
- 頁面拿不到 `chartData`（TWSE 限流時回的空殼也會如此）→ 該檔失敗並印出，不重試。

## 部署與上線

1. `feat/etf-daily` → develop → main（使用者合併）。
2. `feat/dahu_deployment` 沒有自己的 commit，快轉到 main 即可（Railway dahu 服務用 `Dockerfile-dahu`，使用者已確認）；
   會一併帶入 7 月以來的其他改動。
3. 各環境（DEV 先、PROD 經同意）：
   - 下次執行時 `init_schema` 自動建表。
   - 用 README 的 SQL 新增 `etfs` 清單 6 筆。
   - 跑受益人數回補（上面的 `--dahu` 指令）與月規模回補 script。

## 文件

- README：新表說明、ETF 清單的 SQL、daily 的 ETF 子步驟、`--dahu` 的 ETF 行為、兩種回補指令、MIS 欄位與口徑。
- CLAUDE.md「資料表」一節加 `etfs` / `etf_daily`。

## 留給實作者的已知風險

- **MIS 在 daily 執行時間（約 21:50）的 `i` 是否仍是當天**：白天實測 `i` = 當天；晚上未驗證。
  實作時要在晚上實際抓一次確認；若晚上已換成隔天，改為「寫 `i` 那天」也成立，只是要在 README 寫清楚。
- e添富月規模與 daily `c × h` 的計算基礎是否完全一致未驗證；給人看的用途可接受。
- TDCC 合計的「股數/單位數」是集保庫存，不等於已發行單位數，**不存**，避免和 `units` 混淆。

## 我替使用者做的決定（請核對）

1. `etfs` 清單用 README 的 SQL 手動新增，不在 `init_schema` 自動塞資料（避免使用者刪掉的又被加回來）。
2. 兩張表不加 foreign key（同 `stock_holder_percent`）。
3. 列日期 = MIS 資料日期；`nav` 存前一營業日淨值，規模口徑同 e添富。
4. `aum_100m` 用 `NUMERIC(12,2)` 億元，另存 `units`、`nav` 原值。
5. daily 的 ETF 子步驟在 `target_date == today` 時跑（含 `--date <今天>`），不看個股是否寫入成功。
6. 月規模回補只補 NULL。
7. 只在 `etfs`、不在 `stocks` 的代號，`--dahu` 不寫 `stock_holder_percent`。
