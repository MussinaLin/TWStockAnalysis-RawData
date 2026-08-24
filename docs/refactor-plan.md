# Refactor Plan

依 `docs/refactor-map.md` + radon 6.0.1 / vulture 2.16 / pytest-cov 7.1.0 四份報告交叉驗證後
排出的優先級清單。基準：commit `7a3ed8b`，368 passed，整體覆蓋率 53%。

排序規則（依要求）：**高複雜度 + 高覆蓋率 + 低爆炸半徑優先**。覆蓋率低者另置第 4 節。
每項的執行前提是 CLAUDE.md 的 Refactor rules：行為不變、一次一種 transformation、一個 commit、
每次跑 pytest 全綠。

---

## 1. 交叉驗證

### 1.1 map 的判斷，數據支持的部分

| map 的說法 | 支持的數據 | 強度 |
|---|---|---|
| `run.py` 是 god module | MI **0.00**（量表下限）、22 個 rank B+、CC 最高 55、573 行未覆蓋 | 強 |
| `sources.py` 職責過寬 | MI **5.12**（C）、22 個 rank B+、373 行未覆蓋 | 強 |
| 5 個死碼 | vulture 5 筆 + 覆蓋率獨立佐證：五者皆 1–12%，只有 `def` 那行在 import 時執行 | 強 |
| TPEX V2 fetcher 三重複 | 三者覆蓋率 8% / 5% / 42%，都幾乎沒被走過 | 中 |
| `_get_int_col` 兩份已分歧 | 覆蓋率 89% vs **2%**；但追查後兩種寫法行為等價（見 D1），map 說「看不出哪邊對」已可判定 | 中（結論修正） |
| `db.py` 低覆蓋但低風險 | 該檔 **0 個 rank B+**，平均 CC 1.75，未覆蓋的是直線程式碼 | 強 |

死碼那項值得多說一句：vulture 是靜態分析、coverage 是動態執行，兩個獨立方法指向同一批
函式，這比任何單一工具的結論可信。

### 1.2 數據顯示有問題、但 map 沒提到的

這是本次交叉驗證最大的收穫——**map 完全沒有函式層級的「複雜度 × 覆蓋率」交叉**，
所以漏掉了一整類目標：

| 函式 | CC | 覆蓋率 | fan-in | map 為何漏掉 |
|---|---:|---:|---:|---|
| `_dahu_command` (`run.py:277`) | 17 (C) | **2%** | 1 | map 只把它列為「4 個子命令」之一，沒看覆蓋率 |
| `_fetch_and_upsert_market_daily` (`run.py:2166`) | 16 (C) | **2%** | 1 | **map 從頭到尾沒提過這個函式** |
| `_get_margin_data` (`run.py:1415`) | 15 (C) | 21% | 1 | 同上，沒提過 |
| `correct_prev_margin_balance` (`db_utils.py:542`) | 8 (B) | **4%** | 2 | map 只用「1 個資料修正邏輯」一語帶過 |
| `upsert_holder_percent` (`db_utils.py:471`) | 7 (B) | **5%** | 2 | 沒提過 |
| `_main_inner` (`run.py:2406`) | 32 (E) | 41% | 2 | map 只把它列進「最長函式表」，沒當成目標 |
| `fetch_moneydj_margin` (`sources.py:1081`) | 15 (C) | **22%** | 5 | map 只提了它內部的 `_is_valid_date_row` 重複 |

`_fetch_and_upsert_market_daily` 特別值得注意：CC 16、58 行、只有 1 行被覆蓋，
且是唯一寫 `market_daily` 的路徑（CLAUDE.md 列為共用大盤表）。

### 1.3 兩邊不一致的地方

**（a）map 的優先級隱含結論是錯的。**
map 第 6.2 節把 `_run_for_date`（CC 55, rank F）列為頭號問題，讀起來像是「先動它」。
但加入覆蓋率後，`_run_for_date` 只有 **21%**——依你的排序規則它應該進「需先補測試」區，
不能先動。真正符合「高複雜度 + 高覆蓋率 + 低爆炸半徑」的第一名是
**`_fetch_ohlcv_with_fallback`：CC 32 (E)、覆蓋率 97%、內部只有 1 個呼叫點**。
map 裡它只出現在長度表和 CC 表，從未被指認為優先目標。

**（b）`sources.py` 的「爆炸半徑最大」需要修正措辭。**
map 說它 fan-in 最高（`DataUnavailableError` 63 個 raise 點）。耦合的事實成立，
但覆蓋率顯示這些 raise 點多半落在未覆蓋區間（`sources.py` 373 行未覆蓋，集中在
524-757、964-1176、1325-1540）。所以正確的說法不是「動它會炸到很多有測試保護的地方」，
而是**「動它會炸到很多沒有測試保護的地方」**——風險更高，不是更低。

**（c）`prepare.py` 的檔案級指標掩蓋了雙峰分佈。**
map 引 MI A (22.67) 說它「貼著 A/B 分界」。函式級數據顯示真相是兩極：
`prepare_disposition` 100%、`prepare_twse_margin` 89%，對上
`prepare_tpex_margin` **2%**、`prepare_tpex_margin_v2` **3%**、`prepare_moneydj_margin` **5%**。
檔案平均 52% 不代表任何一個函式的實際狀態。

**（d）map 說 `db.py` 覆蓋率 37% 需要「先確認再改」，這條可以放寬。**
map 第 5 節依 CLAUDE.md 規則把 `db.py` 列入需確認清單，但第 6.4 節的 radon 數據
（0 個 rank B+、平均 CC 1.75）顯示它是低分支直線程式碼。規則照舊要問你，
但實際風險低於同樣覆蓋率的 `run.py` 未覆蓋區。

---

## 2. 優先執行區（覆蓋率足夠，可直接動）

> **P1–P7 已於 2026-08-24 全部完成**（commit 637feb6 … aadfa3c，另 3a58c29 為 P1 的
> 連鎖孤兒收尾）。結果：rank D 以上函式 5 → 2（剩下的 `_run_for_date` 55/F 與
> `_main_inner` 32/E 都在第 4 節，未動）；`sources.py` MI 由 C (5.12) 升為 B (9.94)；
> vulture 全 src 掃描零回報；測試 368 → 366（新增 7、刪除 9 個測已無人呼叫的死碼）。
> 逐項實測數字見各項下方的「實際結果」。

### [x] P1 — 刪除 5 個死碼

