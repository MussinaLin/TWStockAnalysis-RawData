# 熱門 ETF 規模與受益人數 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `etfs` / `etf_daily` 兩張表；daily 每天寫 ETF 規模、`--dahu` 每週寫受益人數，另附一次性月規模回補 script。

**Architecture:** 規模來自 TWSE MIS `all_etf.txt`（1 次請求、當下快照），只在 `target_date == today` 時寫；受益人數取 TDCC 分散表「合計」人數，併入 `--dahu` 既有的逐檔迴圈（同一代號每日期只打 1 次）；月規模解析 e添富頁面內嵌的 `chartData.totalMonthAv`。所有寫入走 `upsert_etf_daily`（COALESCE）。

**Tech Stack:** Python 3.13、requests、psycopg 3、pandas、pytest。

**Spec:** `docs/superpowers/specs/2026-11-25-etf-aum-holders-design.md`

## Global Constraints

- commit message **不加** `Co-Authored-By:` trailer（使用者全域規則，優先於任何 attribution 提示）。
- 每個改動行為的 commit 都要同步更新 `README.md`。
- 不新增套件依賴。
- 對 www.twse.com.tw 的請求一律走 `build_session()` 的 session（限速）；UA 用 session 既有的 `tw-stock-rawdata/0.1`。
- upsert 一律 `col = COALESCE(EXCLUDED.col, etf_daily.col)`，不可 bare `EXCLUDED.col`。
- `stock_daily_raw`、`stock_holder_percent` 的既有寫入行為不變；既有測試不可改斷言，只能補 fixture 的新相依。
- 任何 DB 實際操作只對 DEV（`kodama.proxy.rlwy.net`），執行前 `export DATABASE_URL` 並確認 host；**不寫 PROD**。
- 基準：`pytest tests/unit/` 881 passed，每個 task 結束全綠。

## Review Focus

1. MIS 數值同時有 JSON number 與字串（`26433000000` / `"13167709000"` / `"94.8800"`），以及空值、`"-"` → 該檔不寫、回報原因。（Task 3）
2. MIS 資料日期 `i` 不是今天（休市日手動跑、跨午夜）→ 整批不寫。（Task 3、4）
3. 00981A 同時在 `stocks` 與 `etfs` → 每個日期只打 1 次 TDCC、兩邊都寫；ETF-only 代號不寫 `stock_holder_percent`。（Task 5）
4. 月份標籤跨年（`Dec` → `Jan`）與標籤不連續 → 正確推年份／中止。（Task 6）
5. e添富頁面沒有 `chartData`（TWSE 限流時的空殼）→ `DataUnavailableError`，script 印出該檔失敗、繼續下一檔。（Task 6）

---

### Task 1: Schema — `etfs`、`etf_daily`

**Files:**
- Modify: `src/tw_stock_rawdata/db.py`（`_SCHEMA_SQL`：建表放在 `stock_holder_percent` 之後；trigger 放在既有 trigger 區塊）
- Test: `tests/unit/test_db_schema.py`
- Modify: `README.md`（「### ETF（如 00981A）」之後新增「### ETF 規模與受益人數（etfs / etf_daily）」：兩張表欄位說明、新增清單的 SQL）

**Interfaces:**
- Produces: 表 `etfs(symbol, name, enabled, created_time, updated_time)`、`etf_daily(symbol, trade_date, units, nav, aum_100m, holders, created_time, updated_time)`，DDL 照 spec「Schema」一節逐字；trigger `trg_etfs_updated`、`trg_etf_daily_updated`。

- [ ] **Step 1: 寫失敗測試**

```python
_ETF_DAILY_DDL = {
    "units": r"BIGINT", "nav": r"NUMERIC\(12,4\)",
    "aum_100m": r"NUMERIC\(12,2\)", "holders": r"INTEGER",
}

@pytest.mark.parametrize("col,typ", _ETF_DAILY_DDL.items())
def test_etf_daily_columns(col, typ):
    assert re.search(rf"^\s*{col}\s+{typ},", _create_table_body("etf_daily"), re.M)

def test_etf_daily_primary_key():
    assert "PRIMARY KEY (symbol, trade_date)" in _create_table_body("etf_daily")

def test_etfs_table():
    body = _create_table_body("etfs")
    assert re.search(r"^\s*symbol\s+VARCHAR\(10\)\s+PRIMARY KEY,", body, re.M)
    assert re.search(r"^\s*enabled\s+BOOLEAN\s+NOT NULL DEFAULT TRUE,", body, re.M)

@pytest.mark.parametrize("trg,table", [("trg_etfs_updated", "etfs"), ("trg_etf_daily_updated", "etf_daily")])
def test_etf_updated_time_triggers(trg, table):
    assert re.search(rf"tgname = '{trg}'.*?BEFORE UPDATE ON {table}", _SCHEMA_SQL, re.S)
```

