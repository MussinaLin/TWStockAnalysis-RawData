# market_daily 台指 VIX 與大盤當沖統計 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** daily run 寫入台指 VIX 收盤值與上市當沖統計 6 欄到 `market_daily`，當沖以往前第 3 個交易日重抓校正，並提供 `--backfill-vix` 補近 4 個月 VIX。

**Architecture:** 兩個新子來源併入 `run._fetch_and_upsert_market_daily`（各自 try/except、失敗印 log）。只 UPDATE 的批次寫入抽成 `db_utils._update_market_columns_batch`，期貨未平倉、VIX 回補、當沖校正共用。當沖校正只在 daily 由 `_main_inner` 呼叫一次。

**Tech Stack:** Python 3.13、requests、pandas、psycopg 3、pytest。

**Spec:** `docs/superpowers/specs/2026-11-23-market-vix-daytrade-design.md`

## Global Constraints

- branch `feat/market-vix-daytrade`；每個 commit `pytest tests/unit/ -q` 全綠；commit message **不加** `Co-Authored-By`。
- 每次改動同步更新 `README.md`（CLAUDE.md 規則）；新 CLI 旗標同步更新 `CLAUDE.md` Commands。
- 新欄位：`tw_vix_close NUMERIC(8,2)`、`day_trade_shares BIGINT`、`day_trade_shares_pct NUMERIC(6,4)`、`day_trade_buy_amount BIGINT`、`day_trade_buy_amount_pct NUMERIC(6,4)`、`day_trade_sell_amount BIGINT`、`day_trade_sell_amount_pct NUMERIC(6,4)`。占比存小數（26.1 → 0.261）。
- 新欄必須同時出現在 `db.py` 的 `CREATE TABLE market_daily` 與 `ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS`。
- 回補／校正只 UPDATE、不 INSERT `market_daily`；COALESCE 不以 NULL 覆寫舊值。
- TWSE 端點 `stat` 必須是 `"OK"`、`payload["date"]` 必須等於請求日，否則 `DataUnavailableError`（訊息帶 `stat` 原文）。
- 期交所兩支 VIX fetcher 用 `session.get(..., timeout=30)`，維持預設 TLS 驗證，不經 `_http_get`；TWTB4U 走 `_http_get`。三支都加 `@_retry_on_transient`。
- 每個子來源失敗都印 `  <來源名稱>取得失敗：{原因}`，不可靜默。
- 不動既有 public 函式簽名（`update_market_futures_oi_batch` 等）。

## Review Focus

1. 期交所檔案行尾混用 CRLF／LF、結尾有空行（實測就是混用）→ 照常解析。→ Task 2 `test_mixed_line_endings_and_trailing_blank_lines`
2. 期交所回傳無法以 cp950 解碼的位元組 → `DataUnavailableError`，不可拋 `UnicodeDecodeError` 繞過 daily 的 except。→ Task 2 `test_undecodable_bytes_raise_data_unavailable`
3. TWTB4U 單一數值是 `--` 或空字串 → 該欄 None，其餘 5 欄照寫。→ Task 5 `test_unparseable_value_becomes_none`
4. `--backfill-vix` 範圍跨到 4 個月以前 → 舊月份印警告，較新月份照常更新。→ Task 4 `test_unavailable_month_is_warned_and_others_continue`
5. 校正時 TWSE 限流或回別日資料 → 印 log、不 UPDATE、daily 正常結束。→ Task 7 `test_fetch_failure_is_logged_without_update`

---

### Task 1: refactor — 抽出 `_update_market_columns_batch`

**Files:**
- Modify: `src/tw_stock_rawdata/db_utils.py`（`update_market_futures_oi_batch`，約 566-608 行）
- Test: `tests/unit/test_db_utils_queries.py`（`TestUpdateMarketFuturesOiBatch` 旁新增 class）