- **檔案／函式**：`run.py:2226 _run_for_date_no_write`(158行)、
  `sources.py:614 fetch_tpex_daily_quotes`(34)、`sources.py:648 fetch_tpex_3insti`(34)、
  `db_utils.py:60 upsert_stocks`(25)、`sources.py:404 find_twse_open_close`(16)
- **transformation**：刪死碼
- **覆蓋率**：1–12%（只有 `def` 行在 import 時執行）
- **爆炸半徑**：**0**。vulture 靜態掃描 + 全 repo grep 雙重確認，每個名稱的參照數恰好是 1（自己的 def）
- **預估收益**：`src/` 減少約 267 行（-4.4%）；`run.py` 移除一個 CC 38 的 rank E 函式；
  `sources.py` 移除 3 個函式；vulture 報告歸零
- **排序說明**：這項不符合「高覆蓋率優先」——死碼覆蓋率必然接近 0。但覆蓋率的作用是
  「證明改動沒破壞行為」，而 fan-in = 0 時沒有行為可破壞，所以覆蓋率在此不適用。
  收益／風險比最高，放第一
- **建議拆法**：5 個獨立 commit，或至少 `run.py` / `sources.py` / `db_utils.py` 三個
- **注意**：`upsert_stocks` 是唯一沒有底線前綴的，形式上算 public API。本套件 `__init__.py`
  為空、唯一消費端是自己的 CLI，但刪除前值得你確認沒有外部腳本直接 import 它

**實際結果**：刪除 5 個死碼共 267 行。連鎖效應超出原估——刪掉兩個 V1 TPEX fetcher 後，`_format_template` 與兩個 V1 URL 常數同時失去唯一呼叫端（無測試，同批刪除）；`_read_tpex_csv`（CC 16）與 `_extract_first_date` 亦成孤兒，因有既有測試而延後，已於 commit 3a58c29 連同 9 個測試一併刪除。`upsert_stocks` 的外部依賴已查證：下游 TWStockAnalysis 只在 split plan 的 docs 提到本 repo，無程式碼 import，且其自身已無此函式。`sources.py` 1540 → 1322 行。

### [x] P2 — `_fetch_ohlcv_with_fallback` extract method

- **檔案／函式**：`run.py:1270-1389`
- **transformation**：extract method
- **覆蓋率**：**97%**（62 行執行 / 2 行未覆蓋）——全 repo 高複雜度函式中覆蓋最好的
- **爆炸半徑**：**低**。LSP `findReferences` 共 13 refs：`src/` 內只有 `run.py:859` 一個呼叫點，
  其餘 11 個在 3 個測試檔（`test_row_source_provider`、`test_run_ohlcv_change`、`test_run_ohlcv_source_order`）
- **預估收益**：CC 32 (E) 是全 repo 第三高，拆成 3–4 個具名子函式後預期降到各 8–12 (B)。
  這段是 OHLCV 多來源 fallback 順序，是最近效能改動（`7a3ed8b`）碰過的區域
- **為何是第一順位的真正 refactor**：唯一同時滿足「CC ≥ 30」「覆蓋率 ≥ 90%」「src fan-in = 1」的函式
- **紅線**：CLAUDE.md 明載 `change` 不可取自 `STOCK_DAY_ALL`、且 change 取得不可併進
  `STOCK_DAY` 月表區塊的 `any(v is None ...)` 條件。拆函式時這兩條界線不能被合併掉

**實際結果**：CC 32 (E) → **11 (C)**，抽出 `_find_symbol_row` A(4)、`_ohlcv_from_row` A(1)、`_fill_missing_ohlcv` B(6)、`_fill_ohlcv_from_stock_day` B(8)、`_lookup_change` A(4)。各來源觸發條件留在主函式，CLAUDE.md 兩條紅線因此保持；`_lookup_change` 讓「change 是獨立區塊」從註解約定變成結構保證。除既有測試外另跑差異測試：舊實作 vs 新實作對 513 組輸入（來源缺席/NaN 組合 × market_type 三態 × STOCK_DAY 成功失敗 × cache 冷熱）比對回傳值，差異 0。

### [x] P3 — `_build_daily_rows` extract method

- **檔案／函式**：`run.py:1068-1220`
- **transformation**：extract method
- **覆蓋率**：85%（47 執行 / 8 未覆蓋）
- **爆炸半徑**：低—中。src 內 9 處提及、tests 17 處
- **預估收益**：CC 31 (E) → 預期拆成 3 段各 10 左右；155 行是 `run.py` 第五長函式
- **紅線**：`_stock_sources_ok` 的「逐檔跳過半套資料」語意、處置股欄位 `0` 哨兵值語意

**實際結果**：CC 31 (E) → **9 (B)**，抽出 `_to_lots` A(2)、`_insti_total_lots` B(7)、`_turnover_rate` A(5)、`_short_margin_ratio` A(4)、`_resolve_margin_data` A(4)、`_resolve_holding_pct` A(3)。`_to_lots` 是唯一達 3 次門檻的抽取（4 個呼叫點）。**差異測試抓到一個我自己引入的錯誤**：把 `if margin_balance > 0` 改寫成 `if margin_balance <= 0: return None` 在 NaN 下不等價（NaN 的 `>` 與 `<=` 同時為 False，取補集會讓 NaN 漏進除法），`_turnover_rate` 的 `shares > 0` 同理。兩處改回正向條件並就地註明；窮舉 60 組後差異 0。

### [x] P4 — `_fetch_disposition` extract method

- **檔案／函式**：`run.py:560-636`
- **transformation**：extract method
- **覆蓋率**：**100%**（39/39）
- **爆炸半徑**：低。src 5 處、tests 14 處（`test_disposition.py` 專門測它）
- **預估收益**：CC 18 (C) → 預期 3 段各 6–8。100% 覆蓋讓這項幾乎零風險
- **紅線**：45 天回看窗口（`_DISPOSITION_LOOKBACK_DAYS`）、`DispositionData.resolve` 的
  三態語意（TRUE/FALSE/NULL 不可混為二態）

**實際結果**：CC 18 (C) → **4 (A)**，抽出 `_fetch_market_disposition_frames` A(3)、`_merge_disposition_minutes` A(5)、`_expand_disposition_frames` B(6)、`_print_disposition_summary` A(4)。三層巢狀迴圈（市場 → frame → row → 逐日）攤平成具名步驟；`market_ok` 的「全部窗口都成功才算 ok」與「同檔多筆取最小分鐘數」各自獨立成函式並就地寫明理由。原本即 100% 覆蓋。

### [x] P5 — `_prefetch_symbol_ohlcv` extract method

