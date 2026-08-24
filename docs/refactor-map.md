# Refactor Map

`src/tw_stock_rawdata/` 的現況地圖。**純描述，不含修改建議**——用途是 refactor 前先知道
爆炸半徑在哪、哪些地方名實不符、哪些邏輯已經重複。

產出方式：pyright LSP（`documentSymbol` / `findReferences`）+ 文字搜尋交叉驗證，
第 4.5、6 節另以 radon 6.0.1 / vulture 2.16 覆核。
基準 commit：`7a3ed8b`。測試現況：368 passed，整體覆蓋率 53%。

---

## 1. 模組依賴圖

```
__main__.py ──> run.py ──┬──> config.py
                         ├──> db.py <──────── db_utils.py
                         ├──> db_utils.py
                         ├──> prepare.py ──> sources.py
                         ├──> price_limit.py
                         └──> sources.py
```

**沒有循環依賴。** 這是一張 DAG，最長路徑深度 3（`__main__` → `run` → `prepare` → `sources`）。

驗證方式：
- 所有 intra-package import 都在檔案頂層，沒有任何函式內的延遲 import 藏著回邊
  （`run.py:124/192/256` 的三個縮排 import 都是 stdlib `time`，非套件內模組）。
- `sources.py`、`config.py`、`price_limit.py` 是葉節點，不 import 套件內任何東西。
- `__init__.py` 是空檔（0 bytes），套件沒有定義任何 public API 表面，全部靠路徑 import。

| 模組 | 行數 | 依賴誰 | 被誰依賴 |
|---|---:|---|---|
| `__main__.py` | 4 | `run` | — |
| `run.py` | 2617 | `config`, `db`, `db_utils`, `prepare`, `price_limit`, `sources` | `__main__` |
| `sources.py` | 1540 | — | `run`, `prepare` |
| `prepare.py` | 955 | `sources` | `run` |
| `db_utils.py` | 612 | `db` | `run` |
| `db.py` | 203 | — | `run`, `db_utils` |
| `price_limit.py` | 52 | — | `run` |
| `config.py` | 20 | — | `run` |

拓樸是明確的 hub-and-spoke：`run.py` 是唯一的 hub，其餘模組彼此幾乎不認識
（唯二的橫向邊是 `prepare → sources` 和 `db_utils → db`）。

---

## 2. 各模組實際職責 vs 命名

### `config.py`（20 行）— 名實相符
`AppConfig.from_env()`，讀 `DATABASE_URL` / `USE_DB`。覆蓋率 90%。

### `price_limit.py`（52 行）— 名實相符
`tick_size()` + `calc_limits()`，純 Decimal 計算，無 I/O、無 pandas。覆蓋率 100%。

**但這個 domain 被切成兩半**：`run.py:1237-1268` 的 `_reference_price()` 與
`_price_limits()` 也是漲跌停邏輯——負責 pandas NaN 處理、由收盤價與漲跌價差推參考價、
以及「成交價落在區間外就寫 NULL」的判定。純數學在 `price_limit.py`，領域規則在 `run.py`。

### `db.py`（203 行）— 名實不符（範圍比名字窄很多）
名字像是「資料庫層」，實際只有連線池（`get_pool` / `close_pool`）、schema DDL
（`_SCHEMA_SQL` 佔 51-197 共 147 行，是本檔主體）、年度 partition（`ensure_partition`）。
可執行語句只有 30 條，覆蓋率 37%——`get_pool`、`close_pool`、`ensure_partition`、
`init_schema` 全部沒有測試覆蓋。

### `db_utils.py`（612 行）— 名實不符（最明顯的一個）
名字是 "utils"，實際是**完整的持久化層**：8 個查詢函式、3 個 upsert、3 個批次 update、
1 個資料修正邏輯（`correct_prev_margin_balance`）。228 條語句，是 `db.py` 的 7.6 倍。

真正的「資料庫模組」是這個，`db.py` 反而只是它的基礎設施。命名與體量倒置。

### `sources.py`（1540 行）— 名實不符（範圍比名字寬很多）
CLAUDE.md 說它負責「對外抓資料」。24 個 `fetch_*` 確實是抓取，但檔案裡另外還有
**四類非抓取的東西**：