**Interfaces:**
- Produces: `_update_market_columns_batch(database_url: str, col_types: dict[str, str], updates: list[tuple[dt.date, dict]]) -> int`。`col_types` 為欄名 → PG 型別（如 `"bigint"`、`"numeric"`），依插入順序決定 VALUES 欄序；回傳 UPDATE 的 row 數合計。
- `update_market_futures_oi_batch` 簽名與行為不變，改成呼叫上面的 helper（`{c: "bigint" for c in _FUTURES_OI_COLS}`）。

- [ ] **Step 1: 寫測試** `TestUpdateMarketColumnsBatch`：
  - `test_casts_each_column_by_its_type`：`col_types={"a": "bigint", "b": "numeric"}`、1 筆 update → SQL 含 `(%s::date, %s::bigint, %s::numeric)`、`a = COALESCE(v.a, t.a)`、`AS v(trade_date, a, b)`，不含 `INSERT`。
  - `test_empty_skips_db`：空 updates 回 0、沒有 execute、沒有 commit。
- [ ] **Step 2:** `pytest tests/unit/test_db_utils_queries.py -q` → 新測試 FAIL（AttributeError），既有測試 PASS。
- [ ] **Step 3:** 實作 helper，把既有本體（分批 `_BATCH_UPDATE_CHUNK`、`_safe`、rowcount 加總、commit）原樣搬進去，只把 cast 換成逐欄型別。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠（`TestUpdateMarketFuturesOiBatch` 不改一行仍通過）。
- [ ] **Step 5: Commit** `refactor(db_utils): 抽出 market_daily 只 UPDATE 的批次寫入 _update_market_columns_batch`

### Task 2: VIX fetchers（sources.py）

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（URL 常數區與 TAIFEX 區段，`fetch_taifex_txf_institutional` 之後）
- Create: `tests/unit/test_taifex_vix.py`

**Interfaces:**
- Produces:
  - `TAIFEX_VIX_DAILY_URL = "https://www.taifex.com.tw/cht/7/getVixData"`（params `{"filesname": "YYYYMMDD"}`）
  - `TAIFEX_VIX_MONTH_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{yyyymm}new.txt"`
  - `fetch_taifex_vix_close(session: requests.Session, date: dt.date) -> float`
  - `fetch_taifex_vix_month(session: requests.Session, month: dt.date) -> dict[dt.date, float]`（`month` 取其年月）

測試用假 session：`get()` 回傳帶 `.content`（bytes）與 `raise_for_status()` 的物件（conftest 的 `FakeResponse` 沒有 `.content`，在本檔自建，參考 `test_taifex_futures_oi.py` 的 `_Resp`）。fixture 以 cp950 編碼、tab 分隔，表頭與分隔線照實測：

```
交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數
--------\t-------------------\t--------------------
20261120\t13443000\t\t\t52.24
20261120\t13450000\t\t\t52.13
20261120\tLast 1 min AVG\t\t\t52.20
```

月檔表頭多一欄 `收盤前1分鐘平均指數`，資料列如 `20261119\t13450000\t\t\t49.29\t\t49.27`。

- [ ] **Step 1: 寫測試**
  - `test_close_is_the_1345_row`：上面 fixture、date=2026-11-20 → `52.13`；請求 params `{"filesname": "20261120"}`。
  - `test_missing_1345_row_raises`：只有 `13443000` 那列 → `DataUnavailableError`（訊息含 `13:45`）。
  - `test_other_date_rows_are_ignored`：檔內只有 20261119 的 13450000 → `DataUnavailableError`。
  - `test_html_response_raises`：content 為 `b"<!DOCTYPE HTML ..."` → `DataUnavailableError`。
  - `test_header_mismatch_raises`：表頭第 3 欄改名 → `DataUnavailableError`。
  - `test_month_returns_close_by_date`：兩列（11/19、11/20）→ `{date(2026,11,19): 49.29, date(2026,11,20): 52.13}`；請求 URL 含 `202611new.txt`。
  - `test_month_html_raises_with_retention_hint`：HTML → `DataUnavailableError`，訊息含 `近 4 個月`。
  - `test_mixed_line_endings_and_trailing_blank_lines`（Review Focus 1）：同一份內容混用 `\r\n`／`\n` 並以 `\n\n` 結尾 → 兩支都照常解析。
  - `test_undecodable_bytes_raise_data_unavailable`（Review Focus 2）：content=`b"\xff\xfe\x00garbage"` → 兩支都是 `DataUnavailableError`。