- **檔案／函式**：`run.py:1632-1725`
- **transformation**：extract method
- **覆蓋率**：**100%**（38/38）
- **爆炸半徑**：低。src 4 處、tests 14 處（`test_prefetch_symbol_ohlcv.py` 專測）
- **預估收益**：CC 12 (C) → 預期降到 A 級。96 行

**實際結果**：CC 12 (C) → **6 (B)**，抽出 `_month_has_trading` A(2) 與兩個具名 closure（`fetch_month_probing` / `rescue_from_other_market`）。**抽取讓兩條未測路徑現形**：覆蓋率一度由 100% 掉到 96%，因為原本隱式 fall-through 的邊界（市場別未定調且兩市場皆回空 → 跨市場補救無從進行）變成顯式 `return`。補 2 個測試後回到 100%，並確認該情境只打兩發、不會多打第三發。

### [x] P6 — `expand_twse_stock_day` / `expand_tpex_stock_day` 移出 `sources.py`

- **檔案／函式**：`sources.py:441-476`、`sources.py:479-515`
- **transformation**：拆模組（移動職責，不改邏輯）
- **覆蓋率**：92% / **100%**
- **爆炸半徑**：低。fan-in 各 7 / 8（src 2–3 + tests 5）
- **預估收益**：這兩個函式不發任何 HTTP，輸入 DataFrame 輸出 DataFrame，是 `prepare.py`
  的職責。移動後 `sources.py` 少 74 行，且是「拆 `sources.py`」這件大事最安全的第一刀
- **注意**：兩者 CC 同為 6、結構同構，但**先移動、不要順手合併**——合併是另一種
  transformation，依規則要分開的 commit；且只有 2 次重複，未達 CLAUDE.md 的 3 次門檻

**實際結果**：兩個函式移入 `prepare.py`，`sources.py` 自此不再有任何純 DataFrame 重塑函式（`find_twse_ohlcv` 因與 STOCK_DAY 月表格式強耦合暫留）。依 N1 只移動不合併，已於 `prepare.py` 就地註明。代價如預期：`prepare.py` 對 `sources.py` 的私有名稱依賴多一個 `_roc_to_date`。`sources.py` 1448 → 1371、`prepare.py` 955 → 1042。

### [x] P7 — 移除 `run.py` 三處多餘的 `import time`

- **檔案／函式**：`run.py:124`、`run.py:192`、`run.py:256`
- **transformation**：刪冗餘
- **覆蓋率**：不適用（`_fetch_issued_shares_from_api` 等三個函式所在區段部分未覆蓋）
- **爆炸半徑**：0。`time` 已在 `run.py:8` 頂層 import，函式內 import 純屬遮蔽
- **預估收益**：3 行。收益很小，但零風險，適合當暖身或搭車 commit

**實際結果**：移除 3 行。`time` 已在 `run.py:8` 頂層 import。

---

## 3. 需要你先決定的（不是純 refactor）

> D2 已於 2026-08-24 實測結案，結論見下。D1 追查後降級為一般 refactor。

這兩項在 map 裡被歸為「重複」，但實際上是**語意分歧**，機械式合併會選錯一邊。

### [x] D1 — `_get_int_col` 兩份的防護不一致（追查後：**純樣式差異，非潛在 bug**）

- **位置**：`prepare.py:436-440`（`if src_col:`）vs `prepare.py:512-516`（`if src_col and src_col in df.columns:`）
- **覆蓋率**：`prepare_twse_margin` **89%** vs `prepare_tpex_margin` **2%**
- **追查結果**：兩邊的 `cols` 建構方式不同，但**保證同一個不變量**——值必定是
  `df.columns` 的成員，或 `None`：
  - `prepare_twse_margin` 走 `_find_columns` → `_find_column`（`prepare.py:27-35`），
    後者 `for col in df.columns:` 迭代後 `return col`，回傳的必是真實欄位，否則 `None`。
  - `prepare_tpex_margin` 走 `df_cols_lower = {c.lower(): c for c in df.columns}` 再
    `.get(...)`，取出的必是真實欄位，否則 `None`。

  兩邊的 `cols` 在建構後到使用前都沒有再被改寫。因此 `and src_col in df.columns`
  在 `src_col` 為真時**恆為 True**，是多餘的檢查；`prepare_twse_margin` 沒有它也是安全的。
- **結論**：這不是「一邊漏了防護」，兩種寫法行為等價。統一成任一種都不改變行為，
  可歸入一般 refactor。**但因為 `prepare_tpex_margin` 只有 2% 覆蓋率，仍需等 T6 補完測試再動。**
- **真正的問題在別處，且兩邊都有**：pyright 對 `prepare.py:439` 與 `prepare.py:515`
  同時回報 `Type "Series | DataFrame" is not assignable to return type "Series"`。
  當 `df` 有同名重複欄位時 `df[src_col]` 回傳 DataFrame 而非 Series，`.map()` 會爆。
  這個風險兩份都有，加 `in df.columns` 也擋不掉——它檢查的是「存不存在」，不是「唯不唯一」。

**實際結果**（2026-08-24 完成）：冗餘檢查在 `prepare_tpex_margin` 內其實有**三處**，
不只 `_get_int_col`——另兩處是 `margin_cash_col and margin_cash_col in df.columns`
（原 535）與 `short_stock_col and short_stock_col in df.columns`（原 551），
成因與判斷完全相同，一併移除。兩份 `_get_int_col` 現已逐字相同。
不變量已寫在 `cols` 建構處，說明為何不需要 `in df.columns`。

依 CLAUDE.md「重複三次以上才抽象」，兩份 `_get_int_col` 只重複兩次，**未合併成
共用函式**，只是讓它們一致。

先補測試再動：新增 `tests/unit/test_prepare_tpex_margin.py` 9 個案例
（欄位齊全／選填欄缺席／必填欄缺席／大小寫不一致／缺 symbol／值不可解析），
`prepare_tpex_margin` 覆蓋率 **2% → 100%**。另跑差異測試：舊實作 vs 新實作對
299 組輸入（丟棄 0~2 個選填欄的所有組合 × 大小寫 × 多餘欄 × 壞值，加三種空表
邊界）比對回傳的完整 DataFrame，差異 0。

**過程中的額外發現（未修，超出 D1 範圍）**：`prepare_twse_margin` 的
`if margin_buy is not None and margin_sell is not None:`（現 451）恆為真——
`_get_int_col` 永遠回傳 Series（欄位缺席時回一整排 None），不可能是 None，
故其 `else: result["margin_change"] = None` 分支不可達。實際輸出仍是 None
（None 相減經 `map` 後轉回 None），行為無誤，但那道 guard 是死碼。
對應的 `prepare_tpex_margin` 檢查的是欄位名（`if margin_buy_col and ...`），
可以真的走到 else。兩者形似而語意不同，值得單獨評估。

