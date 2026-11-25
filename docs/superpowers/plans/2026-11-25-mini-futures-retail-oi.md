# 小台／微台 散戶多空比原始口數 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `market_daily` 新增小台（MTX）／微台（TMF）的全市場未平倉與三大法人多／空單未平倉 6 欄；daily 每天寫，另附一次性回補 script，補完與玩股網比對。

**Architecture:** 法人來自期交所 `futContractsDateDown`（與大台同一支，抽出共用的 `fetch_taifex_fut_institutional`），全市場未平倉來自 `futDataDown`（新 `fetch_taifex_fut_daily_report`，區間上限一個月）。兩個 prepare 函式各自轉成欄位 dict；`_fetch_and_upsert_market_daily` 在大台之後逐商品、逐來源各自 try/except 併入同一次 upsert。script 只 UPDATE 已存在的列。

**Tech Stack:** Python 3.13、requests、pandas、psycopg 3、pytest。

**Spec:** `docs/superpowers/specs/2026-11-25-mini-futures-retail-oi-design.md`

## Global Constraints

- commit message **不加** `Co-Authored-By:` trailer（使用者全域規則，優先於任何 attribution 提示）。
- 每個改動行為的 commit 同步更新 `README.md`。不新增套件依賴。
- 期交所請求一律用傳入的 session（`build_session()`，UA `tw-stock-rawdata/0.1`），POST、維持預設 TLS 驗證。
- upsert 一律 `COALESCE(EXCLUDED.col, market_daily.col)`；script 只 UPDATE、不 INSERT `market_daily`。
- 大台 `txf_*` 與 `market_daily` 其他欄的寫入結果不變；`--backfill-futures-oi` 行為不變；
  既有測試不可改斷言，只能補 fixture／patch 的新相依。
- 本 repo 不計算散戶多空比（只有驗證步驟在 scratch 裡算）。
- DB 實際操作只對 DEV（`kodama.proxy.rlwy.net`），執行前 `export DATABASE_URL` 並確認 host；**不寫 PROD**。
- 基準：`pytest tests/unit/` 940 passed，每個 task 結束全綠。

## Review Focus

1. `futDataDown` 休市回**只有表頭的 CSV**（不是 alert）→ prepare 回 `{}`；daily 印「沒有該日」log、不寫；script 該段算 0 天、不算失敗。（Task 2、3、5）
2. 盤後列與價差列的未沖銷契約數是 `-`、到期月份含 `/`；小台有週契約（`202611W4`）→ 只加總「一般」單式列（含週）。`futDataDown` 每列有尾逗號 → 讀檔要 `index_col=False`。（Task 2、3）
3. 法人缺一個身份別、或某身份別某欄空 → 該欄 None，不把缺的當 0 加。（Task 3）
4. 單一來源失敗（如微台行情重試用完）→ 其餘 5 欄、大台、VIX、當沖照寫，且印 log。（Task 4）
5. script 區間跨月：每段不超過一個**日曆月**；期交所有、DB 沒有的日期不寫（不 INSERT）並列出。（Task 5）

---

### Task 1: Schema 與寫入欄位

**Files:**
- Modify: `src/tw_stock_rawdata/db.py`（`market_daily` 的 CREATE TABLE 加 6 欄；`day_trade_*` 的 ALTER 之後加 6 行 ALTER 與註解）
- Modify: `src/tw_stock_rawdata/db_utils.py`（`_DAY_TRADE_COL_TYPES` 之後）
- Test: `tests/unit/test_db_schema.py`、`tests/unit/test_db_utils_queries.py`
- Modify: `README.md`（`### 台指期三大法人未平倉` 之後新增 `### 小台／微台未平倉（market_daily.mtx_* / tmf_*）`：6 欄表、散戶多空比公式註明「由下游算」；大台段落「只算大台」那行加一句指向新段落）

**Interfaces:**
- Produces: `db_utils._MINI_FUTURES_OI_COLS: list[str]`，順序固定為
  `["mtx_market_oi", "mtx_inst_long_oi", "mtx_inst_short_oi", "tmf_market_oi", "tmf_inst_long_oi", "tmf_inst_short_oi"]`
- Produces: `db_utils.update_market_mini_futures_oi_batch(database_url: str, updates: list[tuple[dt.date, dict]]) -> int`
  （`_update_market_columns_batch` 加 `{c: "bigint"}`，只 UPDATE）