- [ ] **Step 2:** `pytest tests/unit/test_taifex_vix.py -q` → FAIL（ImportError）。
- [ ] **Step 3:** 實作兩支 fetcher。共用的「bytes → 驗表頭 → 資料列 list」解析若兩支邏輯相同就抽一個私有 helper（`_parse_taifex_vix_rows(content: bytes, expected_header: list[str]) -> list[list[str]]`）；分隔用 `line.split("\t")` 後濾掉空字串。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5: Commit** `feat(sources): 新增期交所台指 VIX 當日收盤與月檔 fetcher`

### Task 3: VIX 寫入 market_daily（schema、upsert、daily）

**Files:**
- Modify: `src/tw_stock_rawdata/db.py`（`CREATE TABLE market_daily` 與 `txf_*` ALTER 之後）
- Modify: `src/tw_stock_rawdata/db_utils.py`（`_FUTURES_OI_COLS` 旁、`upsert_market_daily`、新增 batch wrapper）
- Modify: `src/tw_stock_rawdata/run.py`（`_fetch_and_upsert_market_daily`，台指期未平倉那段之後）
- Test: `tests/unit/test_db_schema.py`、`tests/unit/test_db_utils_queries.py`、`tests/unit/test_market_daily.py`

**Interfaces:**
- Consumes: Task 1 `_update_market_columns_batch`；Task 2 `fetch_taifex_vix_close`。
- Produces:
  - `db_utils._VIX_COL_TYPES: dict[str, str] = {"tw_vix_close": "numeric"}`
  - `update_market_vix_batch(database_url: str, updates: list[tuple[dt.date, dict]]) -> int`
  - `upsert_market_daily` 的欄位清單含 `tw_vix_close`。

- [ ] **Step 1: 寫測試**
  - `test_db_schema.py`：`test_vix_column_in_create_table`（`tw_vix_close\s+NUMERIC\(8,2\)`）、`test_vix_column_has_online_migration`。
  - `test_db_utils_queries.py`：`upsert_market_daily(URL, d, {"tw_vix_close": 52.13})` → SQL 含 `tw_vix_close = COALESCE(EXCLUDED.tw_vix_close, market_daily.tw_vix_close)`、params 值為 52.13；`update_market_vix_batch` SQL 含 `%s::numeric`、不含 `INSERT`。
  - `test_market_daily.py`：`wiring` fixture 加 `monkeypatch.setattr(run, "fetch_taifex_vix_close", lambda s, d: 52.13)`；`test_vix_merged_into_same_upsert`（寫入 dict 含 `tw_vix_close == 52.13`）；`test_vix_failure_is_logged_and_keeps_others`（VIX 丟 `DataUnavailableError("x")` → capsys 含 `台指 VIX 取得失敗：x`，其他欄照寫、無 `tw_vix_close` 鍵）。
- [ ] **Step 2:** 跑上面三個檔 → 新測試 FAIL。
- [ ] **Step 3:** 實作 schema（CREATE TABLE 加欄 + ALTER，附一行註解說明來源與 13:45 收盤）、`_VIX_COL_TYPES`、`upsert_market_daily` 加欄、`update_market_vix_batch`、daily 子來源 try/except。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5: Commit** `feat(market_daily): daily 寫入台指 VIX 收盤值 tw_vix_close`