- [ ] **Step 2:** `pytest tests/unit/test_db_schema.py -q` → Expected: 新測試 FAIL（找不到 CREATE TABLE）。
- [ ] **Step 3:** 在 `_SCHEMA_SQL` 加 DDL 與兩個 trigger（照 `trg_market_daily_updated` 的 `pg_trigger` 檢查寫法）。
- [ ] **Step 4:** `pytest tests/unit/ -q` → Expected: 全綠。
- [ ] **Step 5:** commit `feat(etf): 新增 etfs 與 etf_daily 表`（含 README）。

---

### Task 2: DB 存取 — `get_enabled_etfs`、`upsert_etf_daily`

**Files:**
- Modify: `src/tw_stock_rawdata/db_utils.py`（放在 `upsert_holder_percent` 之後）
- Create: `tests/unit/test_etf_daily_db.py`

**Interfaces:**
- Produces:
  - `get_enabled_etfs(database_url: str) -> list[tuple[str, str]]`：`SELECT symbol, name FROM etfs WHERE enabled = TRUE ORDER BY symbol`（用 `_fetch_all`）。
  - `upsert_etf_daily(database_url: str, rows: list[dict]) -> int`：每個 dict 鍵為 `symbol`、`trade_date`，以及 `units` / `nav` / `aum_100m` / `holders` 的任意子集（缺鍵 = None）；值過 `_safe`；`symbol` strip 後為空的列略過；`executemany` 單一 INSERT … ON CONFLICT (symbol, trade_date)，四個值欄都 COALESCE；commit；回傳實際送出的列數。

- [ ] **Step 1: 寫失敗測試**（`install_fake_pool(monkeypatch, db_utils, FakeCursor(...))`）

```python
def test_upsert_coalesces_every_value_column(monkeypatch):
    n, cur, conn = _run(monkeypatch, [{"symbol": "0050", "trade_date": D, "holders": 4038137}])
    sql, params = cur.executed_many[0]
    for col in ("units", "nav", "aum_100m", "holders"):
        assert f"{col} = COALESCE(EXCLUDED.{col}, etf_daily.{col})" in sql
    assert "= EXCLUDED." not in sql.replace("COALESCE(EXCLUDED.", "")
    assert params == [("0050", D, None, None, None, 4038137)]
    assert n == 1 and conn.committed

def test_upsert_skips_empty_and_blank_symbols(monkeypatch):
    assert _run(monkeypatch, [])[0] == 0
    n, cur, conn = _run(monkeypatch, [{"symbol": "  ", "trade_date": D}])
    assert n == 0 and cur.executed_many == [] and not conn.committed

def test_get_enabled_etfs(monkeypatch):
    cur = FakeCursor(fetchall_rows=[("0050", "元大台灣50")])
    install_fake_pool(monkeypatch, db_utils, cur)
    assert db_utils.get_enabled_etfs(URL) == [("0050", "元大台灣50")]
    assert "WHERE enabled = TRUE" in cur.executed[0][0]
```

- [ ] **Step 2:** `pytest tests/unit/test_etf_daily_db.py -q` → Expected: FAIL（AttributeError）。
- [ ] **Step 3:** 實作兩個函式。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5:** commit `feat(etf): etf_daily 讀寫函式`。

---

### Task 3: MIS 快照抓取與解析

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（常數 `TWSE_MIS_ALL_ETF_URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"`；函式放在 TAIFEX VIX 之後）
- Modify: `src/tw_stock_rawdata/prepare.py`
- Create: `tests/unit/test_etf_mis.py`