### [x] D2 — `stat` 有效性檢查有三套判準（**已實測結案**）

三種寫法在**兩個維度**上不同，不是寬鬆度的單一階梯：

| 寫法 | `stat` 缺鍵（None） | 小寫 `"ok"` | 使用位置 |
|---|---|---|---|
| `!= "OK"` | **拒絕** | **拒絕** | `sources.py:392, 533, 900, 1331, 1361, 1386, 1403` |
| `not in {None, "OK"}` | 接受 | **拒絕** | `sources.py:591`（MI_INDEX，唯一一筆） |
| `not in {None, "ok", "OK"}` | 接受 | 接受 | `sources.py:710, 731, 806, 1009, 1072` |

#### 證據一：git 考古 → 歷史累積，非刻意

`git blame` 顯示**三種變體全部出自同一個 commit `c25738a`（2026-05-11,
「feat: 複製 sources.py（API client 全套）」）**，是整批複製進來的，此後未再被碰過。
同一個 commit 裡 TWSE 的 STOCK_DAY(392)、T86(533)、TAIEX(1331)、FMTQIK(1361) 都用嚴格版，
只有 MI_INDEX(591) 用中間版——**同一批、同一個 host、同一種端點，判準卻不一致**。

後來新增的兩處（`806` TPEX 個股月表 `5060bff7` 2026-08-22、`1072` TPEX 處置股
`2c18abf0` 2026-08-21）都用寬鬆版，與「TPEX 用寬鬆」的模式一致。

#### 證據二：實際呼叫 API（2026-08-24，4 發，間隔 30s）

用專案自己的 `build_session(min_interval=2.0)` 打 `MI_INDEX`：

| 情境 | date / params | HTTP | top-level keys | `stat` |
|---|---|---|---|---|
| A 交易日 | 20260821, type=ALLBUT0999 | 200 | `tables, type, params, stat, date` | `'OK'` |
| B 週六 | 20260822, type=ALLBUT0999 | 200 | `stat, type` | `'很抱歉，沒有符合條件的資料!'` |
| C 無效 type | 20260821, type=NOSUCHTYPE | 200 | `groups, tables, type, stat, date` | `'OK'` |
| D 缺 type | 20260821 | 200 | `tables, type, stat, date` | `'OK'` |

**四種情境（含兩種畸形請求）payload 一律是 dict，且一律帶 `stat` 鍵。**
`sources.py:591` 的 `None` 分支在實測範圍內**不可達**。

證據的界限：4 發不能證明「永遠不會缺 `stat`」。未涵蓋的是 5xx（被 `raise_for_status()`
擋在前面）與維護頁面（會在 `.json()` 就失敗）。但在正常 JSON 回應的範圍內證據一致。

#### 結論與最優解法

- **`sources.py:591` 收斂成 `!= "OK"`**，與同一 commit 的其他 4 個 TWSE 端點一致。
  依實測這是行為保持的（`None` 分支不可達），且移除一個誤導性的死分支。
- **TWSE 嚴格 / TPEX 寬鬆的分野維持不動**。小寫 `"ok"` 確實只出現在 TPEX
  （`tests/unit/test_tpex_stock_day.py:59`），這個分野有實證基礎。
- **不要把 TWSE 放寬成接受 `None`**：`[[twse-rate-limit-ambiguous-response]]` 記載
  TWSE 限流回 HTTP 200 + `{"stat":"很抱歉，沒有符合條件的資料!"}`，與真休市同字串。
  上面情境 B 正是這個字串。嚴格版與中間版都會拒絕它（行為相同），但放寬到接受缺鍵
  只會讓異常 payload 更容易被當成有效資料。

#### 順帶發現：`sources.py:591` 與 `594` 的順序是錯的（潛伏，非現行）

```python
591    if payload.get("stat") not in {None, "OK"}:      # 先用了 payload.get()
592        raise DataUnavailableError(...)
594    if not isinstance(payload, dict):                 # 才檢查是不是 dict
595        raise DataUnavailableError("TWSE MI_INDEX 回傳格式異常")
```

若 API 回傳 JSON 陣列，591 會先丟 `AttributeError`（list 沒有 `.get`），
594 那道防護永遠到不了，且 `AttributeError` 不是 `DataUnavailableError`，
不會走既有的錯誤處理路徑。4 發實測都回 dict，所以這是潛伏問題不是現行故障。
修正方式是把 594-595 移到 591 之前——但這是另一種 transformation，應分開的 commit。

## 4. 需先補測試（覆蓋率不足，不可直接動）

依 CLAUDE.md `沒有測試覆蓋到的檔案，先跟我確認再改`。以下按「補測試的投報比」排序——
CC 高、fan-in 低者優先，因為測試好寫、收益大。

### [x] T1 — `_run_for_date`（六步完成）

- **位置**：`run.py:1765-2063`（308 行）
- **CC / 覆蓋率**：**55 (F，全 repo 最高)** / **21%**（34 執行 / 127 未覆蓋）
- **爆炸半徑**：高。src 4 處呼叫（`run.py:2525, 2587, 2607`）+ tests 6 處
- **為何在這區**：CC 55 表示走完所有分支需 55 條獨立路徑，目前只覆蓋約五分之一。
  這是全 repo 最需要重構、也最不能貿然重構的函式
- **建議**：不要一次補到高覆蓋。先針對要動的那一段補，再動那一段

**實際結果**（2026-08-24，六步完成）。

分析後的關鍵發現：**這不是一個 CC 55 的函式，是兩個演算法被
`if provider is not None:` 黏在一起**——per-symbol 路徑（27 行）自己組列、寫入、
`return True`，完全不碰後面 227 行的批次路徑，兩者唯一共用的只有前面的守衛。
這決定了拆法：先把可獨立的階段抽掉，最後才分離兩條路徑。

| 步驟 | 內容 | CC |
|---|---|---|
| 1 | 補 batch 路徑測試（原 2043 行後全是 0%） | 21% → **100%** |
| 2 | `_resolve_margin_sources` + 兩個子函式（H 段 69 行 / 19 分支） | 55 → 40 |
| 3 | `_fetch_day_all_source` / `_fetch_mi_index_source` | 40 → 30 |
| 4/5 | `_fetch_tpex_batch` / `_fetch_holding_pct_per_symbol` | 30 → 19 |
| 6 | 分離 `_run_date_per_symbol` / `_run_date_batch` | 19 → 分派 **A (5)** |