- [ ] **Step 1: Write the failing tests**

```python
# test_db_schema.py（import 加 _MINI_FUTURES_OI_COLS）
@pytest.mark.parametrize("col", _MINI_FUTURES_OI_COLS)
def test_mini_futures_oi_column_in_create_table(col): ...      # re ^\s*{col}\s+BIGINT,
@pytest.mark.parametrize("col", _MINI_FUTURES_OI_COLS)
def test_mini_futures_oi_column_has_online_migration(col): ... # ALTER ... IF NOT EXISTS {col}\s+BIGINT;
def test_mini_futures_oi_cols_order():
    assert _MINI_FUTURES_OI_COLS == ["mtx_market_oi", "mtx_inst_long_oi", "mtx_inst_short_oi",
                                     "tmf_market_oi", "tmf_inst_long_oi", "tmf_inst_short_oi"]

# test_db_utils_queries.py（沿用該檔 upsert_market_daily / batch 測試的寫法）
def test_upsert_market_daily_writes_mini_futures_oi_with_coalesce():
    # data={"mtx_market_oi": 33055}；params["mtx_market_oi"] == 33055、params["tmf_market_oi"] is None
    # "mtx_market_oi = COALESCE(EXCLUDED.mtx_market_oi, market_daily.mtx_market_oi)" in sql
def test_update_market_mini_futures_oi_batch_is_update_only():
    # SQL 含 "UPDATE market_daily"、不含 "INSERT"；6 欄皆以 ::bigint cast；回傳 rowcount 合計
```

- [ ] **Step 2: Run** `pytest tests/unit/test_db_schema.py tests/unit/test_db_utils_queries.py -q` — Expected: FAIL（ImportError `_MINI_FUTURES_OI_COLS`）
- [ ] **Step 3: Implement** 6 欄 DDL（BIGINT）、`_MINI_FUTURES_OI_COLS`、`upsert_market_daily` 的 `cols` 加 `*_MINI_FUTURES_OI_COLS`、`update_market_mini_futures_oi_batch`；README 段落。
- [ ] **Step 4: Run** `pytest tests/unit/ -q` — Expected: 全綠
- [ ] **Step 5: Commit** `feat(market): market_daily 新增小台／微台未平倉 6 欄`

---

### Task 2: 期交所抓取

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（TAIFEX 區塊）
- Test: `tests/unit/test_taifex_futures_oi.py`（既有大台測試不改）、新 `tests/unit/test_taifex_mini_futures.py`
- Create: `tests/fixtures/taifex_futdatadown_MTX_20261124.csv`、`tests/fixtures/taifex_futdatadown_TMF_20261124.csv`
  （cp950 原檔，複製自 scratchpad `mtx_probe/mkt.csv`、`mtx_probe/tmf_mkt.csv`，2026-11-25 實抓）

**Interfaces:**
- Produces:
  - `TAIFEX_FUT_DAILY_REPORT_URL = "https://www.taifex.com.tw/cht/3/futDataDown"`
  - `class MiniFuture(NamedTuple): prefix: str; label: str; inst_id: str; report_id: str; product: str`
  - `MINI_FUTURES: tuple[MiniFuture, ...] = (MiniFuture("mtx", "小台", "MXF", "MTX", "小型臺指期貨"), MiniFuture("tmf", "微台", "TMF", "TMF", "微型臺指期貨"))`
  - `fetch_taifex_fut_institutional(session, start: dt.date, end: dt.date, commodity_id: str) -> pd.DataFrame`（`@_retry_on_transient`）
  - `fetch_taifex_fut_daily_report(session, commodity_id: str, start: dt.date, end: dt.date) -> pd.DataFrame`（`@_retry_on_transient`）
- 不變：`fetch_taifex_txf_institutional(session, start, end)` 簽名、行為、錯誤訊息；改為**不加 decorator** 的薄包裝，
  `return fetch_taifex_fut_institutional(session, start, end, "TXF")`（避免重試巢狀 6×6）。

- [ ] **Step 1: Write the failing tests**（`test_taifex_mini_futures.py`，沿用 `test_taifex_futures_oi.py` 的 `_PostSession`／`_alert_page` 寫法）