| 類別 | 符號 | 行號 |
|---|---|---|
| HTTP 基礎設施 | `_MinIntervalAdapter`, `build_session` | 71-117 |
| Retry 基礎設施 | `_retry_backoff_delay`, `_retry_on_transient` | 135-192 |
| 文字解析原語 | `_parse_roc_date`, `_parse_date_any`, `_extract_first_date`, `_clean_number`, `_clean_int`, `_roc_to_date`, `_date_to_roc`, `_parse_roc_date_compact` | 195-292, 855-869 |
| DataFrame 重塑 | `find_twse_open_close`, `find_twse_ohlcv`, `expand_twse_stock_day`, `expand_tpex_stock_day` | 404-517 |

最後一類值得單獨標記：`expand_*` 與 `find_*` 不發任何 HTTP 請求，輸入 DataFrame、
輸出 DataFrame——這正是 CLAUDE.md 描述 `prepare.py` 的職責（「把各來源回傳的
DataFrame normalize 成標準欄位」）。同一種工作分佈在兩個模組。

第三類則造成一條跨模組的私有名稱依賴：`prepare.py:11-17` 從 `sources.py` import 了
四個底線開頭的私有函式（`_clean_int`, `_clean_number`, `_parse_roc_date`,
`_parse_roc_date_compact`）。這些是通用字串處理，與「資料來源」無關。

### `prepare.py`（955 行）— 名實大致相符
16 個 `prepare_*` normalize 函式 + 欄位比對工具（`_find_column` / `_find_columns` /
`_extract_standard_columns`）。職責清楚。

唯一的邊界模糊：`_cn_to_int`（中文數字轉阿拉伯數字，649-669）與
`_parse_disposition_period`（671-686）是處置股公告的文字解析，性質接近 `sources.py`
的解析原語群，而非 DataFrame normalize。

### `run.py`（2617 行）— 名實不符（god module）
CLAUDE.md 說它是「CLI 入口、每日抓取編排」。實際上除此之外還裝了：

- **CLI 層**：`_parse_args`, `_parse_date`, `_build_date_range`, `_is_daily_mode`, `_parse_trading_day`
- **4 個子命令**：`_update_shares_command`, `_dahu_command`, `_backfill_limits_command`, `_backfill_disposition_command`
- **抽象層**：`RowSourceProvider`(Protocol, 796) / `BatchSourceProvider`(819) / `PerSymbolRangeProvider`(896) — 全套件唯一存在的抽象層
- **領域邏輯**：`_reference_price`, `_price_limits`（漲跌停）、`DispositionData`(512) + `_disposition_windows` + `_fetch_disposition`（處置股）、`_expected_prev_trade_date`（交易日推算）
- **快取層**：`_prefetch_margin_cache`, `_prefetch_holding_pct_cache`, `_prefetch_symbol_ohlcv`, `twse_month_cache`
- **編排**：`_run_for_date`, `_main_inner`, `_fetch_ohlcv_with_fallback`, `_build_daily_rows`

最長的幾個函式：

| 函式 | 行數 | 起始行 |
|---|---:|---:|
| `_run_for_date` | 308 | 1765 |
| `_main_inner` | 212 | 2406 |
| `PerSymbolRangeProvider` | 172 | 896 |
| `_run_for_date_no_write` | 158 | 2226 |
| `_build_daily_rows` | 155 | 1068 |
| `_fetch_ohlcv_with_fallback` | 122 | 1270 |

錯誤處理風格：`run.py` 有 138 個 `print(`，錯誤路徑多半是 `print(...)` + `return`
而非丟例外，因此失敗會沉默地變成「這檔沒寫入」。

---

## 3. Fan-in 最高的三個模組

模組層級的 fan-in 因為 hub-and-spoke 拓樸而沒有鑑別度（幾乎都是 1-2）。以下用
**實際呼叫點數**（LSP `findReferences` 驗證 + 文字搜尋交叉比對）排序。

### 第 1 名：`sources.py` — 爆炸半徑最大

被 `run.py` 和 `prepare.py` 兩個模組同時依賴，且是唯一被跨模組引用私有符號的模組。

| 符號 | 呼叫/引用點 | 說明 |
|---|---:|---|
| `DataUnavailableError` | `raise` 52 (sources) + 11 (prepare)，`except` 4 (run) | 全套件的錯誤協定 |
| `_clean_int` | prepare.py 內 28 處 | 跨模組私有依賴 |
| `_clean_number` | prepare.py 內 17 處 | 跨模組私有依賴 |
| `_retry_on_transient` | 裝飾 20+ 個 fetcher | 改語意 = 改全部抓取行為 |

