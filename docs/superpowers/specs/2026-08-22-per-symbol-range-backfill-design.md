# `--backfill-stocks` 改為 per-stock 區間抓取 — 設計

日期：2026-08-22
狀態：已與使用者確認設計

## 目的

`--backfill-stocks`（回補特定股票的一段歷史）目前的請求數與「回補幾檔」無關，只與
**天數**成正比：`_run_for_date` 每個交易日固定打 4 發全市場批次。回補 1 檔 3 年約
**2,900 次請求**，其中 1,460 次打 `www.twse.com.tw`，光 `_MinIntervalAdapter` 的 1.0 秒
pacing 就要 24 分鐘，且高頻請求會踩到 TWSE IP 限流（見
`memory/twse-rate-limit-ambiguous-response.md`）。下游 `TWStockAnalysis` 的
`scripts/add_stock/01_backfill_raw.sh` 因此必須切季度分段、段間 `sleep 60`。

改成「per-stock 區間抓」後，同樣的回補約 **40 次請求**（約 1/70），可移除分段與 sleep。

## 現況：每個交易日的 4 發請求

| 來源 | 端點 | 次數/日 |
|---|---|---|
| TWSE 三大法人 | `fund/T86` | 1 |
| TWSE 全市場行情 | `MI_INDEX` | 1 |
| TPEX 日行情 | `afterTrading/dailyQuotes` | 1 |
| TPEX 三大法人 | `insti/dailyTrade` | 1 |

融資融券（MoneyDJ `zcn`）、外資/法人持股佔比（MoneyDJ `zcl`）、處置股名單
（TWSE `punish` / TPEX `disposal`）**已經**是整段預取一次，不是問題。

## 改造後：per-stock 區間來源（回補 1 檔 × 3 年）

| 資料 | 來源 | 請求數 |
|---|---|---|
| OHLCV + change（上市）| TWSE `STOCK_DAY` 月表 | 36（1/月）|
| OHLCV + change（上櫃）| TPEX `afterTrading/tradingStock` 月表 | 36（1/月）|
| 三大法人 | MoneyDJ `zcl`（**與持股佔比同一發**）| 0（併入下列）|
| 持股佔比 | MoneyDJ `zcl` | 1 |
| 融資融券 | MoneyDJ `zcn` | 1 |
| 處置股 | TWSE `punish` + TPEX `disposal` | 2 |

單檔合計 **40 次**（該檔只屬於一個市場，OHLCV 只會走上表其中一列）。

## 驗證結果（2026-08-22 實測）

### TPEX 個股日成交資訊

```
https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code=6488&date=2025/07/01
```

欄位：`日 期 / 成交張數 / 成交仟元 / 開盤 / 最高 / 最低 / 收盤 / 漲跌 / 筆數`

**三個必須守住的不變量：**

1. `date` 參數是**西元** `yyyy/MM/dd`。repo 其他 TPEX v2 端點（`dailyQuotes`、
   `insti/dailyTrade`、`margin/balance`、`bulletin/disposal`）用的是**民國**
   `_date_to_roc()`。這一支相反。日後若有人為了一致性把它改成 `_date_to_roc`，
   端點會回「參數輸入錯誤」。實測民國 `114/07`、`114/07/01`、`1140701` 全數失敗。
2. **不可帶 `response=json`**。這端點本來就直接回 JSON，帶了會回
   `{"stat":"參數輸入錯誤"}`。
3. **參數名打錯不會報錯，會靜默 fallback 回「當月」**。實測 `d=114/07`、
   `ym=202507`、`yearMonth=202507`、`year=2025&month=07` 全部被忽略、回傳當月資料且
   `stat=ok`。因此**必須驗證回應的 `payload["date"]` 等於請求月份**，否則整段歷史
   回補會被寫入當月數字，而且完全無聲無息。

### MoneyDJ `zcl.djhtm` 同時含三大法人買賣超

repo 目前為了 `holding_pct` **已經在打這一頁**（`fetch_moneydj_holding_pct`），
只解析了 col 0 / 9 / 10。實際欄位對位：

| col | 內容 |
|---|---|
| 0 | 日期（民國）|
| 1–4 | **買賣超**：外資 / 投信 / 自營商 / 單日合計 |
| 5–8 | 估計持股：外資 / 投信 / 自營商 / 單日合計 |
| 9–10 | 持股比重：外資 / 三大法人 |

**三大法人不需要任何額外 HTTP 請求。**

