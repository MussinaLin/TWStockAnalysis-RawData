# market_daily 新增台指 VIX 與大盤當沖統計 — 設計

日期：2026-11-23
狀態：待使用者確認

## 目的

在 `market_daily` 加入兩個「大盤見頂」參考指標，供下游 `TWStockAnalysis` 判讀：

- **台指 VIX**（臺指選擇權波動率指數，期交所編製）每日收盤值。
- **大盤（上市）當沖統計**：當沖成交股數、買進／賣出成交金額，以及三者占市場比重。

這次只做 **daily run**，加上用期交所近 4 個月檔案回補 VIX 的指令。更早的 VIX 之後
另寫一次性 script 從其他網站回補，不在本設計範圍。

## 目標 / 非目標 / 反目標

目標：
- 每日抓取寫入 D 日的 VIX 收盤值與當沖統計，失敗時只讓這幾欄留 NULL 並印 log。
- 當沖統計最終以 TWSE 的 T+2 修正值為準（見「當沖校正」）。
- `--backfill-vix` 補齊期交所還提供的近 4 個月 VIX。

非目標：
- 個股當沖（TWTB4U 同一份回應有個股明細，這次不存）。
- 上櫃當沖（櫃買中心另一個來源；`market_daily` 目前只收上市數字）。
- 當沖歷史回補（TWSE 2014-01-06 起都有，之後要再加指令）。
- 2026-08 以前的 VIX（之後的一次性 script 處理）。

反目標（技術上能動但算失敗）：
- 新來源失敗讓 `market_daily` 其他欄位或個股資料寫不進去。
- 回補／校正 INSERT 出只有 VIX 或當沖欄、沒有大盤行情的列（會被
  `find_consensus_prev_trade_date` 當成 D-1，打亂融資融券校正）。
- 把 TWSE 限流的空殼回應當成有效資料寫進去。

## 資料來源（2026-11-23 實測）

### 台指 VIX（期交所，`www.taifex.com.tw`，不受 TWSE pacing 影響）

| 用途 | 端點 | 格式 |
|---|---|---|
| daily | `GET /cht/7/getVixData?filesname=YYYYMMDD` | 當日逐 15 秒 tick |
| 回補 | `GET /file/taifex/Dailydownload/vix/log2data/YYYYMMnew.txt` | 當月每日收盤 |

兩者都是 cp950、tab 分隔、前兩行是表頭與 `----` 分隔線。

- 當日檔：`交易日期  時間(時/分/秒/毫秒)  臺指選擇權波動率指數`，最後一行是
  `YYYYMMDD  Last 1 min AVG  值`。15:29 已有當日 `13450000` 那一列。
- 月檔：`交易日期  時間  臺指選擇權波動率指數  收盤前1分鐘平均指數`，每天一列，
  時間欄固定 `13450000`。**當天收盤後數小時內還沒有當日列**（15:29 實測沒有
  11/23），所以 daily 不能用月檔。
- 兩者的「收盤」一致：月檔 11/20 = 52.13 = 當日檔 11/20 的 `13450000` 那一列。
- 月檔只保留近 4 個月（目前 2026-08 ～ 2026-11）；更早的月份回 302，跟隨後是 HTML。
- 期交所 OpenAPI 沒有 VIX；更早的歷史要走「交易歷史資料申請」。

### 大盤當沖（TWSE `TWTB4U`，`www.twse.com.tw`，受 pacing）

`GET https://www.twse.com.tw/rwd/zh/dayTrading/TWTB4U?date=YYYYMMDD&selectType=All&response=json`

回應 `{stat, date, tables, selectType}`，`tables[0]` 標題為
`115年11月20日 當日沖銷交易統計資訊`，欄位固定 6 個、1 列：

| 欄位 | 11/20 實測 |
|---|---|
| 當日沖銷交易總成交股數 | 4,460,672,000 |
| 當日沖銷交易總成交股數占市場比重% | 26.1 |
| 當日沖銷交易總買進成交金額 | 322,561,954,550 |
| 當日沖銷交易總買進成交金額占市場比重% | 34.48 |
| 當日沖銷交易總賣出成交金額 | 322,720,245,100 |
| 當日沖銷交易總賣出成交金額占市場比重% | 34.50 |

TWSE 附註：
- 股數占比 = 當沖成交股數 ÷ 整體市場成交股數；金額占比 = 當沖買進（賣出）金額 ÷
  （整體市場買進＋賣出金額）÷ 2。分母含一般、盤後定價、零股、鉅額交易。
- T 日晚間公布初版，T+1、T+2 晚間各更新一次，**以 T+2 為最終值**。
- `tables[1]` 是個股明細（約 1,250 列），本設計不用。

## 做法（Approaches）

1. **併入現有大盤抓取流程（採用）**：兩個新子來源加進 `_fetch_and_upsert_market_daily`，
   跟台指期未平倉一樣各自 try/except 並印 log；當沖另加 daily 收尾校正；VIX 另加
   `--backfill-vix`。