`DataUnavailableError` 的語意（什麼算「沒資料」vs「抓取失敗」）被 63 個 raise 點和
4 個 except 點共用，是整個 repo 耦合最緊的一個決定。

覆蓋率 52%，未覆蓋的 373 行集中在實際 HTTP 路徑。

### 第 2 名：`db.py` — 呼叫點密度最高

`get_pool` 經 LSP `findReferences` 確認共 20 references：`db_utils.py` 內 **16 個呼叫點**
（幾乎每個 public 函式開頭一次）+ `run.py:2397` 1 個 + 定義與 import。

改 `get_pool` 的簽名或 pool 語意，會同時觸及 `db_utils.py` 的 16 個函式。
而 `db.py` 本身覆蓋率只有 37%，這 16 個呼叫點沒有一個是在測試裡走到 `get_pool` 真身。

### 第 3 名：`db_utils.py`

只被 `run.py` import，但被 import 的符號有 17 個（`run.py:20-36`），是單一模組
export 面最寬的。`upsert_daily_raw` 的 COALESCE 語意（CLAUDE.md 列為不變量）
是下游所有資料正確性的單點。覆蓋率 54%。

---

## 4. 重複邏輯（含行號）

依「同一段邏輯重複次數」分組。次數對照 CLAUDE.md 的 `不新增抽象層，除非同一段邏輯已重複三次以上`。

### 4.1 完全相同的重複（複製貼上）

**`_parse_moneydj_date` — 逐字元相同，2 份**
- `prepare.py:758-764`（在 `prepare_moneydj_margin` 內）
- `prepare.py:802-808`（在 `prepare_moneydj_holding_pct` 內）

兩份 7 行本體完全一致，連下一行 `result["date"] = df["date"].map(_parse_moneydj_date)` 都一樣。

**`_is_valid_date_row` — 除註解外相同，2 份**
- `sources.py:1141-1146`（在 `fetch_moneydj_margin` 內，多一行 `# Valid ROC date format: 115/02/11` 註解）
- `sources.py:1277-1281`（在 `fetch_moneydj_holding_pct` 內）

同一條 regex `^\d{2,3}/\d{1,2}/\d{1,2}$` 寫了兩次。

### 4.2 已經產生分歧的重複（值得優先注意）

**`_get_int_col` — 2 份，其中一份多一道防護**
- `prepare.py:436-440`（`prepare_twse_margin` 內）：`if src_col:`
- `prepare.py:512-516`（`prepare_tpex_margin` 內）：`if src_col and src_col in df.columns:`

兩份其餘完全相同，接下來的 6 行賦值（`margin_buy` / `margin_sell` / `margin_balance` /
`short_sell` / `short_buy` / `short_balance`）也一字不差。差別只在後者多了
`and src_col in df.columns`。這是典型「複製後只補了一邊」的形狀——兩者之中必有一個
與作者意圖不符，但從程式碼本身看不出是哪一個。

**`stat` 檢查 — 3 種不同寬鬆度並存，13 處**

| 寫法 | 出現位置 |
|---|---|
| `!= "OK"`（最嚴） | `sources.py:392, 533, 900, 1331, 1361, 1386, 1403` |
| `not in {None, "OK"}` | `sources.py:591` |
| `not in {None, "ok", "OK"}`（最寬） | `sources.py:710, 731, 806, 1009, 1072` |

同一個概念（「這個 payload 算不算有效」）有三套判準。TWSE 端點多用嚴格版、
TPEX V2 端點多用寬鬆版，但 `sources.py:591`（MI_INDEX）落在中間，是唯一的
`{None, "OK"}`。

### 4.3 重複三次以上（達到 CLAUDE.md 的門檻）

**HTTP 呼叫前置樣板 — 23 次**

`urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)` 出現 23 次，
`verify=False` 出現 23 次，`raise_for_status()` 出現 24 次。幾乎每個 `fetch_*` 開頭
都有這三行。全 repo 停用 TLS 驗證的決定被複製了 23 份。

**TPEX V2 fetcher 本體 — 3 次，結構完全同構**
- `sources.py:697-716` `fetch_tpex_daily_quotes_v2`
- `sources.py:718-737` `fetch_tpex_3insti_v2`
- `sources.py:992-1015` `fetch_tpex_margin_v2`