**Interfaces:**
- Produces:
  - `fetch_mis_etf_snapshot(session) -> list[dict]`：`_http_get(session, TWSE_MIS_ALL_ETF_URL, None).json()`，攤平 `payload["a1"][*]["msgArray"]`；payload 不是 dict、`a1` 不是 list、或攤平後 0 筆 → `DataUnavailableError`。
  - `prepare_mis_etf_rows(entries: list[dict], symbols: list[str], trade_date: dt.date) -> tuple[list[dict], dict[str, str]]`：回傳 (rows, problems)。row = `{"symbol", "trade_date", "units": int, "nav": Decimal(4 位), "aum_100m": Decimal(2 位)}`，`aum_100m = units × nav ÷ 100_000_000` 四捨五入到 0.01；數值用 `Decimal(str(v))` 解析。problems 的 key 是代號、value 是原因字串：MIS 沒有該代號 / `i` 不是 `trade_date`（訊息含 `i` 原值）/ `c` 或 `h` 不是正數。

- [ ] **Step 1: 寫失敗測試**

```python
ENTRIES = [
    {"a": "0050", "c": 26433000000, "h": "94.8800", "i": "20261125"},
    {"a": "00981A", "c": "13167709000", "h": "21.55", "i": "20261125"},
    {"a": "0056", "c": "-", "h": "48.68", "i": "20261125"},
    {"a": "00878", "c": "20205790000", "h": "30.19", "i": "20261124"},
]
D = dt.date(2026, 11, 25)

def test_number_and_string_values():
    rows, problems = prepare_mis_etf_rows(ENTRIES, ["0050", "00981A"], D)
    assert rows == [
        {"symbol": "0050", "trade_date": D, "units": 26433000000,
         "nav": Decimal("94.8800"), "aum_100m": Decimal("25079.63")},
        {"symbol": "00981A", "trade_date": D, "units": 13167709000,
         "nav": Decimal("21.5500"), "aum_100m": Decimal("2837.64")},
    ]
    assert problems == {}

def test_problems_reported_not_written():
    rows, problems = prepare_mis_etf_rows(ENTRIES, ["0056", "00878", "00919"], D)
    assert rows == []
    assert set(problems) == {"0056", "00878", "00919"}
    assert "20261124" in problems["00878"]

def test_fetch_flattens_msg_array():
    payload = {"a1": [{"msgArray": ENTRIES[:2]}, {"msgArray": ENTRIES[2:]}]}
    assert len(fetch_mis_etf_snapshot(FakeSession(payload=payload))) == 4

@pytest.mark.parametrize("payload", [[], {"a1": "x"}, {"a1": [{"msgArray": []}]}])
def test_fetch_bad_shape_raises(payload):
    with pytest.raises(DataUnavailableError):
        fetch_mis_etf_snapshot(FakeSession(payload=payload))
```

- [ ] **Step 2:** `pytest tests/unit/test_etf_mis.py -q` → FAIL（ImportError）。
- [ ] **Step 3:** 實作。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5:** commit `feat(etf): 解析 MIS all_etf.txt 的規模`。

---

### Task 4: daily 子步驟 `_update_etf_daily`

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（函式放在 `_settle_market_day_trade` 之後；呼叫點在 `_main_inner` 單日模式最後）
- Create: `tests/unit/test_update_etf_daily.py`
- Modify: `tests/unit/test_main_inner_dispatch.py`（`spies` fixture 加 `_update_etf_daily` 的 spy；新增 `TestEtfDaily`）
- Modify: `README.md`（新章節加「daily：MIS 欄位對照、口徑 = c × h、只在今天跑、失敗只印 log」）

**Interfaces:**
- Consumes: Task 2 `get_enabled_etfs`、`upsert_etf_daily`；Task 3 `fetch_mis_etf_snapshot`、`prepare_mis_etf_rows`。
- Produces: `_update_etf_daily(session, config, today: dt.date) -> None`。

行為：沒有啟用 ETF → 印略過、不打請求；problems 逐檔印 `⚠ ETF {symbol} 規模未寫入：{reason}`；rows 非空才 upsert；`except (DataUnavailableError, requests.RequestException)` 與 `except Exception`（DB）兩段，同 `_settle_market_day_trade`，皆只印 log。
呼叫點：`if target_date == today: _update_etf_daily(session, config, today)`，寫在 `_settle_market_day_trade` 那段之後，**不看 `written`**。

