# 小台／微台 散戶多空比原始口數 設計

日期：2026-11-25　狀態：待核准

## 目的

使用者要看**小型臺指（小台）**與**微型臺指（微台）**的散戶多空比，自己看盤參考、用來判斷
階段性頂部。本 repo 只存原始資料：**只存原始口數**（全市場未平倉、三大法人多／空單未平倉），
散戶多空比由下游 `TWStockAnalysis` 計算。

散戶多空比（下游算，寫在這裡是為了說明需要哪些原始數字、以及驗證時怎麼算）：

```
散戶多單 = 全市場未平倉 − 三大法人多單未平倉
散戶空單 = 全市場未平倉 − 三大法人空單未平倉
散戶多空比 = (散戶多單 − 散戶空單) ÷ 全市場未平倉
          = (三大法人空單 − 三大法人多單) ÷ 全市場未平倉
```

2026-11-24 實算：小台 法人多 12,678、空 8,852、全市場 33,055 → −11.57%；
微台 法人多 14,768、空 41,629、全市場 75,681 → +35.49%。

## 範圍

**Goals**
- `market_daily` 新增 6 欄（單位：口，BIGINT）：

  | 欄位 | 意義 |
  |---|---|
  | `mtx_market_oi` | 小台全市場未平倉量 |
  | `mtx_inst_long_oi` | 小台三大法人多方未平倉口數合計 |
  | `mtx_inst_short_oi` | 小台三大法人空方未平倉口數合計 |
  | `tmf_market_oi` | 微台全市場未平倉量 |
  | `tmf_inst_long_oi` | 微台三大法人多方未平倉口數合計 |
  | `tmf_inst_short_oi` | 微台三大法人空方未平倉口數合計 |

- 每天抓：`_run_for_date` 的大盤段，緊接大台三大法人未平倉之後。
- 一次性 script 回補歷史（使用者選的做法 A）：小台從法人端點最早可查日（近三年，約 2023-11-27）起，
  微台從上市日 2024-07-29 起；都只到 `market_daily` 已有的列。
- 補完後與**玩股網**的小台／微台散戶多空比比對正確性（見「驗證」）。

**Non-goals**
- 在本 repo 算散戶多空比或任何比例。
- 拆外資／投信／自營商身份別（比例只需要合計；使用者知悉代價：以後想看身份別只拿得到近三年）。
- 新增 CLI flag；擴充 `--backfill-futures-oi`（做法 B，未選）。
- 三年以前的小台歷史（法人端點拿不到）。

**Anti-goals**（技術上可行也算失敗）
- 改變大台 `txf_*` 或 `market_daily` 其他欄的寫入結果；`--backfill-futures-oi` 行為不變。
- 回補 script INSERT 只有期貨欄的 `market_daily` 列（會被 `find_consensus_prev_trade_date` 當成 D-1，
  打亂融資校正；同 `--backfill-futures-oi` 的 Gotcha）。
- 把「沒抓到」寫成 0；把半套法人（三個身份別缺一）加總寫入。
- 這段任何失敗讓 daily 中斷或以 traceback 結束。

## 資料來源（2026-11-25 實測）

兩支都是期交所 POST、回 cp950 CSV（content-type 標 text/html），錯誤是 HTTP 200 + HTML `alert("…")`。
UA 用 `tw-stock-rawdata/0.1`（`build_session` 既有設定）。

### 三大法人未平倉：`POST https://www.taifex.com.tw/cht/3/futContractsDateDown`

- 與大台現在用的是同一支（`fetch_taifex_txf_institutional`），form 只差 `commodityId`：
  小台 **`MXF`**（填 `MTX` 回「查無資料」）、微台 **`TMF`**。
- CSV 每日 3 列（自營商／投信／外資及陸資），商品名稱分別是「小型臺指期貨」「微型臺指期貨」。
  要用的欄：「多方未平倉口數」「空方未平倉口數」。
- 吃區間、近三年整段一發（2023-11-27～2026-11-24，725 個交易日，一次回完）；起日超過近三年回
  「日期時間錯誤」；休市單日回「查無資料」。
- 微台從 2024-07-29 起有資料。