最終形狀：`_run_for_date` A (5) 58 行（守衛 + 分派）、`_run_date_per_symbol`
A (4) 46 行、`_run_date_batch` C (12) 117 行。三者覆蓋率皆 100%。
呼叫端與既有測試都不用改——`_run_for_date` 保留原簽名當分派。

**第 3 步偏離了原訂計畫**：原本要把 STOCK_DAY_ALL 與 MI_INDEX 合併成一個
共用的「抓來源並驗日期」helper（兩次重複，達門檻）。實際比對後放棄——兩者的
日期驗證政策不同（MI_INDEX 有「沒宣告日期但抓的是今天就當今天」的 fallback、
STOCK_DAY_ALL 對無法解析的日期有專屬訊息且只在 `date == today` 執行）。
屬 CLAUDE.md 說的「結構相似但語意不同」，參數化只會把差異藏進參數裡。

**副產品**：`write_market_daily` 在 per-symbol 路徑其實是死參數，分離後那條
路徑根本沒有這個參數。

驗證方式（每一步都做）：以 git HEAD 的舊實作與新實作跑**完整的
`_run_for_date`**，比對 stdout 內容、回傳值、以及各種副作用。六步合計
216 + 100 + 60 + 432 組輸入，差異 0。第 2 步曾因多包一層 `_phase` 讓 stdout
多出兩行而被比對抓到——這是為什麼驗證要含 stdout 而不只是回傳值。

### [x] T2 — `_fetch_and_upsert_market_daily`

- **位置**：`run.py:2166-2223`（58 行）
- **CC / 覆蓋率**：16 (C) / **2%**（1 執行 / 41 未覆蓋）
- **爆炸半徑**：**低**（src 1 個呼叫點、tests 0）
- **為何優先**：低 fan-in + 高 CC + 幾乎零覆蓋 = 測試好寫、收益大。
  且它是唯一寫 `market_daily` 的路徑（CLAUDE.md 列為共用表），目前完全沒有回歸保護
- **map 漏掉這個函式，這是本次交叉驗證發現的最大缺口**

**實際結果**（2026-08-24）：新增 `tests/unit/test_market_daily.py` 14 個案例，
覆蓋率 **2% → 100%**。

核心不變量寫成參數化測試：四個子來源（TAIEX OHLC / 成交金額 / 外資買賣超 /
融資餘額）各自獨立失敗，任一失敗不影響其他三個、也不阻止寫入已取得的部分——
搭配 `upsert_market_daily` 的 COALESCE，缺的欄位不會覆寫舊值。四者全失敗才完全
不寫。

另外釘住三個容易回歸的細節：
- `prev_margin_balance` 只用來校正 D-1，會被 `pop` 掉，不可寫進 `market_daily`。
- 月表回傳整月資料，請求日不在其中時不可誤用別天的值。
- D-1 校正是附加動作，它拋例外不可拖累當日大盤資料的寫入；舊值為 None 時
  印 `NULL` 而非讓格式化丟例外。

### [x] T3 — `_dahu_command`

- **位置**：`run.py:277-348`（72 行）
- **CC / 覆蓋率**：17 (C) / **2%**（1 執行 / 45 未覆蓋）
- **爆炸半徑**：**最低**（src 1 處、tests 0）
- **為何優先**：獨立子命令，邊界清楚，是整份清單裡最容易補測試的高 CC 函式

**實際結果**（2026-08-24）：新增 `tests/unit/test_dahu_command.py` 14 個案例。
`_dahu_command` **2% → 100%**，同批把兩個相鄰 helper 也補滿：
`_resolve_dahu_dates` 100%、`_fetch_tdcc_with_retry` 87% → 100%。

涵蓋：標的選取（`--stocks` 明列／空白與空項過濾／空值中止／改用 enabled 清單／
清單為空中止）、日期解析（未給區間取最新一週／給區間取窗口內全部／窗口外不寫）、
失敗處理（取 token 失敗即中止不寫／單檔連續失敗達 `_TDCC_MAX_ATTEMPTS` 後跳過該檔
但不影響其他檔／大戶比例無法解析跳過／散戶比例 None 仍寫入，因 COALESCE 保護歷史值／
股名空字串轉 None／換 token 也失敗時仍跑完重試不讓例外逸出）。

不碰網路與 DB：`session` 傳 None，`load_stock_names` / `get_enabled_stocks` /
`fetch_tdcc_*` / `prepare_tdcc_*` / `upsert_holder_percent` / `time.sleep` 全部
monkeypatch，並記錄呼叫以斷言重試次數與寫入內容。

### [x] T4 — `_get_margin_data`

- **位置**：`run.py:1415-1468`
- **CC / 覆蓋率**：15 (C) / 21%
- **爆炸半徑**：低（src 1 處、tests 0）

**實際結果**（2026-08-24）：新增 `tests/unit/test_get_margin_data.py` 11 個案例，
覆蓋率 **21% → 100%**。釘住的關鍵語意是「TWSE 命中即返回」——只要該檔出現在
TWSE 整批裡就直接回傳，即使有欄位是 NaN 也不去 TPEX 補（上市股不該拿上櫃資料
補洞）。這條靠 TWSE 分支尾端的 `return` 撐住，很容易在重構時被誤刪成 fall-through。

有了 100% 防護網後，依新的兩次門檻順手合併函式內的逐字重複：兩段完全相同的
填值迴圈提為 `_fill_margin_from(result, df, symbol) -> bool`（回傳是否命中），
欄位清單提為模組常數 `_MARGIN_FIELDS`（原本以 dict literal 寫死一次、
`result.keys()` 迭代兩次）。`_get_margin_data` CC **15 → 2 (A)**，
`_fill_margin_from` B (8)。以舊實作對 147 組輸入（兩來源各 7 種形態 × 三個
查詢代號）比對回傳 dict，差異 0。

### [x] T5 — `correct_prev_margin_balance`

- **位置**：`db_utils.py:542-612`（71 行）
- **CC / 覆蓋率**：8 (B) / **4%**（1 執行 / 24 未覆蓋）
- **爆炸半徑**：低（src 2 處、tests 0）
- **為何值得補**：它做「前日／前前日餘額一致性修正」，是 `db_utils.py` 邏輯最繞的一段，
  且與 `_consensus_prev_trade_date` 的 gap 處理耦合。無測試等於無保護

**實際結果**（2026-08-24）：新增 `tests/unit/test_correct_prev_margin_balance.py`
10 個案例，覆蓋率 **4% → 100%**（連帶 `_consensus_prev_trade_date` 也到 100%）。

