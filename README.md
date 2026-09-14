# TWStockAnalysis-RawData

每日抓取台股 raw data（OHLCV、三大法人、融資融券、外資/法人/投信持股比例、發行股數、處置股註記、大盤行情），寫入 PostgreSQL，供下游 [`TWStockAnalysis`](https://github.com/MussinaLin/TWStockAnalysis) 分析使用。

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

# 只回補處置股註記（is_disposition / disposition_match_minutes）
tw-stock-rawdata --backfill-disposition --backfill-start 2025-01-01 --backfill-end 2026-08-12

# 只回補處置股註記，且限定特定股票
tw-stock-rawdata --backfill-disposition --backfill-stocks 2330,2317 \
    --backfill-start 2025-01-01 --backfill-end 2026-08-12

# 只回補投信持股比例（trust_holding_pct），可選 --backfill-stocks 限定股票
tw-stock-rawdata --backfill-trust-holding --backfill-start 2024-01-02 --backfill-end 2026-09-14

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

### 處置股（is_disposition / disposition_match_minutes）

`stock_daily_raw` 的 `is_disposition` 記錄該檔該日是否落在交易所公告的處置期間內，
`disposition_match_minutes` 記錄處置期間的撮合間隔分鐘數。處置期間改以人工管制撮合
（不再逐筆連續撮合），會明顯壓抑成交量，下游做量能判斷時應納入。

| `is_disposition` | `disposition_match_minutes` | 意義 |
|---|---|---|
| `TRUE`  | `5` / `10` / `20` / `25` / `45` / `60` | 該日在處置期間內，約每 N 分鐘撮合一次 |
| `TRUE`  | `0` | 在處置期間內，但公告未載明撮合頻率（實測極罕見） |
| `FALSE` | `0` | 該日已成功取得該市場處置名單，此檔不在名單內 |
| `NULL`  | `NULL` | 該日名單取得失敗或尚未回補，**下游需容忍** |

- 資料來源：TWSE「公布處置有價證券資訊」（`rwd/zh/announcement/punish`）與
  TPEX「上櫃處置有價證券資訊」（`www/zh-tw/bulletin/disposal`）。兩支都吃日期區間，
  daily 模式每天各打 1 次。
- 查詢窗口會自動往前推 45 個日曆日：處置期間最長 10 個營業日、公告日又早於期間起日，
  只查當日會漏掉「正處在處置期間中段」的個股。
- **查詢窗口超過約 6 個月時會自動切段**（每段 ≤ 183 個日曆日，兩市場各自切、結果合併）。
  只驗證過 6 個月內不會被端點截斷，更長的窗口沒驗過；而截斷的後果不是「缺 NULL」而是
  **寫錯**——漏掉的公告會讓該檔該日被判定成「已確認非處置」`FALSE/0`，用 `COALESCE`
  蓋掉原本正確的註記。切段後某一段失敗時，該市場整段都不算取得成功（該市場的個股寫
  `NULL`），已取得的段落仍會標出處置日。回補 3 年 = 每市場 7 段、兩市場共 14 次請求。
- 撮合頻率的分級不固定（同樣是「第一次處置」，2026 上半年為每 5 分鐘、8 月起為每 2
  分鐘），故一律從公告內文解析實際數字，不從「第一次／第二次處置」推導。
- 非處置日寫 `0` 而不是 `NULL`：upsert 採 `COALESCE`（NULL 不覆寫舊值），
  若寫 `NULL`，前一段處置留下的分鐘數會永遠清不掉。
- 兩市場獨立降級：只有 TPEX 抓到時，上市個股該日寫 `NULL`（不寫 `FALSE`）——
  「沒查到」不等於「已確認非處置」。處置名單不納入逐檔跳過判定，抓不到不會讓個股整檔不寫。

> **已知限制：處置期間邊界**。公告給的是「預定」區間，遇停止買賣、全日暫停交易會順延，
> 遇有價證券最後交易日則提前結束；交易所沒有提供「當日處置中清單」這種 API，
> 故邊界日可能有誤差。區間中段一律正確。

歷史資料可用 `--backfill-disposition` 回補，整段公告一次查完（長區間自動切段），逐日只 `UPDATE`
已存在的 row（不新增 row）。與 `--backfill-limits` 不同，它**支援** `--backfill-stocks`
限定股票。兩市場名單皆取得失敗時直接放棄、不寫入。結果冪等，可重複執行，不需 `--force`。

### 持股比例（foreign / insti / trust_holding_pct）

`stock_daily_raw` 的 `foreign_holding_pct` / `insti_holding_pct` / `trust_holding_pct`
分別是外資、三大法人合計、投信的持股比例（小數，如 `0.0238` = 2.38%）。三欄都來自
MoneyDJ 法人持股頁（`zcl`），與三大法人買賣超是同一次請求，不額外打 API。

- 頁面只給外資與三大法人的持股比例，投信的要自己算：
  `trust_holding_pct = insti_holding_pct × 投信估計持股 ÷ 三大法人合計估計持股`。
  分母由同一天頁面上的比例反推，就是 MoneyDJ 當天用的股本。
  例：2330 / 2025-07-31 為 76.79% × 618,021 ÷ 19,916,237 = `0.0238`，
  與拿股本 25,935,030,992 股直接算的結果相同。
- **不用 `stocks.issued_shares` 當分母**：那是目前的股本快照，回補歷史時遇到增資、
  減資、配股，舊日期的分母就是錯的。
- 精度與另外兩欄相同（`NUMERIC(8,4)`，到 0.01 個百分點）：`insti_holding_pct` 只有
  兩位小數，反推後誤差不超過 ±0.005 個百分點，多存位數是假精度。
- 合計估計持股 ≤ 0 卻有投信持股時反推不出分母，寫 `NULL`；投信估計持股為 0 時寫 `0`。
- `zcl` 靠欄位位置取值，表頭結構（含估計持股的第 5–8 欄）不符時整頁拒收，
  不會把錯位的數字寫進去。

> **投信持股是估計值，不是官方數字**。交易所每天只公布外資持股；投信、自營商的
> 「持股」是 MoneyDJ 從歷來買賣超累加推估的（推估值為負時照存，不修正）。
> 適合看趨勢，不適合當精確持股使用。

歷史資料可用 `--backfill-trust-holding` 回補：對區間內 DB 已有資料的個股（含現已停用者）
逐檔整段只打 1 次 MoneyDJ（請求數 = 檔數，與區間長短無關；實測 3 年一次查詢不截斷），
只 `UPDATE` 已存在的 row、只寫這一欄，算不出來的日期跳過、不寫 `NULL`。支援
`--backfill-stocks` 限定股票；某檔 MoneyDJ 失敗時繼續下一檔，收尾列出失敗代號供重跑。
結果冪等，可重複執行，不需 `--force`。之後的新資料不必再回補：daily、一般區間回補、
`--backfill-stocks` 都會寫這一欄。

### 回補特定股票（--backfill-stocks）

`--backfill-stocks` 走 **per-stock 區間抓取**，與其他回補模式的成本結構不同：

- OHLCV + 漲跌價差：每檔**每月 1 次**請求（上市走 TWSE `STOCK_DAY`、
  上櫃走 TPEX 個股月表 `afterTrading/tradingStock`）
- 三大法人：每檔**整段 1 次**請求（MoneyDJ `zcl`，與外資/法人/投信持股佔比同一頁，
  故為零額外請求）
- 融資融券：每檔整段 1 次（MoneyDJ）
- 處置股名單：兩市場各切成 ≤ 6 個月的窗口查詢（回補 3 年共 14 次，見上一節）

回補 1 檔 3 年約 **50 次請求**（一般日期區間回補是每個交易日 4 次全市場批次，
同樣範圍約 2,900 次）。**呼叫端不需要自己分段執行或在段間 sleep**：唯一有截斷疑慮的
處置股名單已經由本工具內部自動切段（見上一節），其餘來源都是逐月／逐檔的小請求。

行為細節：
- 某檔的 MoneyDJ（三大法人）整段取得失敗時，該檔**整檔跳過**、連月表都不抓：
  那些價格每一天都過不了三大法人 gating，一列也寫不進去。留待重跑補上。
- 個股中途換市場（上櫃轉上市等）時，`stocks.market_type` 只記得轉換後的市場；
  轉換前的月份在該市場的月表會回空，此時會自動改試另一個市場的月表，不會誤判成限流。
- 月表整月回空、但 MoneyDJ 顯示該月有交易，且兩個市場都拿不到時，判定為限流／取得
  失敗，該月不寫入並印出 ⚠ 警告（請稍後重跑該區間）。單純沒交易的空月份不會印訊息。
- 跑完會印一段收尾摘要：寫入／未寫入天數、三大法人整檔失敗的個股、判定失敗的月份。

已知差異（設計時確認接受）：
- 三大法人與上櫃成交量來自「張」為單位的來源，與交易所股數經 `// 1000` 的結果
  最多差 1 張。
- 上櫃股**除權息日**的漲跌價差，此模式取得的是正確值（daily 模式的來源只給
  文字標記，寫 NULL）。搭配 upsert 的 `COALESCE`，回補過的日子資料較完整。
- `--backfill-stocks` 仍**不寫** `market_daily`。

### OHLCV 來源順序（逐檔請求最小化）

> 本節只適用於 **daily 模式與一般日期區間回補**（`BatchSourceProvider`，走
> `_fetch_ohlcv_with_fallback`）。`--backfill-stocks` 走完全不同的
> `PerSymbolRangeProvider`，不會進到這條 fallback 鏈，見上面「回補特定股票
> （--backfill-stocks）」一節。

`_fetch_ohlcv_with_fallback` 的 fallback 鏈是：

```
STOCK_DAY_ALL → MI_INDEX → TPEX quotes → STOCK_DAY 月表
  (整批)         (整批)      (整批)        (逐檔 HTTP，最後手段)
```

前三個都是每日各抓一次的全市場批次資料，逐檔組列時只是記憶體查表、零額外請求；
只有 `STOCK_DAY` 月表是**每檔各打一次** `www.twse.com.tw`。順序的重點就是把它墊底：

- **上櫃股（`stocks.market_type = 'tpex'`）完全跳過 `STOCK_DAY`。** 那支 API 只有上市
  資料，對上櫃代號必定回「很抱歉，沒有符合條件的資料!」，打了純粹消耗限流配額。
- `market_type` 未知（`NULL`）時維持既有行為往下打，不誤殺。
- `market_type` 欄由下游 TWStockAnalysis repo 維護，本 repo 只讀不寫；`db.py` 只保留
  `ADD COLUMN IF NOT EXISTS` 讓全新 DB 也建得起來。

`--backfill-stocks` 對市場別未知（DB 查不到該代號）的處理方式不同：`PerSymbolRangeProvider`
（見 `_prefetch_symbol_ohlcv`）用第一個抓到資料的月份定調——先試 TWSE STOCK_DAY，
回空再試 TPEX 個股月表，之後整段沿用該市場別；不會經過上面這條 fallback 鏈。

**為什麼順序重要**：`STOCK_DAY` 原本排在 `MI_INDEX` 前面，只要 `STOCK_DAY_ALL` 沒補滿
五個欄位就會觸發。2026-08-19 它「無法解析日期」而整批棄用，結果 217 檔全部各打一次
API（`逐檔組列` 階段耗時 224 秒 ≈ 217 × `TWSE_MIN_INTERVAL`），數百次請求足以踩到
TWSE 限流——而限流回應與「真的沒資料」是同一個字串，無法區分。`MI_INDEX` 本來就涵蓋
全部上市股且同樣有五欄，提前之後同一情境的逐檔請求從 217 次降到 1 次。

### 來源回應的驗證政策

各端點回應的 `stat` 欄判準**刻意不統一**，分野有實測依據（2026-08-24 驗證）：

- **TWSE 端點採嚴格判準**（`stat` 必須是 `"OK"`）。缺 `stat` 鍵也視為異常——實測
  MI_INDEX 在交易日、休市日、無效參數、缺參數四種情境下一律回傳帶 `stat` 的 dict。
- **TPEX 端點採寬鬆判準**（接受缺鍵與小寫 `"ok"`）。小寫 `ok` 確實只出現在 TPEX。

**不要為了「統一」把 TWSE 放寬**：`www.twse.com.tw` 被限流時回 HTTP 200 +
`{"stat":"很抱歉，沒有符合條件的資料!"}`，與「該日真的沒資料」是同一個字串，
無法從回應區分。放寬判準只會讓異常 payload 更容易被當成有效資料。

抓取的日期驗證同樣分兩種政策：

- `STOCK_DAY_ALL` / `MI_INDEX` / TWSE 融資融券：資料日期不等於請求日期就**棄用**
  （TWSE 尚未發布時會回前一日資料，寫下去等於把 D-1 標成 D）。
- TPEX 整批：日期不符只印訊息、**資料照用**。
- `MI_INDEX` 額外有一條 fallback：端點沒宣告日期但確實有資料、且抓的就是今天時，
  視為今天；歷史日不做這個推定。

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

### 重構前後的資料一致性驗證

`tools/replay/` 是一套錄製／重放比對工具：用真實上游回應驗證「重構前後產出是否相同」。
單元測試餵的是合成輸入，證明不了這件事。

```bash
git worktree add --detach /tmp/base-tree <重構前的 commit>
python tools/replay/capture.py --src /tmp/base-tree/src --mode daily --record \
    --tape /tmp/tape.json --out /tmp/base.json
python tools/replay/capture.py --src ./src --mode daily \
    --tape /tmp/tape.json --out /tmp/dev.json
python tools/replay/compare.py /tmp/base.json /tmp/dev.json base dev
```

三種模式（`daily` / `dahu` / `backfill-stocks`）分別涵蓋三條抓取路徑，要三種都跑
才算全覆蓋。比對的是每次 `fetch_*` / `prepare_*` 的呼叫順序、參數、完整回傳值，
外加所有 DB 寫入內容。詳見 `tools/replay/README.md`。

**為什麼是錄製／重放而不是各跑一次**：直接讓兩個版本各打一次 API，請求量翻倍
（20 天約 17,680 次），且 TWSE 限流回應與「沒資料」同字串，上游不穩會被誤判成
程式差異。錄一次、重放兩次，網路只打 1×，重放完全離線可無限重跑。

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
pytest                     # 全部（668 個，無網路、無 DB）

# 覆蓋率
pytest --cov=src/tw_stock_rawdata --cov-report=term-missing
```

測試不碰網路也不碰 DB：`tests/conftest.py` 提供兩組共用替身——

- `FakeCursor` / `FakeConn` / `FakePool` + `install_fake_pool(monkeypatch, module, ...)`：
  psycopg 連線池。注意 patch 要打在**呼叫 `get_pool` 的那個模組**（通常是 `db_utils`），
  不是定義它的 `db`——`from .db import get_pool` 綁定的名稱不會被換掉。
- `FakeResponse` / `FakeSession`：HTTP 回應，同時支援 `.text` 與 `.json()`，
  並記錄每次 GET 的 `(url, params)` 供斷言。

目前覆蓋率（`pytest --cov`）：

| 模組 | 覆蓋率 |
|---|---:|
| `price_limit.py` | 100% |
| `db_utils.py` | 99% |
| `config.py` | 90% |
| `run.py` | 88% |
| `sources.py` | 81% |
| `prepare.py` | 76% |
| `db.py` | 37% |
| **總計** | **85%** |

`db.py` 偏低是因為它主體是約 150 行的 schema DDL 字串與連線池，需要真的 DB 才走得到；
其邏輯分支極少（radon 平均 CC 1.75，無任何 rank B 以上函式）。