- [ ] **Step 1: 寫失敗測試**

```python
# test_update_etf_daily.py（monkeypatch run.get_enabled_etfs / fetch_mis_etf_snapshot / upsert_etf_daily）
def test_writes_rows_for_enabled_etfs(etf): ...          # upsert 收到 0050 一列、aum 25079.63
def test_no_enabled_etf_skips_request(etf): ...           # fetched == 0、upserts == []
def test_fetch_failure_only_logs(etf, capsys): ...        # fetch 丟 DataUnavailableError → 不 raise、無 upsert、輸出含「失敗」
def test_db_failure_only_logs(etf, capsys): ...           # upsert 丟 RuntimeError → 不 raise
def test_stale_date_not_written(etf, capsys): ...         # i=前一天 → upserts == []、輸出含 ⚠

# test_main_inner_dispatch.py
class TestEtfDaily:
    def test_daily_runs_etf_once(self, spies):
        run._main_inner(CONFIG, _args(), TODAY, TODAY); assert spies["etf_dates"] == [TODAY]
    def test_runs_even_if_nothing_written(self, spies, monkeypatch):
        monkeypatch.setattr(run, "_run_for_date", lambda *a, **k: False)
        run._main_inner(CONFIG, _args(), TODAY, TODAY); assert spies["etf_dates"] == [TODAY]
    def test_historical_date_skips(self, spies):
        run._main_inner(CONFIG, _args(date="2026-08-19"), TODAY, dt.date(2026, 8, 19)); assert spies["etf_dates"] == []
    def test_backfill_skips(self, spies):
        run._main_inner(CONFIG, _args(backfill_start="2026-08-17", backfill_end="2026-08-21"), TODAY, TODAY)
        assert spies["etf_dates"] == []
```

- [ ] **Step 2:** `pytest tests/unit/test_update_etf_daily.py tests/unit/test_main_inner_dispatch.py -q` → FAIL。
- [ ] **Step 3:** 實作函式與呼叫點。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠。
- [ ] **Step 5:** commit `feat(etf): daily 寫入 ETF 規模`（含 README）。

---

### Task 5: `--dahu` 寫 ETF 受益人數

**Files:**
- Modify: `src/tw_stock_rawdata/prepare.py`（`prepare_tdcc_holder_count`，放在 `prepare_tdcc_retail_ratio` 之後）
- Modify: `src/tw_stock_rawdata/run.py`（`_dahu_command`）
- Modify: `tests/unit/test_dahu.py` 或新增 `tests/unit/test_tdcc_holder_count.py`；`tests/unit/test_dahu_command.py`
- Modify: `README.md`（`--dahu` 說明加 ETF 受益人數與回補指令；新章節加受益人數來源）

**Interfaces:**
- Consumes: Task 2 `get_enabled_etfs`、`upsert_etf_daily`。
- Produces: `prepare_tdcc_holder_count(df: pd.DataFrame) -> int | None`：以 `_find_column(df, ["持股"])` 與 `_find_column(df, ["人數"])` 找欄；「持股」欄去掉所有空白（含全形 `　`）後等於 `合計` 的那列，人數經 `_clean_number` 轉 int；找不到欄、列或解析失敗 → None。

`_dahu_command` 規則（spec「--dahu」一節）：
- `etf_set` = `get_enabled_etfs` 的代號集合。
- 無 `--stocks`：stock 目標 = `get_enabled_stocks`（不變）；ETF 目標 = `etf_set`。兩者皆空才印既有錯誤訊息並 return。
- 有 `--stocks`：代號照舊解析；stock 目標 = 其中**不是**「在 `etf_set` 且不在 `load_stock_names` 結果」的代號；ETF 目標 = 其中在 `etf_set` 的代號。
- 抓取順序 = stock 目標，再接不在 stock 目標裡的 ETF 目標；每個代號每個日期只呼叫一次 `_fetch_tdcc_with_retry`。
- stock 目標的大戶／散戶解析與 `upsert_holder_percent` 呼叫完全照舊（每個日期都呼叫一次）。
- ETF 目標：`holders` 為 None → 印 `{symbol} 無法解析受益人數`；否則收集 `{"symbol", "trade_date": date, "holders"}`，該日期結束時非空才 `upsert_etf_daily`，並印 ETF 寫入筆數。