這是 `db_utils` 邏輯最繞的一段——要處理 D-1 與 D-2 兩層共識日推算，且
「找不到共識」與「值為 NULL」必須導向不同結果。測試把四種放棄條件
（兩表 MAX 不一致／任一表缺日／market_daily 該日無列／新舊值完全一致）與
三種 NULL change 來源（D-2 無共識／D-2 該日 balance 為 NULL／D-2 無列）
分開釘住，另驗證「balance 已一致但 change 仍 stale 也要修」這條容易被
`if balance == api_balance: return` 之類的簡化誤刪的路徑。

### [x] T6 — `prepare_tpex_margin` / `prepare_tpex_margin_v2`

- **位置**：`prepare.py:478-557`、`prepare.py:560-635`
- **CC / 覆蓋率**：14 (C) / **2%**、16 (C) / **3%**
- **爆炸半徑**：低（各 src 3 處、tests 0）
- **關聯**：D1 的分歧點在 `prepare.py:512`，就在這裡。補完測試才有辦法處理 D1

**實際結果**：兩半分別完成。`prepare_tpex_margin` 於 D1（commit 596dc22）補測試
達 100%；`prepare_tpex_margin_v2` 於 2026-08-24 新增
`tests/unit/test_prepare_tpex_margin_v2.py` 15 個案例，**3% → 100%**。

V2 最容易壞的是欄位歧義修正：`_find_column` 用「包含」比對，而 API 回應裡
`前資餘額(張)` 排在 `資餘額(張)` 之前，`資餘額` 會先比中前者；函式內有一段
專門重找非「前」的那一欄。測試用真實欄位順序釘住它，並另加一個把兩欄對調的
案例，確認修正邏輯不依賴特定排列。

另涵蓋兩條 change 計算路徑（有前餘額欄時用餘額差、缺席時退回
買−賣−償還、連償還欄都沒有時以 0 計而非 NaN）、券資比與 0 餘額、
五個必填欄位各自缺席時拋 DataUnavailableError。

`prepare.py` 全檔覆蓋率 52% → 75%。

### [x] T7 — `fetch_moneydj_margin`

- **位置**：`sources.py:1081-1176`
- **CC / 覆蓋率**：15 (C) / **22%**
- **爆炸半徑**：中（src 5 處、tests 9 處）
- **不對稱**：孿生的 `fetch_moneydj_holding_pct` 覆蓋率 88%、CC 13。同一組邏輯，
  一邊有保護一邊沒有。`_is_valid_date_row` 的重複（`sources.py:1141` / `1277`）就跨在這兩者之間

**實際結果**（2026-08-24）：新增 `tests/unit/test_moneydj_margin.py` 10 個案例，
覆蓋率 **26% → 100%**。用 conftest 的共用 `FakeSession`（該批 HTTP 假物件於
commit 36b44e4 集中）。

這張表是**靠位置取欄**的（0:日期 1:資買 2:資賣 4:資餘額 5:資增減 8:券賣
9:券買 11:券餘額 12:券增減），所以「挑中哪張表」與「濾掉哪些列」是正確性關鍵——
挑錯表或混進合計列都會整批錯位，而且不會有任何錯誤訊號。測試分別釘住：
表格選取（欄數不足 12 的小表不可誤選、子表頭缺「日期」不可誤選、
位置對應正確）與列過濾（合計列、西元日期列被丟掉、全部無效時拋
DataUnavailableError）。

另釘住請求參數形狀：MoneyDJ 吃 `YYYY-M-D`，補零會查不到。

`read_html` 的 ValueError 刻意不在函式內攔截（讓 retry 吸收暫時性壞 HTML）
這條也寫成測試，但必須把 `sources.time.sleep` 停掉，否則單這一個案例會真的
等完整個 backoff（實測 8.6 秒）。

### [x] T8 — `upsert_holder_percent`

- **位置**：`db_utils.py:471-516`
- **CC / 覆蓋率**：7 (B) / **5%**
- **爆炸半徑**：低（src 2 處、tests 0）

**實際結果**（2026-08-24）：新增 `tests/unit/test_upsert_holder_percent.py`
11 個案例，覆蓋率 **5% → 100%**。

重點釘住 ON CONFLICT 的三欄語意刻意不一致：`name` 與 `retail_ratio` 用
COALESCE（不可把既有股名／歷史散戶比例蓋成 NULL），`major_ratio` 則直接
EXCLUDED 覆寫（那是本次要更新的主資料）。另涵蓋空 rows／全空白 symbol 完全
不觸 DB、空白 symbol 過濾、空白股名轉 None（才能讓 COALESCE 生效）。

### [x] T9 — `_main_inner`

- **位置**：`run.py:2406-2613`（212 行）
- **CC / 覆蓋率**：32 (E) / 41%
- **爆炸半徑**：低（src 1 處）但它是 CLI 總分派，改壞會影響所有子命令

**實際結果**（2026-08-24）：新增 `tests/unit/test_main_inner_dispatch.py` 26 個案例，
覆蓋率 **41% → 100%**。

只測「分派」與「前置閘門」，不重測各子命令自身的邏輯（那些已有專屬測試檔）：

- **子命令路由**：四個旗標各自走對分支，且**走完就停**——不可再跑到後面的模式。
  另釘住優先序（`--update-shares` > `--dahu` > `--backfill-limits`），
  同時給多個旗標時的行為是明確的而非碰運氣。
- **休市閘門**：只擋純 daily 模式；`--date` / `--backfill-*` / `--update-shares` /
  `--dahu` 完全不查 `config.is_trading_day`。讀取失敗時 **fail-open**——
  排程不可因 DB 抖動整天不跑。
- **`--backfill-stocks` 的守衛**：缺 start/end 中止、代號清單全空白中止、
  `write_market_daily=False`（CLAUDE.md 不變量：逐檔回補不動共用大盤表）、
  起訖顛倒要正規化（否則 `_month_starts` 回空 list，整段靜默 no-op 卻照樣印抬頭）。
- **一般回補與單日**：`--force` 對應 `skip_existing=False`、只給
  `--backfill-start` 或只給 `--backfill-end` 時另一端取 `target_date`、
  一天都沒寫入時不做 D-1 margin 修正。

`run.py` 全檔覆蓋率 53% → 74%。

---

## 5. 達到 3 次門檻、但要等前置條件

CLAUDE.md：`不新增抽象層，除非同一段邏輯已重複三次以上`。以下三項**已達門檻**，
但覆蓋率不足以安全抽取，需等第 4 節相關項目完成。