三者的 10 行本體（`_date_to_roc` → params → disable_warnings → get → raise_for_status
→ json → stat 檢查 → `_parse_date_any` → `_extract_tpex_v2_table` → return）
逐行同構，只差 URL 常數、params 多出的鍵、錯誤訊息字串、表格關鍵字。

**`db_utils.py` 的連線樣板 — 16 次**

`pool = get_pool(database_url)` 16 次、`with pool.connection() as conn` 16 次、
`conn.cursor()` 9 次。每個 public 函式都自己開一次 pool。

### 4.4 結構同構但只重複兩次（未達門檻）

**`update_price_limits_batch` vs `update_disposition_batch`**
- `db_utils.py:332-371`
- `db_utils.py:373-414`

兩者的分塊 UPDATE ... FROM (VALUES ...) 骨架逐行同構，差別只有：SET 的欄位名、
`AS v(...)` 的欄位名、`%s::` 的型別轉換（`numeric,numeric` vs `boolean,smallint`）。
連 docstring 的結構、`if not updates: return 0`、`n_updated += cur.rowcount` 都相同。

**`fetch_twse_taiex_ohlc` vs `fetch_twse_market_volume`**
- `sources.py:1314-1345`
- `sources.py:1347-1371`

前 7 行（`replace(day=1)` → params → disable_warnings → get → raise_for_status → json
→ stat 檢查）完全相同，只差 URL 常數與錯誤訊息；後半的 `for row in payload["data"]:
d = _roc_to_date(row[0]); if d:` 迴圈骨架也相同，只差取哪幾個欄位。

**`expand_twse_stock_day`(441) vs `expand_tpex_stock_day`(479)** — 同構，差在成交量
單位（股 vs 張）。

**`_backfill_limits_command`(684) vs `_backfill_disposition_command`(729)** — 參數驗證
前置段同構（「印警告說某些參數被忽略」→「檢查 start/end 存在否則印錯誤 return」）。

**`prepare_twse_margin`(410) vs `prepare_tpex_margin`(478) vs `prepare_tpex_margin_v2`(560)**
— 三者都做「找欄位 → `_get_int_col` 取六個整數欄 → 算 margin_change / short_change →
算 ratio」，但欄位比對策略各自不同（`_find_columns` vs 手寫 `col_mapping` dict vs
前綴比對），所以是**概念重複但實作不同構**，不是複製貼上。

### 4.5 死碼

vulture 2.16 掃出 5 個零呼叫點的函式，逐一以全 repo 搜尋覆核（行號取 `def` 那行；
vulture 報的是 `@_retry_on_transient` 裝飾器行，故 `sources.py` 兩筆比本表少 1）——**每一個的參照數都恰好
是 1（自己的 def 那行）**，`src/` 與 `tests/` 都沒有呼叫、沒有 monkeypatch、沒有 export
（`__init__.py` 是空的）：

| 函式 | 位置 | 行數 | 備註 |
|---|---|---:|---|
| `_run_for_date_no_write` | `run.py:2226` | 158 | 與 `_run_for_date` 大幅重疊 |
| `fetch_tpex_daily_quotes` | `sources.py:614` | 34 | V1，已被 `_v2` 取代 |
| `fetch_tpex_3insti` | `sources.py:648` | 34 | V1，已被 `_v2` 取代 |
| `upsert_stocks` | `db_utils.py:60` | 25 | 唯一 public 名稱的死碼 |
| `find_twse_open_close` | `sources.py:404` | 16 | 旁邊的 `find_twse_ohlcv` 才是活的 |

合計約 267 行。

幾點觀察：

- **兩個 V1 TPEX fetcher 死了，但它們的 prepare 對應物還活著。**
  `prepare_tpex_quotes` 與 `prepare_tpex_3insti` 仍被 `run.py` 使用（前者另有 6 個測試），
  現在改由 `fetch_tpex_daily_quotes_v2` / `fetch_tpex_3insti_v2` 餵資料。也就是說遷移到 V2
  時換掉了抓取端、沿用了 normalize 端，V1 抓取端留在原地。
  注意 `fetch_tpex_margin`（953）**不是**死碼——它與 `fetch_tpex_margin_v2` 都還在用。
- **`upsert_stocks` 是這批裡唯一沒有底線前綴的**，形式上屬 public API。但本套件
  `__init__.py` 為空、唯一消費端是自己的 CLI，沒有外部呼叫者。