- [ ] **Step 1: 寫失敗測試**

```python
def test_holder_count_from_total_row():
    df = pd.DataFrame({"序": [15, 16, 17],
                       "持股/單位數分級": ["1,000,001以上", "差異數調整（說明4）", "合　計"],
                       "人數": [302.0, None, 4038137.0]})
    assert prepare_tdcc_holder_count(df) == 4038137

def test_holder_count_missing_total_row():
    assert prepare_tdcc_holder_count(pd.DataFrame({"持股/單位數分級": ["1"], "人數": [1.0]})) is None

# test_dahu_command.py：wiring fixture 加
#   monkeypatch.setattr(run, "get_enabled_etfs", lambda url: calls["etfs"])   # 預設 []
#   monkeypatch.setattr(run, "upsert_etf_daily", lambda url, rows: calls["etf_upserts"].append(list(rows)) or len(rows))
#   monkeypatch.setattr(run, "prepare_tdcc_holder_count", lambda d: 1000)
class TestEtfHolders:
    def test_etf_only_symbol_writes_etf_daily_only(self, wiring):   # etfs=[("0050","元大台灣50")]
        # fetches 含 ("0050", D1/D2) 各一次；holder_percent rows 不含 0050；etf_upserts 每日期一筆 holders=1000
    def test_symbol_in_both_fetched_once(self, wiring, monkeypatch):  # 2330 同時在 enabled stocks 與 etfs
        # fetches 中 2330 每日期恰好一次；兩邊都有寫
    def test_explicit_stocks_with_etf(self, wiring):                # --stocks 0050,2330
        # 0050 → 只 etf_daily；2330 → 只 holder_percent
    def test_holder_parse_failure_skips_etf(self, wiring, monkeypatch, capsys):  # holder_count → None
        # etf_upserts == []，輸出含「無法解析受益人數」
    def test_only_etfs_enabled_still_runs(self, wiring, monkeypatch):  # get_enabled_stocks → []、etfs 有值
        # 有 fetch、有 etf_upserts
```

- [ ] **Step 2:** `pytest tests/unit/test_dahu_command.py -q` 與 holder_count 測試 → 新測試 FAIL，既有測試仍 PASS。
- [ ] **Step 3:** 實作。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠（既有 `test_dahu*.py` 斷言未改）。
- [ ] **Step 5:** commit `feat(etf): --dahu 寫入 ETF 受益人數`（含 README）。

---

### Task 6: e添富月規模 — 解析與一次性回補 script

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（常數 `TWSE_ETF_INFO_URL = "https://www.twse.com.tw/zh/ETFortune/etfInfo/{symbol}"`；`fetch_etf_month_aum`）
- Modify: `src/tw_stock_rawdata/prepare.py`（`resolve_month_labels`）
- Create: `tests/unit/test_etf_month_aum.py`
- Create: `tools/backfill_etf_month_aum.py`
- Modify: `README.md`（新章節加「月規模一次性回補」：來源、只補 NULL、指令）

**Interfaces:**
- Consumes: Task 2 `get_enabled_etfs`、`upsert_etf_daily`。
- Produces:
  - `fetch_etf_month_aum(session, symbol: str) -> list[tuple[str, Decimal]]`：`_http_get` 取頁面 `.text`，regex `let chartData\s*=\s*(\{.*?\});`（`re.S`）→ `json.loads` → `totalMonthAv` 的 `(date, Decimal(str(count)))`，保留原順序；找不到、JSON 錯、`totalMonthAv` 不是 list → `DataUnavailableError`。
  - `resolve_month_labels(labels: list[str], today: dt.date) -> list[tuple[int, int]]`：英文三字月份（`Jan`…`Dec`）；最後一個標籤 = 從 `today` 所在月份往前找第一個名稱相符的月份；其餘逐一往前一個月，名稱不符 → `ValueError`。