```python
def test_institutional_posts_given_commodity():
    # fetch_taifex_fut_institutional(s, D, D, "MXF") → calls[0] == (futContractsDateDown URL,
    #   {"queryStartDate": "2026/11/24", "queryEndDate": "2026/11/24", "commodityId": "MXF"})
def test_report_posts_form():
    # fetch_taifex_fut_daily_report(s, "MTX", D, D) → data == {"down_type": "1", "commodity_id": "MTX",
    #   "commodity_id2": "", "queryStartDate": "2026/11/24", "queryEndDate": "2026/11/24"}
def test_report_columns_not_shifted_by_trailing_comma():
    # 每列比表頭多一個尾逗號：沒設 index_col=False 時 pandas 把第一欄當 index、整列錯位（實測）。
    # fixture MTX → len(df) == 35；df["交易日期"].iloc[0] == "2026/11/24"；
    # df["到期月份(週別)"].iloc[0] == "202611W4"；df["未沖銷契約數"].iloc[0] == "865"；df["交易時段"].iloc[1] == "盤後"
def test_report_header_only_returns_empty_frame():
    # 只有表頭的 bytes → df.empty 且 "未沖銷契約數" in df.columns（不 raise）
def test_report_range_alert_explains_one_month_limit():
    # alert("日期時間錯誤") → DataUnavailableError，訊息含 "一個月"
def test_report_no_data_alert_raises():  # alert("查無資料") → DataUnavailableError
def test_report_retries_transient_connection_error(monkeypatch):
    # 第 1 次 post 丟 requests.ConnectionError、第 2 次回 fixture；sources.time.sleep patch 掉；共 2 次 post
def test_mini_futures_table():
    assert [(f.prefix, f.inst_id, f.report_id) for f in MINI_FUTURES] == [("mtx", "MXF", "MTX"), ("tmf", "TMF", "TMF")]
```

- [ ] **Step 2: Run** `pytest tests/unit/test_taifex_mini_futures.py -q` — Expected: FAIL（ImportError）
- [ ] **Step 3: Implement.** 把現有大台函式的 HTML alert 判斷抽成共用 helper（兩支邏輯相同，依 CLAUDE.md 重用），
  由呼叫端傳入「日期時間錯誤」時的說明：法人沿用現有「該端點只提供近三年資料，起日 {start} 可能太舊」，
  行情用「該端點一次最多查一個月（{start} ~ {end}）」。法人照舊 `pd.read_csv(io.StringIO(content.decode("cp950")), dtype=str)`；
  行情**必須**加 `index_col=False`（每列有尾逗號，否則整列錯位）。
- [ ] **Step 4: Run** `pytest tests/unit/ -q` — Expected: 全綠（`test_taifex_futures_oi.py` 不改即通過）
- [ ] **Step 5: Commit** `feat(taifex): 抓小台／微台三大法人與全市場未平倉`

---

### Task 3: 解析

**Files:**
- Modify: `src/tw_stock_rawdata/prepare.py`（`prepare_taifex_txf_oi` 之後）
- Test: `tests/unit/test_taifex_mini_futures.py`

**Interfaces:**
- Consumes: Task 2 的 DataFrame（原始字串表）與 `MiniFuture` 欄位值。
- Produces:
  - `prepare_taifex_inst_long_short(df, product: str, prefix: str) -> dict[dt.date, dict[str, int | None]]`
    → `{date: {f"{prefix}_inst_long_oi": …, f"{prefix}_inst_short_oi": …}}`
  - `prepare_taifex_market_oi(df, commodity_id: str, prefix: str) -> dict[dt.date, dict[str, int]]`
    → `{date: {f"{prefix}_market_oi": …}}`
- 規則（spec）：都按表頭名稱取欄；缺欄、日期無法解析 → `DataUnavailableError`。
  法人：必要欄 `日期、商品名稱、身份別、多方未平倉口數、空方未平倉口數`；找不到 `product` 的列 → `DataUnavailableError`；
  身份別不在 `_TAIFEX_IDENTITIES` → `DataUnavailableError`；每欄三個身份別都有值才加總，否則 None。
  行情：必要欄 `交易日期、契約、到期月份(週別)、未沖銷契約數、交易時段`；只取 `契約.strip() == commodity_id`、
  `交易時段.strip() == "一般"`、`"/" not in 到期月份`、`_clean_int(未沖銷契約數) is not None` 的列加總；
  沒有任何有效列的日期不出現；空表 → `{}`。