2. 做成獨立指令另外排程：多一個要排程、要監控的 job，又同樣寫 `market_daily`，
   跟現有流程的校正邏輯互相干擾。不採用。
3. 用個股明細自己加總當沖比率：TWSE 的分母含盤後定價、零股、鉅額交易，自己算對不上
   官方數字。不採用。

使用者選 1。

## 設計

### 新欄位（`market_daily`）

| 欄位 | 型別 | 單位／說明 |
|---|---|---|
| `tw_vix_close` | NUMERIC(8,2) | 台指 VIX 13:45 收盤值 |
| `day_trade_shares` | BIGINT | 當沖成交股數（股） |
| `day_trade_shares_pct` | NUMERIC(6,4) | 股數占市場比重（小數，26.1% → 0.2610） |
| `day_trade_buy_amount` | BIGINT | 當沖買進成交金額（元） |
| `day_trade_buy_amount_pct` | NUMERIC(6,4) | 買進金額占市場比重（小數） |
| `day_trade_sell_amount` | BIGINT | 當沖賣出成交金額（元） |
| `day_trade_sell_amount_pct` | NUMERIC(6,4) | 賣出金額占市場比重（小數） |

- 占比存小數，跟 `stock_daily_raw.foreign_holding_pct` 的慣例一致。
- 6 個當沖欄照 TWSE 原樣存，不自己挑「哪個才是當沖率」，由下游決定。新聞常說的
  「當沖占比」是金額占比。
- `db.py`：`CREATE TABLE` 加欄，並加 `ALTER TABLE market_daily ADD COLUMN IF NOT EXISTS`
  線上 migration（同 `txf_*` 的做法）。

### sources.py

- `fetch_taifex_vix_close(session, date) -> float`：抓當日檔，取 `交易日期 == date`
  且時間 `== 13450000` 的那一列。回應是 HTML（`<` 開頭）、表頭不符、找不到該列、
  數值解析不出來，一律 `DataUnavailableError`（訊息說明原因）。**不**退而取最後一筆
  tick：檔案不完整時（盤中重跑）最後一筆不是收盤，寫下去是看不出來的錯值。
- `fetch_taifex_vix_month(session, month) -> dict[date, float]`：抓月檔，回傳該月每日收盤
  （第 3 欄）。HTML／表頭不符 → `DataUnavailableError`，訊息註明「期交所只提供近 4 個月」。
- `fetch_twse_day_trade(session, date) -> dict`：
  - `stat` 必須是 `"OK"`（TWSE 嚴格判準），否則 `DataUnavailableError`，訊息帶 `stat`
    原文（限流字串看得出來）。
  - `payload["date"]` 必須等於請求日，否則棄用（TWSE 日期驗證政策）。
  - 找標題含 `當日沖銷交易統計資訊` 的表；欄位名稱必須與上表 6 個完全相同、恰好 1 列，
    否則 `DataUnavailableError`（表頭改版）。
  - 回傳 6 個欄位的 dict；股數／金額用 `_clean_int`，占比 `_clean_number ÷ 100`。
    任一值解析不出來時該欄為 None（不整筆丟棄）。
- 三者都套 `_retry_on_transient`（與其他每日整批 fetcher 同一個長窗口 profile）。
- VIX 兩支走期交所，維持預設 TLS 驗證（同 `fetch_taifex_txf_institutional`），不經
  `_http_get`。TWTB4U 走 `_http_get`。

### db_utils.py

- **先做一個獨立的 refactor commit**：把 `update_market_futures_oi_batch` 的本體抽成
  `_update_market_columns_batch(database_url, col_types, updates)`（`col_types` 為
  欄名 → PG 型別，用於 VALUES 的 cast），`update_market_futures_oi_batch` 改成呼叫它，
  簽名與行為不變。理由：期貨未平倉、VIX 回補、當沖校正三處是同一段「只 UPDATE、
  COALESCE、分批」邏輯（CLAUDE.md：重複兩次以上才抽）。
- 新增 `update_market_vix_batch` 與 `update_market_day_trade_batch`，都是上面 helper 的
  薄包裝。
- `upsert_market_daily` 的欄位清單加上 7 個新欄（沿用 COALESCE）。
- 新增 `find_prev_market_trade_date(database_url, date, n) -> date | None`：
  `market_daily` 中早於 `date` 的第 n 個 trade_date（`ORDER BY trade_date DESC OFFSET n-1`）。

### run.py

**daily 抓取（`_fetch_and_upsert_market_daily`）**：在台指期未平倉之後加兩段：

```
try: market_data["tw_vix_close"] = fetch_taifex_vix_close(session, date)
except (DataUnavailableError, RequestException) as exc: print(f"  台指 VIX 取得失敗：{exc}")

try: market_data.update(fetch_twse_day_trade(session, date))
except (...) as exc: print(f"  大盤當沖統計取得失敗：{exc}")
```

這個函式在 daily、`--date`、一般區間回補都會被呼叫，所以三條路徑自動都會寫這 7 欄。
`--backfill-stocks` 不寫 `market_daily`，不受影響。