對照 TWSE `T86`（2330 / 2025-07-31），定義完全吻合：MoneyDJ 的「外資」對應 T86
「外陸資買賣超股數(不含外資自營商)」。當日全市場 14,186 檔的「外資自營商」欄**全為 0**，
兩種定義實務上等價。

頁面上的「(自設區間僅提供一年內查詢)」是 UI 下拉選單的說明，**不是 API 限制**：
實測 `c=2023-1-1&d=2025-12-31` 一發回傳 724 列，無截斷。

### 精度影響

`stock_daily_raw` 存的**本來就是「張」**（`_build_daily_rows` 寫入前一律 `// 1000`）。
所以差異只是 **floor（現行）vs 四捨五入（MoneyDJ / TPEX 月表）**：

| 欄位 | T86 股數 | 現行 `// 1000` | 新來源（張）| 差 |
|---|---|---|---|---|
| 外資 | 9,039,647 | 9039 | 9040 | **+1** |
| 投信 | −1,292,952 | −1293 | −1293 | 0 |
| 自營商 | 1,611,836 | 1611 | 1612 | **+1** |
| TPEX volume | 3,709,228 | 3709 | 3709 | **0** |

TPEX 成交量**零損失**。唯一殘留影響是 `turnover_rate`（用 `// 1000` 前的原始股數計算）：
上櫃股相對誤差約 0.006%；上市股走 `STOCK_DAY` 拿到精確成交股數，無誤差。

**已與使用者確認：接受此誤差，全面採用 per-stock 來源。**

### change 在除權息日

| 來源 | 除權息日回傳 | 結果 |
|---|---|---|
| `STOCK_DAY_ALL` | `0.0000`（**無任何標記**）| 危險，CLAUDE.md 已禁用 |
| `MI_INDEX` | `NaN` | change NULL → 漲跌停 NULL |
| `STOCK_DAY` 月表 | `X0.00`（顯式標記）| `_clean_number` fail-safe 成 `None` → 與 MI_INDEX 同結果，**無退步** |
| `dailyQuotes`（現行上櫃）| 文字 `"除息 "` | change NULL → 漲跌停 NULL |
| `tradingStock` 月表（新，上櫃）| **`18.50`**（正確的除息參考價漲跌）| **漲跌停算得出來** |

實例：6488 環球晶 2025-07-16 除息 6 元，收盤 322.50，前日收盤 310.00。
月表回 `18.50` = `322.50 − (310.00 − 6.00)`，即相對除息參考價 304.00 的正確漲跌。

**已與使用者確認：接受 backfill 與 daily 不一致，backfill 寫正確值。**
搭配 upsert 的 `COALESCE`（daily 寫 NULL 不覆寫舊值），實際效果是「回補過的日子資料更完整」。
本次**不動** daily 流程。

## 架構：Source Provider 介面

### 為何不用另外兩個方案

- **合成批次表**（per-stock 抓完後偽裝成每日 batch DataFrame 餵回 `_run_for_date`）：
  `_stock_sources_ok` 的 `is_tpex` 是靠「symbol 有沒有出現在當日 `tpex_quotes`」判定的。
  用合成資料撐住這個訊號，正是 CLAUDE.md 反覆警告「兩個獨立訊號不要互相取代」的地雷區。
- **獨立 per-stock 編排函式**（新寫 `_run_backfill_stocks()` 自己組列）：會複製一份組列
  邏輯。CLAUDE.md 的不變量（COALESCE、處置 `0` 哨兵值、change 來源規則、跳過半套資料）
  全都埋在組列裡，雙軌必然漂移。

### 介面

```python
class RowSourceProvider(Protocol):
    def ohlcv(self, symbol: str, date: dt.date,
              market_type: str | None) -> OhlcvResult: ...
    def insti(self, symbol: str, date: dt.date
              ) -> tuple[int | None, int | None, int | None]: ...
    def insti_ok(self, symbol: str, market_type: str | None) -> bool: ...
    def is_tpex(self, symbol: str, market_type: str | None) -> bool: ...
```

**`insti()` 與 `ohlcv().volume` 的契約單位一律是「股」。** batch provider 回交易所的精確
股數；per-symbol provider 回「張 × 1000」。`_build_daily_rows` 尾端的 `// 1000` 完全不用
改，也不需要任何 per-mode 特例分支——精度差異就是來源本質。

### `BatchSourceProvider`