- [ ] **Step 1: Write the failing tests**

```python
# 法人：2026-11-24 實際回應三列（小型臺指期貨：自營商 7578/5227、投信 90/168、外資及陸資 5010/3457）
def test_inst_sums_three_identities():
    assert prepare_taifex_inst_long_short(df, "小型臺指期貨", "mtx")[D] == {"mtx_inst_long_oi": 12678, "mtx_inst_short_oi": 8852}
def test_inst_tmf():  # 微型臺指期貨 2099/34153、0/0、12669/7476 → {"tmf_inst_long_oi": 14768, "tmf_inst_short_oi": 41629}
def test_inst_missing_identity_gives_none():   # 去掉投信列 → 兩欄皆 None
def test_inst_blank_value_gives_none_for_that_column_only():  # 投信多方未平倉口數 "" → long None、short 8852
def test_inst_other_products_ignored():        # 混入「臺股期貨」三列 → 結果同 test_inst_sums_three_identities
def test_inst_no_product_rows_raises():        # 只有臺股期貨 → DataUnavailableError
def test_inst_unknown_identity_raises():
def test_inst_missing_header_raises():         # 刪「空方未平倉口數」欄

# 行情：用 Task 2 的 fixture
def test_market_oi_mtx_fixture():  assert prepare_taifex_market_oi(mtx_df, "MTX", "mtx") == {D: {"mtx_market_oi": 33055}}
def test_market_oi_tmf_fixture():  assert prepare_taifex_market_oi(tmf_df, "TMF", "tmf") == {D: {"tmf_market_oi": 75681}}
def test_market_oi_includes_weekly_contracts():   # 只留 202611W4 一般列（865）→ 865
def test_market_oi_excludes_after_hours_and_spreads():  # 只留盤後列與價差列、並把它們的未沖銷契約數改成數字 → {}（證明是依時段與「/」排除，不是靠 "-"）
def test_market_oi_other_commodity_ignored():     # commodity_id="TMF" 套在 MTX fixture → {}
def test_market_oi_header_only_is_empty():        # → {}
def test_market_oi_multiple_dates_keyed_separately():
def test_market_oi_missing_header_raises():
def test_market_oi_unparseable_date_raises():
```

- [ ] **Step 2: Run** `pytest tests/unit/test_taifex_mini_futures.py -q` — Expected: FAIL（ImportError）
- [ ] **Step 3: Implement** 兩個函式（與 `prepare_taifex_txf_oi` 結構相似但語意不同，不合併）。
- [ ] **Step 4: Run** `pytest tests/unit/ -q` — Expected: 全綠
- [ ] **Step 5: Commit** `feat(prepare): 解析小台／微台法人多空單與全市場未平倉`

---

### Task 4: daily 寫入

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（imports；`_fetch_and_upsert_market_daily` 大台區塊之後、VIX 之前；docstring）
- Test: `tests/unit/test_market_daily.py`
- Modify: `README.md`（新段落加 daily 說明：每日 4 次期交所請求、各自獨立失敗、一般回補同樣會抓）

**Interfaces:**
- Consumes: `MINI_FUTURES`、`fetch_taifex_fut_institutional`、`fetch_taifex_fut_daily_report`（Task 2）；兩個 prepare（Task 3）。
- 行為：`for fut in MINI_FUTURES:` 兩個獨立 try：
  法人 `prepare_taifex_inst_long_short(fetch_taifex_fut_institutional(session, date, date, fut.inst_id), fut.product, fut.prefix)`；
  行情 `prepare_taifex_market_oi(fetch_taifex_fut_daily_report(session, fut.report_id, date, date), fut.report_id, fut.prefix)`。
  `date in map` → `market_data.update(...)`；不在 → 印 `f"  {fut.label}…取得失敗：期交所沒有 {date}"`；
  `(DataUnavailableError, requests.RequestException)` → 印 `f"  {fut.label}三大法人未平倉取得失敗：{exc}"` / `f"  {fut.label}全市場未平倉取得失敗：{exc}"`。

- [ ] **Step 1: Write the failing tests**