**當沖校正（`_settle_market_day_trade`）**：
- 只在 daily（`target_date == today` 且當天有寫入）時呼叫一次，放在 `main()` 單日分支、
  `_settle_market_margin` 旁邊。
- 校正對象：`find_prev_market_trade_date(db, today, 3)`，也就是 DB 裡往前第 3 個交易日。
  重抓 TWTB4U，用 `update_market_day_trade_batch` 只 UPDATE 那一列。
- **為什麼是第 3 個、不是第 2 個**：TWSE 的 T+2 更新是 T+2 晚間，和我們 22:00 的
  daily 幾乎同時，不保證已更新；選第 3 個交易日保證拿到最終值，請求數一樣是 1 次。
  代價只是最終值晚一天到位。
- 同時當作備援：D 日當晚 TWSE 若還沒公布（附註說成交量大時會延後），D 那幾欄先是 NULL，
  3 個交易日後由校正補上。
- 略過條件：DB 往前不足 3 個交易日 → 印 log 略過。任何失敗只印 log，不讓 daily 以
  traceback 結束（同 `_settle_market_margin`）。
- `--date` 重跑與區間回補**不**做這個校正：舊日期抓到的本來就是最終值；最近 3 天內的
  日期會由之後的 daily 校正到。每天多打的 TWSE 請求固定 1 次（D 初版 + 第 3 個交易日
  校正 = 2 次），回補 N 天不會多 N 次。
- 已知缺口：某天 daily 沒跑，那一天對應的校正也就沒做；該日期停在 T+1 或 T+2 的值。
  影響是小幅修正值，接受。

**`--backfill-vix`**（與 `--backfill-futures-oi` 同一個模式）：
- 需 `--backfill-start` / `--backfill-end`；`--backfill-stocks`、`--date` 會被忽略並印警告。
- 範圍內 `market_daily` 已有的日期（`load_market_dates_in_range`）→ 涵蓋的月份 → 每月抓
  一次月檔。某月抓不到（太舊或其他錯誤）就印警告、繼續下一個月。
- 只 UPDATE 已存在的列（`update_market_vix_batch`），不 INSERT。
- 收尾列出：DB 有、期交所沒有的日期；期交所有、DB 沒有的日期（不新增列）。
- 在 `main()` 的分派位置與 `--backfill-futures-oi` 並列（在載入個股之前就 return）。

### 文件

- README：CLI 範例加 `--backfill-vix`；新增「台指 VIX 與大盤當沖（market_daily）」一節，
  寫欄位語意、來源、VIX 只有近 4 個月、當沖第 3 個交易日校正；「來源失敗一律印 log」
  的子來源清單從 5 個改成 7 個。
- CLAUDE.md：Commands 加 `--backfill-vix`；Gotchas 加「當沖校正只在 daily 做、對象是往前
  第 3 個交易日」與「`--backfill-vix` 只 UPDATE 不 INSERT」；資料表段落補一句
  `market_daily` 含 VIX 與當沖。

### 測試（TDD，先紅後綠）

- sources：TWTB4U 正常解析（以 11/20 實測值做 fixture）、`stat` 非 OK、日期不符、表頭改版、
  數值缺；VIX 當日檔正常、缺 13:45 列、HTML 回應；VIX 月檔正常、HTML 回應。
- db_utils：`update_market_futures_oi_batch` 既有測試在 refactor 後全綠；新 batch 函式的
  SQL 形狀與 cast；`upsert_market_daily` 含新欄；`find_prev_market_trade_date`。
- run：`_fetch_and_upsert_market_daily` 寫入新欄、各自失敗只印 log 不影響其他欄；
  `_settle_market_day_trade` 選對日期、DB 不足略過、失敗不拋；`main()` 只在 daily 呼叫校正；
  `--backfill-vix` 只更新 DB 已有日期、某月失敗不中斷、印出兩邊差異。
- db schema：新欄出現在 DDL。

### Commit 與 branch

branch `feat/market-vix-daytrade`（從 develop 開）。依序：
1. `docs`：本設計文件。
2. `refactor(db_utils)`：抽出 `_update_market_columns_batch`（行為不變）。
3. `feat(market_daily)`：台指 VIX（daily + `--backfill-vix`）＋ README／CLAUDE.md。
4. `feat(market_daily)`：大盤當沖統計（daily + 校正）＋ README／CLAUDE.md。

每個 commit pytest 全綠；commit message 不加 `Co-Authored-By`。

## 留給實作者決定

- 測試 fixture 的組織方式（沿用既有 `test_market_daily_fetchers.py` / `test_market_daily.py` /
  `test_backfill_futures_oi.py` 的寫法即可）。
- log 訊息的確切措辭（需包含來源名稱與原因）。
- 上線後第一週觀察：22:06 的 daily 是否穩定拿得到 D 日 TWTB4U 與 VIX 當日檔。若常拿不到，
  再討論調整排程時間，不在本次範圍。