### 全市場未平倉：`POST https://www.taifex.com.tw/cht/3/futDataDown`

- form：`down_type=1`、`commodity_id=MTX`（小台）或 `TMF`（微台）、`commodity_id2=`（空字串）、
  `queryStartDate=YYYY/MM/DD`、`queryEndDate=YYYY/MM/DD`。
- 表頭：`交易日期,契約,到期月份(週別),開盤價,…,成交量,結算價,未沖銷契約數,…,交易時段,價差對單式委託成交量`。
- 每個交易日每個到期月份各有「一般」「盤後」兩列，另有價差列（到期月份含 `/`，如 `202612/202701`）。
  只有「一般」的單式契約列有未沖銷契約數，盤後列與價差列是 `-`。
- 小台有週契約（如 `202611W4`、`202612W1`），也算進全市場未平倉。
- **區間上限約一個月**：10/24～11/24 可以，10/24～11/25、1 年、3 年都回「日期時間錯誤」。
  可查到 2020 年以前。
- **休市日不回 alert，回只有表頭的 CSV**（197 bytes）。`commodity_id` 空字串也只回表頭（不能一發抓全部商品）。
- 當日 15:41 已有當日「一般」列（2026-11-25 實測小台 33,876），22:00 的 daily 抓得到當天。

**全市場未平倉的算法**：同一 `交易日期`、`契約` 等於目標商品、`交易時段 == 一般`、`到期月份(週別)` 不含 `/`
的列，把 `未沖銷契約數` 加總。2026-11-24 小台：865+15+27,799+3,091+83+645+271+286 = 33,055。

## 做法（已選 A）

| | 每天 | 歷史 |
|---|---|---|
| **A（選定）** | `_run_for_date` 大盤段多打 4 次期交所 | 一次性 `tools/backfill_mini_futures_oi.py` |
| B（未選） | 同 A | 擴充 `--backfill-futures-oi`：一個指令補大台＋小台＋微台、可重複用來修漏抓；但這個指令會從 1 次請求變約 65 次、改到現有指令行為 |

選 A 的理由：三年上限一補完就沒東西可補；daily 漏掉的日子，`--date` 或一般回補也會補到，不需要常駐指令。
另一個被排除的做法：只在 daily 當天抓（像 ETF 規模）——歷史來源存在，沒理由放棄 `--date`／回補的路徑。

## 設計

### 每天（`_run_for_date`）

- 位置：大台三大法人未平倉區塊之後、台指 VIX 之前。只跑在會寫 `market_daily` 的路徑（daily、`--date`、
  `--backfill-start/--backfill-end`）；`--backfill-stocks` 照舊不動 `market_daily`。
- 每個日期 4 次請求：小台法人、微台法人、小台行情、微台行情，**各自** try/except：一支失敗只是那幾欄
  不寫（印一行 log），不影響其他來源與其他欄。期交所與 TWSE 不同站，不受 TWSE pacing 影響。
- 法人：該日三個身份別都在才寫 `*_inst_long_oi`、`*_inst_short_oi`（合計）；缺任一 → 該商品兩欄都是 None。
- 寫入走既有 `upsert_market_daily`（COALESCE：None 不覆寫舊值）。
- 大台的抓取（`fetch_taifex_txf_institutional` 與它的呼叫）**行為不變**，小台／微台另外打。

### 一次性 script：`tools/backfill_mini_futures_oi.py`

- 慣例同 `tools/backfill_etf_month_aum.py`：預設模擬、`--apply` 才寫；`DATABASE_URL` 取環境變數（沒有才讀 `.env`），
  執行前印出目標 host；結束時 `close_pool()`。
- 範圍：每個商品 `[起日, market_daily 最晚日]`，起日 = max(商品下限, market_daily 最早日)。
  商品下限：微台 2024-07-29；小台 = 今天往前 3 年（法人端點的近三年；若仍回「日期時間錯誤」就印出錯誤、該商品不補，
  由使用者改用較晚起日重跑——不自動往後試）。