`wiring` fixture 補兩個 patch（新相依，不改既有斷言）：`fetch_taifex_fut_institutional` 依 `commodity_id` 回對應的法人表、
`fetch_taifex_fut_daily_report` 依 `commodity_id` 回只含一般單式列的小表（日期 `2026/08/21`），並把呼叫記到 `calls["mini_inst"]`、`calls["mini_report"]`。
`test_all_sources_fail_writes_nothing` 的失敗清單補上這兩個名稱（新相依；斷言 `upserts == []` 不變）。

```python
def test_mini_futures_oi_merged_into_same_upsert(wiring):
    # _written 含 6 欄且值等於 fixture 合計；txf_foreign_oi_net 仍在
def test_mini_futures_query_target_date_and_commodity_ids(wiring):
    assert wiring["mini_inst"] == [("MXF", DATE, DATE), ("TMF", DATE, DATE)]
    assert wiring["mini_report"] == [("MTX", DATE, DATE), ("TMF", DATE, DATE)]
def test_mini_report_failure_keeps_other_columns(monkeypatch, capsys, wiring):
    # TMF 行情丟 RequestException → 無 tmf_market_oi；其餘 5 欄、txf_*、taiex_close 仍在；log 含「微台全市場未平倉取得失敗」
def test_mini_inst_failure_keeps_other_columns(monkeypatch, capsys, wiring):
    # MXF 法人丟 DataUnavailableError("查無資料") → 無 mtx_inst_*；mtx_market_oi 仍在；log 含「小台三大法人未平倉取得失敗」
def test_mini_report_without_target_date_is_logged(monkeypatch, capsys, wiring):
    # 行情回只有表頭 → 無 *_market_oi；log 含「沒有 2026-08-21」
def test_mini_format_change_does_not_block_others(monkeypatch, wiring):
    # 法人表「身份別」改名 → 無 *_inst_*；taiex_close 仍在
```

- [ ] **Step 2: Run** `pytest tests/unit/test_market_daily.py -q` — Expected: 新測試 FAIL（`AttributeError: run 沒有 fetch_taifex_fut_institutional`）
- [ ] **Step 3: Implement** 如 Interfaces；README daily 說明。
- [ ] **Step 4: Run** `pytest tests/unit/ -q` — Expected: 全綠
- [ ] **Step 5: Commit** `feat(market): daily 寫入小台／微台未平倉`

---

### Task 5: 一次性回補 script

**Files:**
- Create: `tools/backfill_mini_futures_oi.py`
- Test: 新 `tests/unit/test_backfill_mini_futures_oi.py`（以 `importlib.util.spec_from_file_location` 載入 script 模組，只測純函式）
- Modify: `README.md`（新段落加 `#### 一次性回補（tools/backfill_mini_futures_oi.py）`：指令、範圍、只 UPDATE、請求數）

**Interfaces:**
- Consumes: `MINI_FUTURES`、兩支 fetch、兩個 prepare、`load_market_dates_in_range`、`update_market_mini_futures_oi_batch`、`close_pool`。
- Produces（script 內純函式，供測試）：
  - `month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]`：按日曆月切，首尾截到 `[start, end]`
  - `floor_for(prefix: str, today: dt.date) -> dt.date`：`"tmf"` → `dt.date(2024, 7, 29)`；`"mtx"` → today 往前 3 年（2/29 → 2/28）
  - `merge_updates(db_dates: list[dt.date], *maps: dict[dt.date, dict]) -> tuple[list[tuple[dt.date, dict]], list[dt.date]]`
    → (只含 `db_dates` 內日期的 updates（同日多個 map 合併）, 期交所有、DB 沒有的日期)
- CLI：`--apply`（預設模擬）、`--start YYYY-MM-DD`（選用；起日 = max(floor, --start, DB 最早日)）。
  其餘慣例同 `tools/backfill_etf_month_aum.py`：印目標 host、`DATABASE_URL` 優先環境變數、`finally: close_pool()`。
- 流程：每個商品：法人 1 次整段（失敗 → 印錯誤、該商品整個跳過）；行情逐 `month_chunks` 段（某段失敗 → 印該段、繼續）；
  `merge_updates` → 印將寫入天數、DB 有但法人沒有的日期、期交所有但 DB 沒有的日期、失敗段；`--apply` 才
  `update_market_mini_futures_oi_batch`。

- [ ] **Step 1: Write the failing tests**