- `_run_for_date_no_write` 落在 `run.py` 覆蓋率報告的未覆蓋區間 `2243-2381` 內；
  `run.py:2345` 的註解自己寫著「與 `_run_for_date` 一致」。

**`run.py:124`, `run.py:192`, `run.py:256` 的 `import time`** — `time` 已在
`run.py:8` 頂層 import，這三個函式內 import 是多餘的重複。

vulture 在 `tests/` 另外報了約 50 筆，絕大多數是 `lambda *a, **k:` 形式的 stub 參數
與 `def fake_get(..., timeout=None, verify=None)` 的簽名對齊參數，屬誤報。

---

## 5. 無測試覆蓋的區域（改動前需確認）

對照 CLAUDE.md 的 `沒有測試覆蓋到的檔案，先跟我確認再改`：

| 模組 | 覆蓋率 | 未覆蓋的行 |
|---|---:|---|
| `__main__.py` | 0% | 1-4（全部） |
| `db.py` | 37% | 17-20, 26-28, 36-48, 200-203 |
| `prepare.py` | 52% | 163-252, 313-407, 486-635, 752-786 等 |
| `sources.py` | 52% | 524-757, 964-1176, 1325-1540 等 |
| `run.py` | 53% | 1890-2063, 2243-2381, 2551-2607 等 |
| `db_utils.py` | 54% | 93-198, 444-463, 487-516, 569-612 |
| `config.py` | 90% | 17 |
| `price_limit.py` | 100% | — |

值得注意的交集：本文件標出的重複邏輯，有相當比例落在未覆蓋區間內——
`prepare.py:512`(`_get_int_col` 分歧版) 在 486-635 內、
`db_utils.py:487-516`(`upsert_holder_percent`) 未覆蓋、
`sources.py` 三個 V2 fetcher 中的 992-1015 落在 964-1176 內、
`_run_for_date_no_write` 整段未覆蓋。

pyright 另外在未覆蓋區報了型別問題：`db.py:44-45`、`db_utils.py:51/262/321/367/410/462`、
`prepare.py:91/439/515/722`、`sources.py:462/503`。多數是 psycopg 3 的 `Query` 型別
與 pandas stub 的 overload 判定，非執行期錯誤。

---

## 6. 複雜度量測（radon）

### 6.1 檔案層級

| 模組 | LOC | SLOC | LLOC | 平均 CC | MI |
|---|---:|---:|---:|---|---|
| `run.py` | 2617 | 1826 | 1366 | B (7.77) | **C (0.00)** |
| `sources.py` | 1540 | 985 | 868 | B (5.64) | **C (5.12)** |
| `prepare.py` | 955 | 629 | 539 | B (6.24) | A (22.67) |
| `db_utils.py` | 612 | 375 | 257 | A (4.20) | A (46.04) |
| `db.py` | 203 | 171 | 39 | A (1.75) | A (60.38) |
| `price_limit.py` | 52 | 27 | 25 | A (2.50) | A (82.94) |
| `config.py` | 20 | 13 | 14 | A (1.50) | A (69.99) |

MI 只有 `run.py` 與 `sources.py` 掉到 C。`run.py` 的 **0.00 是量表下限**——不是「剛好很差」，
而是已經觸底、量表無法再區分。`prepare.py` 的 A (22.67) 也要小心讀：radon 的 A 涵蓋
20-100，22.67 幾乎貼著 A/B 分界。

`db.py` 的 LLOC 只有 39 而 SLOC 171，落差來自 `_SCHEMA_SQL` 那 147 行 DDL 字串——
它在量測上算不進邏輯行，所以 `db.py` 的各項指標偏樂觀。

### 6.2 函式層級（rank C 以上）

`run.py` 佔了 15 個中的全部 5 個 D/E/F：