### Task 4: `--backfill-vix` 指令與 VIX 文件

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（argparse 約 424 行旁、`_is_daily_mode`、`_main_inner` 分派於 `--backfill-futures-oi` 之後、新增 `_backfill_vix_command` 於 `_backfill_futures_oi_command` 之後）
- Modify: `README.md`、`CLAUDE.md`
- Create: `tests/unit/test_backfill_vix.py`
- Modify: `tests/unit/test_main_inner_dispatch.py`（`_args` base 加 `"backfill_vix": False`；`spies` 加 `_backfill_vix_command`；routing parametrize 加一組）

**Interfaces:**
- Consumes: Task 2 `fetch_taifex_vix_month`；Task 3 `update_market_vix_batch`；既有 `load_market_dates_in_range`、`_build_date_range`、`_parse_date`。
- Produces: `_backfill_vix_command(session: requests.Session, config: AppConfig, args: argparse.Namespace) -> None`；CLI `--backfill-vix`（`store_true`，`args.backfill_vix`）。

行為照 spec「`--backfill-vix`」一節；結構仿 `_backfill_futures_oi_command`（同樣的忽略旗標警告、缺範圍錯誤、DB 無列略過、兩邊差異列出），差別是按 DB 日期涵蓋的月份逐月抓。

- [ ] **Step 1: 寫測試**（`test_backfill_vix.py`，fixture 仿 `test_backfill_futures_oi.py` 的 `backfill`）
  - `test_fetches_once_per_month_covering_db_dates`：DB 有 10/30、11/02、11/20 → 抓 2026-10、2026-11 各一次。
  - `test_updates_only_db_dates`：月檔含 DB 沒有的日期 → 不在 updates 內，且印 `ℹ` 行列出。
  - `test_db_dates_missing_from_taifex_are_reported`：印 `⚠` 行。
  - `test_unavailable_month_is_warned_and_others_continue`（Review Focus 4）：2026-07 丟 `DataUnavailableError` → 印警告含 `2026-07`，2026-08 照常更新。
  - `test_no_db_rows_in_range_skips_fetch`、`test_missing_range_is_an_error`、`test_backfill_stocks_and_date_are_ignored_with_warning`。
  - dispatch：`("backfill_vix", "backfill_vix")` 加入 `test_flag_routes_and_returns`；`_is_daily_mode(_args(backfill_vix=True))` 為 False。
- [ ] **Step 2:** 跑兩個測試檔 → FAIL。
- [ ] **Step 3:** 實作 argparse（help：`只回補 market_daily 的台指 VIX 收盤值（期交所只提供近 4 個月，每月 1 次請求）`）、`_is_daily_mode`、分派、`_backfill_vix_command`。
- [ ] **Step 4:** 文件：
  - README：CLI 範例區加 `tw-stock-rawdata --backfill-vix --backfill-start 2026-08-01 --backfill-end 2026-11-20`；新增「台指 VIX 與大盤當沖（market_daily）」一節的 VIX 部分（欄位語意＝13:45 收盤、daily 用當日檔、只近 4 個月、`--backfill-vix` 只 UPDATE）；「來源失敗一律印 log」的 `market_daily` 子來源清單加 `台指 VIX`。
  - CLAUDE.md：Commands 加 `--backfill-vix` 一行；Gotchas 加「`--backfill-vix` 只 UPDATE 不 INSERT（理由同 futures-oi）；daily 不可改用月檔（收盤後數小時內月檔還沒有當日列）」；資料表段落 `market_daily` 補「台指 VIX」。
- [ ] **Step 5:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 6: Commit** `feat(market_daily): 新增 --backfill-vix 回補期交所近 4 個月台指 VIX`

### Task 5: TWTB4U fetcher（sources.py）

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（URL 常數區；fetcher 放在 `fetch_twse_market_margin` 之後）
- Test: `tests/unit/test_market_daily_fetchers.py`（新增 `TestFetchTwseDayTrade`）

