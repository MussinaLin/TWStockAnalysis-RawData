# CLAUDE.md

## Workflow

- **所有涉及 coding、架構規劃、寫程式的任務，一律請先設計完架構並釐清所有實作細節，有疑問的地方提出討論，沒問題再開始實作。** 不可以未經討論就直接動手寫 code。

- **每次改動必須同步更新 `README.md`，讓 `README.md` 保持最新狀態。** 包括但不限於：新增/修改 CLI 參數、新增/修改資料抓取邏輯、新增/修改資料表。

## Refactor rules

- 行為不可改變。每次改動後跑 pytest，全綠才算完成。
- 一次只做一種 transformation，一個 commit。
- 不新增抽象層，除非同一段邏輯已重複兩次以上。**確認邏輯相同就重用程式碼**；
  結構相似但語意不同（欄位、單位、端點、來源不同）者不算，需個別判斷是否值得參數化。
- 不動 public API 簽名，除非我明確要求。
- 沒有測試覆蓋到的檔案，先跟我確認再改。

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
tw-stock-rawdata --backfill-disposition --backfill-start ... --backfill-end ...  # 只回補處置股註記
tw-stock-rawdata --backfill-trust-holding --backfill-start ... --backfill-end ...  # 只回補投信持股比例
tw-stock-rawdata --backfill-margin --backfill-start ... --backfill-end ...  # 只回補融資融券（交易所逐日整批；餘額以隔日前日餘額為準）
tw-stock-rawdata --backfill-futures-oi --backfill-start ... --backfill-end ...  # 只回補台指期三大法人未平倉（期交所整段 1 發、只近三年）
tw-stock-rawdata --backfill-vix --backfill-start ... --backfill-end ...  # 只回補台指 VIX（期交所月檔每月 1 發、只近 4 個月）
tw-stock-rawdata --update-shares      # 只刷新 stocks.issued_shares
tw-stock-rawdata --dahu               # 每週：大戶/散戶持股佔比 + etfs 清單的 ETF 受益人數（TDCC）
python tools/backfill_etf_month_aum.py [--apply]  # 一次性：ETF 近 12 個月月底規模（e添富，只補 NULL）
python tools/backfill_mini_futures_oi.py [--start YYYY-MM-DD] [--apply]  # 一次性：小台／微台未平倉 6 欄（只 UPDATE）