```python
def test_month_chunks_calendar_months():
    assert month_chunks(date(2026, 9, 15), date(2026, 11, 24)) == [
        (date(2026, 9, 15), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31)), (date(2026, 11, 1), date(2026, 11, 24))]
def test_month_chunks_single_day():       assert month_chunks(d, d) == [(d, d)]
def test_month_chunks_crosses_year():     # 2025-12-20 ~ 2026-01-05 → 兩段
def test_floor_for_tmf_is_listing_date(): assert floor_for("tmf", date(2026, 11, 25)) == date(2024, 7, 29)
def test_floor_for_mtx_is_three_years():  assert floor_for("mtx", date(2026, 11, 25)) == date(2023, 11, 25)
def test_floor_for_mtx_leap_day():        assert floor_for("mtx", date(2028, 2, 29)) == date(2025, 2, 28)
def test_merge_updates_only_db_dates_and_reports_extra():
    # db=[d1, d2]；inst={d1: {...}, d3: {...}}；mkt={d1: {...}, d2: {...}}
    # → updates == [(d1, inst+mkt 合併), (d2, mkt)]；extra == [d3]
```

- [ ] **Step 2: Run** `pytest tests/unit/test_backfill_mini_futures_oi.py -q` — Expected: FAIL（檔案不存在）
- [ ] **Step 3: Implement** script 與 README。
- [ ] **Step 4: Run** `pytest tests/unit/ -q` 與 `python tools/backfill_mini_futures_oi.py --help` — Expected: 全綠；help 列出 `--apply`、`--start`
- [ ] **Step 5: Commit** `feat(tools): 一次性回補小台／微台未平倉`

---

### Task 6: 文件、DEV 驗證、玩股網比對

**Files:**
- Modify: `CLAUDE.md`（Commands 加 `python tools/backfill_mini_futures_oi.py [--start YYYY-MM-DD] [--apply]`；資料表 `market_daily` 那行補「小台／微台全市場與三大法人多空單未平倉 `mtx_*`／`tmf_*`」；
  Gotchas 加一條：`futDataDown` 區間上限一個月、休市回只有表頭不是 alert；小台法人代碼是 `MXF`、行情是 `MTX`；script 只 UPDATE 不 INSERT）

- [ ] **Step 1:** 改 CLAUDE.md，`pytest tests/unit/ -q` 全綠，commit `docs: CLAUDE.md 補小台／微台未平倉`。
- [ ] **Step 2: DEV daily 路徑。** `export DATABASE_URL=<DEV>`、確認 host 是 `kodama.proxy.rlwy.net`，然後
  `python -c` 呼叫 `run._fetch_and_upsert_market_daily(build_session(), date(2026, 11, 24), AppConfig.from_env())`（結束 `close_pool()`）。
  Expected：log 無小台／微台失敗；DEV 該日 `mtx_market_oi=33055, mtx_inst_long_oi=12678, mtx_inst_short_oi=8852,
  tmf_market_oi=75681, tmf_inst_long_oi=14768, tmf_inst_short_oi=41629`；其他欄與執行前相同。
- [ ] **Step 3: DEV 回補。** 同一個 export 下 `python tools/backfill_mini_futures_oi.py`（模擬）→ 讀輸出（範圍、天數、對不上日期、失敗段）
  → `--apply`。Expected：小台約 2023-11-27 起、微台 2024-07-29 起；失敗段 0；DEV 查 6 欄非 NULL 天數與模擬一致。
- [ ] **Step 4: 玩股網比對。** 用瀏覽器（playwright）開 `https://www.wantgoo.com/futures/retail-indicator/wmt&` 與 `…/wtm&`，
  讀頁面上的日期與散戶多空比；只讀頁面已顯示的資料，不大量翻頁。抽樣：最近 5 個交易日＋頁面能看到的較早日期
  （盡量含小台週契約存在的日子與微台 2024-08 前後）。從 DEV 以 `(inst_short − inst_long) / market_oi × 100` 算，
  四捨五入到 0.01 比對。Expected：全部一致。**不一致 → 列出日期、兩邊數值、差距，回報使用者，不改定義。**
  比對結果寫進 ledger。
- [ ] **Step 5:** 不 commit 任何 DB／比對產物；PROD 不動（等使用者核准）。