把現行 `_fetch_ohlcv_with_fallback` / `_get_institutional_data` /
`symbol in tpex_symbols` 原樣搬入，持有 `twse_day_all` / `twse_mi_index` /
`tpex_quotes` / `twse_3insti` / `tpex_3insti` / `twse_month_cache` /
`twse_insti_ok` / `tpex_insti_ok`。**零行為變更。**

### `PerSymbolRangeProvider`

建構時完成全部預取：

- **OHLCV**：依 `market_type` 選 TWSE `STOCK_DAY` 或 TPEX `tradingStock`，逐月抓，
  展開成 `{symbol: {date: OhlcvResult}}`。
- **insti**：MoneyDJ `zcl` 一發整段，展開成 `{symbol: {date: (foreign, trust, dealer)}}`。
- **`is_tpex`**：直接回 `market_type == "tpex"`。per-stock 模式下市場別是已知事實，
  不需要「靠當日 tpex_quotes 推市場別」這個 workaround。
- **`insti_ok`**：該檔的 MoneyDJ 取得是否成功。整段取得失敗時該檔**每一天**都不通過
  `_stock_sources_ok`，等同整檔跳過不寫——這與現行「逐檔跳過半套資料」的不變量一致，
  留待重跑補上。

## 交易日判定與限流防護

per-stock 模式沒有 `MI_INDEX` / `T86` 可以做現行的 `twse_confirmed` 判斷。改為：

**月表裡有這一列 = 該檔該日有交易**；沒有這一列就不寫該檔該日（未上市 / 停牌 / 無成交）。

並加一道現行設計沒有的安全網，正面回應
`memory/twse-rate-limit-ambiguous-response.md` 記錄的已知風險：

> 月表整月回空時，**與 MoneyDJ `zcl` 交叉比對**。若 MoneyDJ 在該月有列（證明該檔那個月
> 確實有交易），而交易所月表回空，判定為**限流 / 取得失敗**——該月**不寫**並印出明顯
> 警告，而不是靜默當成「沒交易」。

這是目前唯一能區分「限流」與「真的沒資料」的訊號，因為兩者的 HTTP 回應完全相同。

## 編排（`main()` 的 `--backfill-stocks` 分支）

維持不變的部分：`--force`、處置名單整段預取、margin / holding_pct 預取、
`write_market_daily=False`（不動共用大盤表）、`_refresh_prev_day_margin`。

改變的部分：把「逐日打 4 發批次」換成「建 `PerSymbolRangeProvider` 一次預取完」，
再逐日呼叫 `_build_daily_rows(provider=...)` → `upsert_daily_raw`。

`stocks.market_type` 查不到時（`--backfill-stocks` 直接給代號，DB 可能沒有該檔）：
拿區間第一個月**先試 TWSE `STOCK_DAY`，回空再試 TPEX `tradingStock`**，以先取得資料者
定調市場別，之後整段沿用。每檔最多多 1 發。兩邊都回空則該檔整段跳過並警告。

## 測試

新增：

- `fetch_tpex_stock_day` 參數格式（西元日期、無 `response=json`）
- **回傳月份與請求月份不吻合時拋 `DataUnavailableError`**（擋靜默 fallback 回當月）
- `prepare_moneydj_insti` 欄位對位與「張 → 股」換算
- `STOCK_DAY` 的 `X0.00` → change `None`
- 限流交叉比對：月表整月空 + MoneyDJ 該月有列 → 不寫該月且警告
- per-stock 與 batch 對同一天同一檔產出一致（三大法人容許 ±1 張）

既有測試改走 provider 後**斷言不變**：`test_run_ohlcv_source_order.py`（8 tests）、
`test_run_stock_sources.py`、`test_daily_rows_limit_seam.py`。

## 已知取捨

1. 三大法人 floor → round，最多差 1 張。已確認接受。
2. 上櫃 `turnover_rate` 相對誤差約 0.006%（成交量僅到張）。上市無誤差。
3. 上櫃除權息日的 `change` / 漲跌停：backfill 有值、daily 為 NULL。已確認接受，
   本次不動 daily。
4. TPEX 月表參數靜默 fallback 回當月——靠強制月份驗證擋住，這是本設計最容易
   出事的地方。

## 下游影響

`TWStockAnalysis` 的 `scripts/add_stock/01_backfill_raw.sh` 的季度分段與段間
`sleep 60` 在改造後不再必要，可簡化為單次呼叫。該檔案屬於下游 repo，**不在本次改動範圍**，
但本 repo 的 `README.md` 需同步更新 `--backfill-stocks` 的行為說明與請求數特性。