script（不寫單元測試，同其他 tools；以 DEV 模擬驗證）：
- 參數只有 `--apply`；`load_dotenv(repo/.env)`、印 `目標 DB：host:port/db`（同 `tools/backfill_vix_history.py`）。
- `build_session()`；對每檔 `get_enabled_etfs`：`fetch_etf_month_aum` → `resolve_month_labels(labels, today)` → `count <= 0` 略過 → 用 psycopg 直連查 `market_daily` 各月最大 `trade_date`（沒有就印出略過）→ 查 `etf_daily` 既有 `aum_100m`，非 NULL 的略過 → 收集 `{"symbol", "trade_date", "aum_100m"}`。
- 某檔 `DataUnavailableError` / `requests.RequestException` / `ValueError` → 印該檔失敗、繼續下一檔，不重試。
- 印每檔將寫入筆數與月份範圍；無 `--apply` 只模擬；有則 `upsert_etf_daily`，`finally: close_pool()`。

- [ ] **Step 1: 寫失敗測試**

```python
def test_resolve_labels_ending_last_month():
    labels = ["Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct"]
    out = resolve_month_labels(labels, dt.date(2026, 11, 25))
    assert out[0] == (2025, 11) and out[2] == (2026, 1) and out[-1] == (2026, 10)

def test_resolve_labels_including_current_month():
    assert resolve_month_labels(["Oct", "Nov"], dt.date(2026, 11, 30)) == [(2026, 10), (2026, 11)]

def test_resolve_labels_not_consecutive_raises():
    with pytest.raises(ValueError):
        resolve_month_labels(["Aug", "Oct"], dt.date(2026, 11, 25))

def test_fetch_month_aum_parses_chart_data():
    html = '<script>let chartData = {"totalMonthAv":[{"count":9423.45,"date":"Nov"},{"count":24556.07,"date":"Oct"}],"close1":[]};</script>'
    assert fetch_etf_month_aum(FakeSession(text=html), "0050") == [
        ("Nov", Decimal("9423.45")), ("Oct", Decimal("24556.07"))]

def test_fetch_month_aum_without_chart_data_raises():
    with pytest.raises(DataUnavailableError):
        fetch_etf_month_aum(FakeSession(text="<html></html>"), "0050")
```

- [ ] **Step 2:** `pytest tests/unit/test_etf_month_aum.py -q` → FAIL。
- [ ] **Step 3:** 實作兩個函式與 script。
- [ ] **Step 4:** `pytest tests/unit/ -q` → 全綠；`python tools/backfill_etf_month_aum.py --help` 正常。
- [ ] **Step 5:** commit `feat(tools): 一次性回補 ETF 月規模`（含 README）。

---

### Task 7: CLAUDE.md 與 DEV 驗證

**Files:**
- Modify: `CLAUDE.md`（「資料表」加 `etfs` / `etf_daily` 一行；Commands 加月規模 script 與受益人數回補指令）

- [ ] **Step 1:** 更新 CLAUDE.md，`pytest tests/unit/ -q` → 全綠，commit `docs: CLAUDE.md 補 ETF 表與指令`。
- [ ] **Step 2（DEV）：** `export DATABASE_URL=<DEV>`、確認 host 含 `kodama`；建表：
  `python -c "import os; from tw_stock_rawdata.db import get_pool, init_schema, close_pool; init_schema(get_pool(os.environ['DATABASE_URL'])); close_pool()"`；
  再用 README 的 SQL 新增 6 筆 `etfs`。
  Expected: `etfs` 6 列、`etf_daily` 存在且為空。
- [ ] **Step 3（DEV）：** `tw-stock-rawdata --dahu --stocks 0050,0056,00878,00919,00981A,00403A --from 2025-11-28 --to <最近週五>`。
  Expected: 每個日期 ETF 寫入 6 筆（00403A 上市前的週為解析失敗或查無資料，屬預期）；0050 在 2026-11-20 的 `holders` = 4038137。
- [ ] **Step 4（DEV）：** `python tools/backfill_etf_month_aum.py` 模擬 → 檢查月份範圍 → 加 `--apply`。
  Expected: 0050 的 2026-10-30 列 `aum_100m` = e添富當時的 Oct 值。
- [ ] **Step 5（DEV，晚上 21:30 之後）：** 以 DEV 呼叫一次 `run._update_etf_daily(build_session(), AppConfig.from_env(), today)`。
  Expected: 6 檔都寫入、無 ⚠；記錄當時 MIS 的 `i`（spec 已知風險）——若 `i` 已不是當天，回報使用者再決定，不自行改設計。