### [x] X1 — HTTP 呼叫前置樣板（TWSE / TPEX / MoneyDJ 完成，TDCC 維持現狀）

- **位置**：`sources.py` 全檔，`urllib3.disable_warnings(...)` 23 次、`verify=False` 23 次、
  `raise_for_status()` 24 次
- **transformation**：新增抽象層（已達 3 次門檻，是全 repo 最強的抽取理由）
- **覆蓋率**：分散，多數落在 `sources.py` 未覆蓋的 373 行內
- **爆炸半徑**：**最高**——動到所有 24 個 fetcher
- **收益**：`sources.py` 預估減少 40–60 行；「停用 TLS 驗證」這個決定從 23 份收斂成 1 份
- **前置**：建議排在最後。單一 commit 涵蓋 24 個 fetcher 違反「一次一種 transformation」的精神，
  可考慮分批（TWSE 一批、TPEX 一批、MoneyDJ/TDCC 一批）

**進度**（2026-08-24）：**TWSE 群已完成**，TPEX / MoneyDJ / TDCC 三群待辦。

前置：新增 `tests/unit/test_twse_request_shape.py` 24 個案例，只涵蓋「請求怎麼送、
回應怎麼進入解析」這一層——每個 fetcher 的目標 URL、完整 params、以及
`raise_for_status` 的例外不可被吞掉。

抽出 `_twse_get(session, url, params)`，涵蓋十個 fetcher。**邊界刻意停在
「送出請求並確認 HTTP 狀態」，不含回應解析**——這是 X2 檢討得到的教訓：
各端點在 `raise_for_status` 之後差異很大（stat 判準嚴格 / 寬鬆兩種、
`fetch_twse_stock_day` 先看 `response.text` 是否空白、`fetch_twse_company_basic`
要改編碼走 `.text`、`fetch_twse_stock_day_all` 回 list 而非 dict），
一併吞進共用函式只會讓真正的差異藏進參數裡。

`fetch_twse_disposition` 刻意不納入：它是這群裡唯一沒有 `verify=False` 的，
納入會改變該端點的 TLS 行為。

收斂成果（`sources.py`）：

| 樣板 | X2 完成時 | 現在 |
|---|---:|---:|
| `urllib3.disable_warnings` | 21 | **10** |
| `verify=False` | 21 | **12** |
| `raise_for_status` | 22 | **13** |

`verify=False` 這個決定從十份收斂成一份，日後要改回驗證憑證只需動 `_twse_get`。

驗證：以舊實作對 120 組輸入（八個 fetcher × 三種連線結果 × 五種 payload）
比對回傳值、例外型別與訊息、以及實際送出的 (url, params)，差異 0。

**剩餘三群的性質**（尚未評估是否值得比照辦理）：
- TPEX：`fetch_tpex_stock_day` / `fetch_tpex_company_basic` / `fetch_tpex_margin` /
  `fetch_tpex_disposition`，另有 `_fetch_tpex_v2` 已自成一群。
- MoneyDJ：兩個 fetcher，走 `read_html`、PER_SYMBOL retry profile。
- TDCC：兩個 fetcher，需要 token 鏈接，其中一個是 POST 而非 GET。


---

**X1 收尾（2026-08-24）**：TPEX 與 MoneyDJ 兩批完成，TDCC 刻意維持現狀。

`_twse_get` 泛化為 `_http_get`——它本來就沒有任何 TWSE 專屬邏輯，只是名字取窄了。
現涵蓋 TWSE 群（10 處）、TPEX 群（4 處 + `_fetch_tpex_v2` 內部）、MoneyDJ（2 處）。

累計收斂（以 X2 完成時 e55948d 為基準）：

| 樣板 | 起點 | 完成 |
|---|---:|---:|
| `urllib3.disable_warnings` | 21 | **3** |
| `verify=False` | 21 | **6** |
| `raise_for_status` | 22 | **6** |

**三處刻意不納入**：

| 對象 | 理由 |
|---|---|
| `fetch_twse_disposition` | 唯一沒有 `verify=False` 的端點，納入會改變其 TLS 行為 |
| `fetch_tdcc_distribution` | 是 `POST(data=...)`，要納入得給 `_http_get` 加 method 參數——為一個呼叫點擴大共用函式的職責 |
| `fetch_tdcc_token_and_dates` | 形狀相同但只遷移它會讓 TDCC 那一對不對稱（同一組流程一半用 helper 一半不用），且兩者覆蓋率僅 6% / 4% |

前置測試：`test_twse_request_shape.py` 24 個 + `test_tpex_request_shape.py` 20 個，
只涵蓋「請求怎麼送、回應怎麼進入解析」這一層。過程中兩次靠測試發現原本以為
一致的地方其實不同：`fetch_twse_stock_day` 在 `json()` 前多一道空白 body 檢查
（限流防護）、`fetch_tpex_stock_day` 的參數是 `code` 且日期用西元（程式碼註解
標為「坑 1/2」，原本只有註解沒有測試）。這兩點都直接影響 helper 該抽到哪一層。

驗證：三批各自以舊實作做差異測試，共 120 + 135 + 24 = 279 組輸入，差異 0。

**取捨記錄**：`verify=False` 集中之後，改動會同時影響 TWSE / TPEX / MoneyDJ 三家
上游。若日後只想對其中一家恢復憑證驗證，得先把 `_http_get` 拆開。這點已寫進
它的 docstring。

### [x] X2 — TPEX V2 fetcher 三重複

- **位置**：`sources.py:698-715`、`719-757`、`993-1014`
- **transformation**：新增抽象層（恰好 3 次，達門檻）
- **覆蓋率**：8% / 5% / 42%
- **爆炸半徑**：中（fan-in 6 / 2 / 5）
- **前置**：三者覆蓋率都低，需先補測試

**實際結果**（2026-08-24）：先補 `tests/unit/test_tpex_v2_fetchers.py` 27 個案例
把三者由 8%/5%/42% 補到 **100%**，再抽出 `_fetch_tpex_v2`。

三個 fetcher CC 各 6 → 1/1/2（皆 A），共用函式 A (4)。
`sources.py` 的 `urllib3.disable_warnings` 與 `verify=False` 各由 21 降到 19，
TPEX 的 stat 判準由 6 處降到 4 處。

兩個刻意不做的決定寫在程式碼裡：
- 3insti 的欄位位置改名**不下沉**到共用函式——那是該端點獨有的（欄名重複，
  只能靠位置區分），放進共用流程會讓另外兩個 fetcher 讀起來像也需要它。