**Interfaces:**
- Produces:
  - `TWSE_DAY_TRADE_URL = "https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U"`（params `{"date": "YYYYMMDD", "selectType": "All", "response": "json"}`）
  - `fetch_twse_day_trade(session: requests.Session, date: dt.date) -> dict[str, int | float | None]`，鍵固定為 `day_trade_shares`、`day_trade_shares_pct`、`day_trade_buy_amount`、`day_trade_buy_amount_pct`、`day_trade_sell_amount`、`day_trade_sell_amount_pct`（對應 spec 表格 6 個中文欄位，依序）。

fixture payload（11/20 實測）：`{"stat": "OK", "date": "20261120", "tables": [{"title": "115年11月20日 當日沖銷交易統計資訊", "fields": [<spec 的 6 個欄名>], "data": [["4,460,672,000", "26.1", "322,561,954,550", "34.48", "322,720,245,100", "34.50"]]}, {"title": "115年11月20日 當日沖銷交易標的及成交量值", ...}]}`

- [ ] **Step 1: 寫測試**
  - `test_returns_six_fields`：→ `day_trade_shares == 4460672000`、`day_trade_shares_pct == pytest.approx(0.261)`、`day_trade_buy_amount == 322561954550`、`day_trade_buy_amount_pct == pytest.approx(0.3448)`、`day_trade_sell_amount == 322720245100`、`day_trade_sell_amount_pct == pytest.approx(0.3450)`；請求 params 與 URL 如上。
  - `test_stat_not_ok_raises_with_stat`：`RATE_LIMITED` → 訊息含 `很抱歉`。
  - `test_date_mismatch_raises`：`date="20261119"` → `DataUnavailableError`。
  - `test_summary_table_missing_raises`、`test_header_change_raises`（任一欄名不同）、`test_row_count_not_one_raises`。
  - `test_unparseable_value_becomes_none`（Review Focus 3）：股數欄 `"--"`、占比欄 `""` → 這兩鍵為 None，其餘 4 鍵照常。
- [ ] **Step 2:** `pytest tests/unit/test_market_daily_fetchers.py -q` → FAIL。
- [ ] **Step 3:** 實作（`_clean_int` / `_clean_number`；占比 `÷ 100`，None 保持 None）。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5: Commit** `feat(sources): 新增 TWSE 當沖統計 TWTB4U fetcher`

### Task 6: 當沖寫入 market_daily（schema、upsert、daily、DB helper）

**Files:**
- Modify: `src/tw_stock_rawdata/db.py`、`src/tw_stock_rawdata/db_utils.py`、`src/tw_stock_rawdata/run.py`（`_fetch_and_upsert_market_daily`，VIX 那段之後）
- Test: `tests/unit/test_db_schema.py`、`tests/unit/test_db_utils_queries.py`、`tests/unit/test_market_daily.py`

**Interfaces:**
- Consumes: Task 1 helper；Task 5 `fetch_twse_day_trade`。
- Produces:
  - `db_utils._DAY_TRADE_COL_TYPES: dict[str, str]`：三個股數／金額欄 `"bigint"`、三個 `_pct` 欄 `"numeric"`，順序同 Task 5 的鍵。
  - `update_market_day_trade_batch(database_url: str, updates: list[tuple[dt.date, dict]]) -> int`
  - `find_prev_market_trade_date(database_url: str, date: dt.date, n: int) -> dt.date | None`：`market_daily` 中早於 `date` 的第 n 個 trade_date（`n=1` 為前一個）。

- [ ] **Step 1: 寫測試**
  - schema：6 欄各自在 CREATE TABLE（型別照 Global Constraints）與 ALTER 出現（parametrize over `_DAY_TRADE_COL_TYPES`）。
  - db_utils：`upsert_market_daily` 含 6 欄 COALESCE；`update_market_day_trade_batch` 的 cast 依型別；`find_prev_market_trade_date(URL, date(2026,11,23), 3)` 的 SQL 為 `trade_date < %s ORDER BY trade_date DESC OFFSET %s LIMIT 1`、params `(date, 2)`，fetchone 回 None 時回 None。
  - market_daily：`wiring` 加 `fetch_twse_day_trade` 假回傳 6 欄；`test_day_trade_merged_into_same_upsert`；`test_day_trade_failure_is_logged_and_keeps_others`（capsys 含 `大盤當沖統計取得失敗：`）。