# 測試
pytest tests/unit/
```

## 架構（src/tw_stock_rawdata/）

- `run.py`     — CLI 入口、每日抓取編排（`_run_for_date` 為主流程）
- `sources.py` — 對外抓資料（TWSE / TPEX / MoneyDJ / TDCC / TAIFEX），含 retry 機制
- `prepare.py` — 把各來源回傳的 DataFrame normalize 成標準欄位
- `db_utils.py`— upsert 邏輯（stocks / daily_raw / market_daily）
- `db.py`      — 連線池、schema 建立、年度 partition
- `config.py`  — 從 .env 讀 `DATABASE_URL` / `USE_DB`

## 資料表

PG infra（docker compose）由下游 `TWStockAnalysis` repo 擁有；本 repo 只擁有
`stock_daily_raw` 的年度 partition。主要的表：

- `stocks` — 個股 master（symbol PK、issued_shares、enabled、alpha_pick_enabled）
- `stock_daily_raw` — 每日逐檔 raw（PK `(symbol, trade_date)`，**按年 RANGE partition**）
  含處置股註記 `is_disposition` / `disposition_match_minutes`（語意見 README）
- `market_daily` — 大盤每日行情（trade_date PK），含台指期（大台）三大法人未平倉淨額 `txf_*`、
  台指 VIX 收盤值 `tw_vix_close`、上市大盤當沖統計 `day_trade_*`、小台／微台全市場與三大法人多空單
  未平倉 `mtx_*`／`tmf_*`（原始口數，散戶多空比由下游算）
- `stock_holder_percent` — 每週大戶／散戶持股佔比（`--dahu`，TDCC）
- `etfs` — 要追蹤的 ETF 清單（手動維護）；`etf_daily` — ETF 每日規模（daily，MIS 快照）
  與每週受益人數（`--dahu`），PK `(symbol, trade_date)`，一列可能只有部分欄位

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
- **處置股欄位的 `0` 是刻意的哨兵值**：`disposition_match_minutes` 在非處置日寫 `0`
  而非 `NULL`。upsert 的 COALESCE 不以 NULL 覆寫舊值，若非處置寫 NULL，前一段處置
  留下的分鐘數會永遠清不掉，變成「非處置卻每 20 分鐘撮合」的矛盾列。搭配
  `is_disposition` 三態讀：TRUE=處置中、FALSE=已確認非處置、NULL=名單沒抓到。
  「沒抓到」與「已確認非處置」必須分開 —— 只有該市場公告確實取得時才寫 FALSE
  （見 `DispositionData.resolve`）。處置名單**不可**納入 `_stock_sources_ok`：
  它只是註記欄，抓不到不該讓整檔個股跳過不寫。
- **處置公告查詢窗口要往前推 45 天**（`_DISPOSITION_LOOKBACK_DAYS`）：處置期間最長
  10 個營業日、公告日又早於期間起日，只查當日會漏掉正處在處置期間中段的個股。
  端點吃區間查詢，窗口拉長不增加請求數。同理**不要**改用 OpenAPI 快照版
  （`announcement/punish`、`tpex_disposal_information`）—— 那兩支只回最近幾個公布日。
- **`trust_holding_pct` 的分母不用 `stocks.issued_shares`**：頁面沒給投信持股比例，
  由同頁 `insti_holding_pct × 投信估計持股 ÷ 合計估計持股` 反推（見
  `prepare_moneydj_holding_pct`），分母就是 MoneyDJ 當天用的股本。`issued_shares`
  是目前的快照，回補歷史遇到增資、減資、配股會算錯。zcl 全靠位置取欄，第 6 / 8 欄
  由 `_check_moneydj_zcl_header` 驗表頭，不可省略。
- **`change` 不可取自 `STOCK_DAY_ALL`**：漲跌價差只從 `MI_INDEX`（上市）與 TPEX
  quotes（上櫃）取。`STOCK_DAY_ALL` 在除權息日給 `Change=0.0000` 且無任何標記，
  而它是 `_fetch_ohlcv_with_fallback` 的第一順位；若讓它供應 change，除權息日會
  算出「參考價 = 收盤」的錯誤漲跌停價。另：change 的取得寫成獨立區塊，**不可**加進
  `STOCK_DAY` 逐檔月表區塊的 `any(v is None ...)` 觸發條件（那是逐檔 HTTP，
  加進去會讓每檔都多打一次 API）。
- **重寫歷史日後要收尾校正大盤融資**（`_settle_market_margin`）：處理 D 寫入的是當日速報
  餘額，只有處理 D+1 才校正 D，所以 `--date D` 與回補最後一天要另外用 D+1 的前日餘額校正。
  只對最後寫入日做 1 次，**不要**改成每天都做（回補 N 天就多打 N 次 TWSE）。
- **`--backfill-futures-oi` 只 UPDATE 不 INSERT `market_daily`**：只有期貨欄的列沒有大盤
  行情，卻會被 `find_consensus_prev_trade_date` 當成 D-1，打亂融資融券校正。期交所的錯誤
  （休市「查無資料」、超過近三年「日期時間錯誤」）都是 HTTP 200 + HTML alert，靠內容判斷。
- **台指 VIX daily 用當日檔、不可改用月檔**：月檔在收盤後數小時內還沒有當日列。當日檔找不到
  13:45 那一列就寫 NULL，**不要**退而取最後一筆 tick（盤中不完整的檔案最後一筆不是收盤）。
  `--backfill-vix` 用月檔（只保留近 4 個月），同樣只 UPDATE 不 INSERT `market_daily`。
- **當沖統計校正只在 daily 做 1 次、對象是 DB 往前第 3 個交易日**（`_settle_market_day_trade`）：
  TWSE 以 T+2 為最終值，但 T+2 更新與 22:00 的 daily 幾乎同時，**不要**改成第 2 個；也**不要**
  改成回補每天都做（舊日期抓到的已是最終值，回補 N 天就多打 N 次 TWSE）。
- **小台／微台未平倉（`mtx_*`／`tmf_*`）的兩個期交所端點規則不同**：法人 `futContractsDateDown` 小台代碼是
  `MXF`（填 `MTX` 回「查無資料」）、只保留近三年；行情 `futDataDown` 小台代碼才是 `MTX`、**區間上限一個月**、
  **休市回只有表頭的 CSV 而非 alert**、每列有尾逗號（讀檔要 `index_col=False`，否則整列錯位）。
  全市場未平倉只加總「一般」單式列（含週契約），價差與盤後列不算。`tools/backfill_mini_futures_oi.py`
  只 UPDATE 不 INSERT（理由同 `--backfill-futures-oi`）。