- stat 判準沿用 TPEX 寬鬆版，docstring 註明不可為了「統一」收斂成 TWSE 嚴格版
  （理由見 D2 實測）。這是抽共用函式最容易順手做錯的地方。

驗證：以舊實作對 288 組輸入（三個 fetcher × 表格標題 × 四種 stat × 三種日期字串
× 欄數 3/24 × 有無 stat 鍵）比對欄位、資料、data_date 與實際送出的請求參數，差異 0。

**對 X1 的啟示**：這次抽取沒有踩到「差異落在語意層」的問題，因為三者確實只差
參數。X1 的 23 處 HTTP 樣板橫跨 TWSE/TPEX/MoneyDJ/TDCC 四種來源，stat 判準、
編碼處理、retry profile 都不同，不能照搬這個模式——需要先分群。

### [x] X3 — `db_utils.py` 連線樣板（15 次）

- **位置**：`db_utils.py` 內 16 個 public 函式各自 `pool = get_pool(database_url)` + `with pool.connection()`
- **transformation**：新增抽象層（16 次，遠超門檻）
- **覆蓋率**：不均——`update_price_limits_batch` / `update_disposition_batch` 100%，
  但 `upsert_holder_percent` 5%、`correct_prev_margin_balance` 4%、`upsert_stocks` 12%
- **爆炸半徑**：高（LSP 確認 `get_pool` 20 refs，其中 16 個在此檔）
- **前置**：需 T5 / T8 完成

**實際結果**（2026-08-24）：實際是 15 次而非 16——`upsert_stocks` 已於 P1 刪除。

前置作業：先補 `tests/unit/test_db_utils_queries.py` 15 個案例，把七個覆蓋率
20% 以下的函式（五個唯讀查詢 + `upsert_stock_shares` + `upsert_market_daily`）
補到 100%，`db_utils.py` 全檔 **75% → 99%**，才有本錢動共用樣板。

重構本體：
- `_connect(url)` contextmanager 取代 `pool = get_pool(url)` + `with
  pool.connection()`，呼叫點 **15 → 1**。
- `_fetch_all` / `_fetch_one` 讓五個唯讀查詢只剩「SQL + 轉換」兩件事。
- `_connect` 刻意不負責 commit：各寫入函式的 commit 時機不一致，統一會改變
  交易邊界，就不是行為保持的重構了。

檔案行數 587 → 590（多的是三個 helper 的簽名與 docstring），但日後要改連線
行為（逾時、retry、metrics）只需動一個地方。

驗證：以舊實作對 14 個函式各跑代表性輸入，比對回傳值、cursor 收到的 SQL/params、
以及 commit 與否，差異 0。

---

## 6. 明確不做

### [x] N1 — 門檻改為兩次後，逐字重複已全部合併（2026-08-24）

CLAUDE.md 的抽象門檻由三次改為**兩次**：確認邏輯相同就重用；結構相似但語意不同
（欄位、單位、端點、來源不同）者不算，需個別判斷是否值得參數化。

**已合併（逐字相同）**：

| 原重複處 | 合併為 | 覆蓋率 |
|---|---|---|
| `prepare.py` 兩份 `_get_int_col` | `_int_col_or_nulls(df, cols, col_name)` | 100% |
| `prepare.py` 兩份 `_parse_moneydj_date` | 模組層級 `_parse_moneydj_date` | 100% |
| `sources.py` 兩份 `_is_valid_date_row` | `_is_moneydj_date_row` + `_MONEYDJ_ROC_DATE_RE` | 100% |

三組都先補測試再合併（新增 `test_prepare_tpex_margin.py` 9 個、
`test_prepare_moneydj.py` 23 個），`prepare_tpex_margin` 2% → 100%、
`prepare_moneydj_margin` 5% → 100%。

**未合併（結構同構但語意不同，需個別判斷是否值得參數化）**：

| 對象 | 差異 | 覆蓋率 | 備註 |
|---|---|---|---|
| `update_price_limits_batch` / `update_disposition_batch`（`db_utils.py`） | SET 欄位名、`%s::` 型別轉換（numeric,numeric vs boolean,smallint） | 100% / 100% | 骨架逐行同構，參數化代價低；但把欄位名與型別轉換變成字串參數會削弱可讀性與型別檢查 |
| `expand_twse_stock_day` / `expand_tpex_stock_day`（`prepare.py`） | 欄位名（`日期` vs `日 期`、`開盤價` vs `開盤`）、**成交量單位（股 vs 張，後者需 ×1000）** | 92% / 100% | 單位換算是語意差異不是參數差異，合併需在函式內分支，可讀性反而變差 |
| `fetch_twse_taiex_ohlc` / `fetch_twse_market_volume`（`sources.py`） | URL、回傳結構（4 欄 dict vs 單一 int） | 7% / 7% | 前 7 行同構，後半完全不同；且兩者覆蓋率都極低，合併前需先補測試 |
| `_backfill_limits_command` / `_backfill_disposition_command`（`run.py`） | 忽略的參數不同（前者忽略 `--backfill-stocks`，後者支援它） | 84% / 77% | 只有參數驗證前置段同構，主體邏輯不同 |

這四組的共通點是**差異落在語意層而非參數層**，機械式合併會把「兩個東西不一樣」
這個事實藏進 if 分支或字串參數裡。建議個別評估，不隨新門檻自動合併。

### [ ] N2 — 暫不拆 `run.py` / `sources.py` 成多個模組

- **理由**：map 指出兩者職責過寬（`run.py` MI 0.00、`sources.py` MI 5.12），拆模組是最終目標。
  但拆模組會同時改動大量 import，違反「一次只做一種 transformation」，
  且目前兩檔覆蓋率各 53% / 52%，沒有足夠的回歸保護
- **前置**：P1–P7 與第 4 節完成後再評估。P6 是這個方向的第一小步

---

## 7. 建議執行順序

```
P1 (刪死碼, 5 commits)
  └─ P7 (import time, 可搭車)
P2 → P3 → P4 → P5        # 高覆蓋率的 extract method，風險遞增排列
P6                        # 拆模組的第一刀
D1 / D2                   # 需要你的決定
T3 → T2 → T4 → T5 → T8    # 補測試，低 fan-in 優先
T6 → D1 收尾
T7 → T1 → T9
X3 → X2 → X1              # 抽象層，爆炸半徑遞增
N2 重新評估
```

第一輪（P1–P7）預估：`src/` 減少約 270 行，移除 1 個 rank E 函式，
另外 4 個 E/C 函式降級，全程都在 85%+ 覆蓋率的保護下。