- 選用參數 `--start YYYY-MM-DD`：覆蓋起日（仍取 max(商品下限, --start)），給上面重跑用。（我的判斷）
- 法人：每個商品 1 次請求整段。行情：每個商品按月切段（每段不超過一個月）逐段請求；小台約 36 次、微台約 28 次。
  某段失敗印出該段、繼續下一段；結尾列出失敗段落。
- **只 UPDATE `market_daily` 已存在的列、不 INSERT**（重用 `_update_market_columns_batch`）。新值 None 保留舊值；
  非 None 直接覆寫（歷史值是最終值，覆寫 daily 寫過的同值無害）。
- 兩邊日期對不上的列出來（同 `--backfill-futures-oi`）：DB 有、期交所沒有；期交所有、DB 沒有（不新增列）。

### Schema

- `db.py`：`ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS <6 欄> BIGINT`，加註解說明單位與「法人 = 三大法人合計」。
- `db_utils.upsert_market_daily` 的欄位清單加這 6 欄。

### 程式碼形狀（我的判斷，可改）

以下各點與「6 欄命名」「法人不拆身份別」「大台另外打、不共用請求」都是我的判斷，使用者可改。

- 法人抓取：抽出共用的 `fetch_taifex_fut_institutional(session, start, end, commodity_id)`；
  `fetch_taifex_txf_institutional` 簽名不變，改呼叫它並傳 `"TXF"`（同一段邏輯，重用而非複製）。
- 行情抓取：新 `fetch_taifex_fut_daily_report(session, commodity_id, start, end) -> DataFrame`，加
  `@_retry_on_transient`；HTML alert → `DataUnavailableError`（alert 判斷與法人那支共用）。區間切段是呼叫端的事。
- 解析：兩個新 prepare 函式，都按表頭名稱取欄、表頭／商品名稱／身份別／日期認不得就丟 `DataUnavailableError`
  （同 `prepare_taifex_txf_oi` 的風格）：
  - 法人：`{date: {f"{prefix}_inst_long_oi", f"{prefix}_inst_short_oi"}}`，商品名稱與 prefix 由呼叫端給。
  - 行情：`{date: {f"{prefix}_market_oi"}}`，只回有至少一列有效未沖銷契約數的日期；只有表頭 → 空 dict。
- 與 `prepare_taifex_txf_oi` 結構相似但取的欄與語意不同（多／空分開 vs 淨額），依 CLAUDE.md 不強行合併。

### 文件

- `README.md`：`market_daily` 欄位說明加 6 欄與散戶多空比公式（註明由下游算）、script 用法。
- `CLAUDE.md`：Commands 加 script；資料表段落補上這 6 欄；Gotchas 加「`futDataDown` 區間上限一個月、
  休市回只有表頭、script 只 UPDATE 不 INSERT」。

## 驗證

1. 單元測試：`pytest tests/unit/` 全綠（含既有大台測試不變）。
2. DEV：先跑 daily 路徑（`--date` 最近一個交易日）確認 6 欄寫入；再跑 script 模擬 → `--apply`。
3. **與玩股網比對**：從 DB 用上面的公式算比例，跟玩股網「小台指散戶多空比」
   （`https://www.wantgoo.com/futures/retail-indicator/wmt&`）與「微台指散戶多空比」（`…/wtm&`）比對。
   - 頁面是 JS 載入（curl 只拿到空殼），用瀏覽器讀頁面上的數值；只讀少量日期，不大量抓取。
   - 抽樣：最近 5 個交易日＋區間內幾個較早日期（含小台有週契約的日子、微台上市初期）。
   - 判準：四捨五入到 0.01% 一致。不一致時列出差異與可能原因（例如玩股網排除週契約、分母不同），
     **回報使用者，不自行改定義**。
4. PROD：只在使用者核准後（建欄、回補、比對同 DEV）。

## 風險與未定

- 玩股網的歷史表可能只顯示近期日期或要登入；拿不到的日期就只比能看到的。
- 法人「小型臺指期貨」是否包含週契約：假設包含（與全市場加總一致），由玩股網比對驗證。
- 一般回補（`--backfill-start/end`）每天多 4 次期交所請求；期交所限流行為未知，回補很長區間時可能變慢。

## 留給實作者

函式內部寫法、測試檔切分、log 文字、script 的輸出格式。