- [ ] **Step 2:** 跑三個檔 → FAIL。
- [ ] **Step 3:** 實作（schema 註解寫明 TWSE 原樣、占比為小數、T+2 為最終值）。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5: Commit** `feat(market_daily): daily 寫入上市當沖統計 6 欄`

### Task 7: 當沖校正 `_settle_market_day_trade` 與文件

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（`_settle_market_margin` 之後新增函式；`_main_inner` 單日分支末尾）
- Modify: `README.md`、`CLAUDE.md`
- Create: `tests/unit/test_settle_market_day_trade.py`
- Modify: `tests/unit/test_main_inner_dispatch.py`（`spies` 加 `_settle_market_day_trade` 記錄呼叫日期）

**Interfaces:**
- Consumes: Task 5 `fetch_twse_day_trade`；Task 6 `find_prev_market_trade_date`、`update_market_day_trade_batch`。
- Produces: `_DAY_TRADE_SETTLE_LAG = 3`；`_settle_market_day_trade(session: requests.Session, config: AppConfig, today: dt.date) -> None`。

`_main_inner` 單日分支：`if written and target_date == today: _settle_market_day_trade(session, config, target_date)`（放在既有 `_settle_market_margin` 判斷之後；兩者條件互斥）。

- [ ] **Step 1: 寫測試**
  - `test_refetches_third_previous_trade_date`：`find_prev_market_trade_date` 回 2026-11-18 → 以 11/18 呼叫 fetch，`update_market_day_trade_batch` 收到 `[(date(2026,11,18), {...6 欄})]`；確認呼叫 `find_prev_market_trade_date(url, today, 3)`。
  - `test_not_enough_history_skips_without_request`：回 None → 不 fetch、印略過訊息。
  - `test_fetch_failure_is_logged_without_update`（Review Focus 5）：fetch 丟 `DataUnavailableError("很抱歉，沒有符合條件的資料!")` 或 `requests.RequestException` → capsys 含原因、不呼叫 update、不拋例外。
  - `test_db_error_is_contained`：update 丟 `Exception` → 只印 log。
  - dispatch：`test_daily_settles_day_trade_once`（`_args()`、target=today → 記錄 `[TODAY]`）；`test_explicit_date_today_also_settles`（`_args(date=TODAY.isoformat())`）；`test_historical_date_does_not_settle_day_trade`；`test_unwritten_today_does_not_settle_day_trade`；`test_backfill_does_not_settle_day_trade`。
- [ ] **Step 2:** 跑兩個測試檔 → FAIL。
- [ ] **Step 3:** 實作（docstring 寫明為何是第 3 個交易日、同時是 D 日未公布的備援、只在 daily 做的理由）。
- [ ] **Step 4:** 文件：
  - README「台指 VIX 與大盤當沖」一節補當沖部分：6 欄語意與 11/20 實測值、占比定義（TWSE 附註）、T+2 為最終值、daily 以往前第 3 個交易日校正（理由與「某天 daily 沒跑就少一次校正」的已知缺口）、`--date`／回補不校正；log 清單加 `大盤當沖統計`。
  - CLAUDE.md Gotchas 加「當沖校正只在 daily 做 1 次、對象是往前第 3 個交易日；**不要**改成回補每天都做，也不要改成第 2 個（T+2 更新與 22:00 daily 同時，不保證已更新）」；資料表段落補「上市當沖統計」。
- [ ] **Step 5:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 6: Commit** `feat(market_daily): daily 以往前第 3 個交易日重抓校正當沖統計`