| 函式 | 位置 | CC | rank |
|---|---|---:|---|
| `_run_for_date` | `run.py:1765` | **55** | **F** |
| `_run_for_date_no_write` | `run.py:2226` | 38 | E |
| `_fetch_ohlcv_with_fallback` | `run.py:1270` | 32 | E |
| `_main_inner` | `run.py:2406` | 32 | E |
| `_build_daily_rows` | `run.py:1068` | 31 | E |
| `_backfill_disposition_command` | `run.py:729` | 19 | C |
| `_fetch_disposition` | `run.py:560` | 18 | C |
| `_extract_twse_table` | `sources.py:296` | 17 | C |
| `_dahu_command` | `run.py:277` | 17 | C |
| `_fetch_and_upsert_market_daily` | `run.py:2166` | 16 | C |
| `_read_tpex_csv` | `sources.py:330` | 16 | C |
| `prepare_tpex_margin_v2` | `prepare.py:560` | 16 | C |
| `_get_margin_data` | `run.py:1415` | 15 | C |
| `fetch_moneydj_margin` | `sources.py:1081` | 15 | C |
| `prepare_tpex_margin` | `prepare.py:478` | 14 | C |

（其餘 C：`_backfill_limits_command` 13、`fetch_moneydj_holding_pct` 13、
`prepare_disposition` 13、`_prefetch_symbol_ohlcv` 12、`_refresh_prev_day_margin` 12、
`fetch_twse_stock_day_all` 12、`_resolve_dahu_dates` 11、`PerSymbolRangeProvider.build` 11、
`fetch_twse_mi_index` 11）

### 6.3 與前述發現的交叉點

三個獨立訊號指向同一批程式碼：

| 函式 | CC | 覆蓋率 | 其他 |
|---|---:|---|---|
| `_run_for_date_no_write` | 38 (E) | 未覆蓋 | 死碼 |
| `_run_for_date` | 55 (F) | 部分未覆蓋 | 全 repo 最高 CC |
| `prepare_tpex_margin` / `_v2` | 14 / 16 | 486-635 未覆蓋 | `_get_int_col` 分歧點在此 |
| `fetch_moneydj_margin` / `holding_pct` | 15 / 13 | 964-1176 未覆蓋 | `_is_valid_date_row` 重複在此 |

CC 55 的 `_run_for_date` 意味著要完整走過所有分支需要 55 條獨立路徑，而它目前是部分覆蓋。

### 6.4 放寬到 rank B 之後

`radon cc src/ -n B` 共 61 個函式（B 37、C 19、E 4、F 1）。分佈：

| 模組 | rank B 以上的函式數 |
|---|---:|
| `sources.py` | 22 |
| `run.py` | 22 |
| `prepare.py` | 12 |
| `db_utils.py` | 5 |
| `db.py` | **0** |
| `price_limit.py` | **0** |
| `config.py` | **0** |
| `__main__.py` | **0** |

**四個模組沒有任何函式達到 rank B**，全部是 A。這修正了第 5 節單看覆蓋率得到的印象：
`db.py` 覆蓋率只有 37%，但它的每個函式 CC 都在 A 級（平均 1.75），未覆蓋的
`get_pool` / `close_pool` / `ensure_partition` / `init_schema` 都是低分支的直線程式碼。
低覆蓋 + 低複雜度，與低覆蓋 + 高複雜度（如 `_run_for_date_no_write`：未覆蓋 + CC 38）
是兩種不同的風險。

B 級名單另外印證了幾組前面標出的重複：

- `expand_twse_stock_day`(441) 與 `expand_tpex_stock_day`(479) **CC 同為 6**——
  第 4.4 節說它們同構，複雜度相同是旁證。
- `fetch_tpex_daily_quotes`(614) 與 `fetch_tpex_3insti`(648) **CC 同為 6**，
  且兩者都是第 4.5 節的死碼。
- 三個平行的融資融券 normalize 複雜度遞增：`prepare_twse_margin` B(10) →
  `prepare_tpex_margin` C(14) → `prepare_tpex_margin_v2` C(16)。第 4.4 節說它們
  「概念重複但實作不同構」，複雜度階梯與此一致。

一個不對稱值得記下：第 3 節列為最高 fan-in 的兩個 helper，複雜度差很多——
`_clean_number`(`sources.py:251`) 是 **B (7)**，`_clean_int`(`sources.py:268`) 只有 **A (3)**。
兩者在 `prepare.py` 各被呼叫 17 / 28 次。高 fan-in 的那一組裡，風險集中在 `_clean_number`。

同樣屬「小函式但分支密」的還有 `db_utils.py:41` 的 `_safe`——12 行、CC 7，
四道連續 guard（`None` / `float` NaN-Inf / `pd.isna` / `hasattr(val, "item")`）。
pyright 對這個函式報的 `db_utils.py:51` 型別問題正落在最後那道 `hasattr` 分支上。
