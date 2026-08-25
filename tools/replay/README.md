# 錄製／重放比對工具

重構前後「同樣的上游回應，兩個版本產出是否相同」的驗證工具。

單元測試證明不了這件事：它們餵的是合成輸入。這套工具用**真實上游回應**——
錄一次、離線重放給兩個版本、逐欄比對。

## 為什麼是錄製／重放，不是各跑一次

直接讓兩個版本各自打 API 有兩個致命問題：

1. **請求量翻倍**。歷史日單日就要 442 次（216 檔 × MoneyDJ 融資融券 + 持股佔比），
   20 天兩個版本 = 17,680 次。
2. **上游不穩定會被誤判成程式差異**。`www.twse.com.tw` 限流時回 HTTP 200 +
   「很抱歉，沒有符合條件的資料!」，與「該日真的沒資料」是同一個字串
   （見 `memory/twse-rate-limit-ambiguous-response.md`）。兩邊拿到不同回應，
   比對結果就沒有意義。

錄一次、重放兩次，網路只打 1×，且重放完全離線、可無限次重跑。

## 用法

```bash
# 0. 準備基準版（重構前的 commit）
git worktree add --detach /tmp/base-tree <重構前的 commit>

# 1. 用基準版錄製（真的打 API，TWSE 部分自動 ≥1s 間隔）
python tools/replay/capture.py --src /tmp/base-tree/src --mode daily \
    --record --tape /tmp/tape.json --out /tmp/base.json --today 2026-08-25

# 2. 離線重放給兩個版本
python tools/replay/capture.py --src /tmp/base-tree/src --mode daily \
    --tape /tmp/tape.json --out /tmp/base.json --today 2026-08-25
python tools/replay/capture.py --src ./src --mode daily \
    --tape /tmp/tape.json --out /tmp/dev.json --today 2026-08-25

# 3. 比對（退出碼 0 = 完全相同）
python tools/replay/compare.py /tmp/base.json /tmp/dev.json base dev
```

三種模式各自涵蓋不同路徑，**要三種都跑才算全覆蓋**：

| `--mode` | 涵蓋 | 參考請求數 |
|---|---|---|
| `daily` | daily / 一般區間回補（`BatchSourceProvider`、`_fetch_ohlcv_with_fallback`） | 8 檔 × 20 天 ≈ 456 |
| `dahu` | `--dahu`（TDCC token 鏈、逐檔分散表） | 8 檔 × 8 週 ≈ 65 |
| `backfill-stocks` | `--backfill-stocks`（`PerSymbolRangeProvider`、逐檔月表） | 8 檔 × 3 月 ≈ 42 |

`daily` 模式的日期清單在 `days.json`，標的清單在 `stocks.json`（8 檔，涵蓋
上市／上櫃、高中低價 tick 級距，`6488` 在 2025-07-16 有除息，會踩到
change / 漲跌停那條路徑）。

## 比對什麼

側錄每次 `fetch_*` / `prepare_*` 的**呼叫順序、傳入參數、完整回傳值**，
外加所有 DB 寫入內容。所以下列任一改變都看得出來：呼叫次數不同、順序不同、
參數不同、抓回或 normalize 出來的資料不同、要寫進 DB 的內容不同。

**tape miss 本身就是差異訊號**：重放時某個版本要了 tape 裡沒有的請求，
代表兩邊送出的請求集合不同。`compare.py` 會單獨標出來。

## 三個踩過的坑

**基準版要選對。** 曾經拿 `main` 當基準，結果它落後 60 個 commit、缺整批功能
（處置股、`PerSymbolRangeProvider`），比出來的差異全是功能差異而非重構差異。
正確做法是選「重構開始前的最後一個 commit」，並先確認兩邊的功能面一致
（例如 `grep -l disposition src/*.py` 兩邊筆數相同）。

**DataFrame 不能用 `to_dict("records")` 序列化。** 它對重名欄位會**靜默丟掉**
其中幾欄——TPEX 三大法人的表就是重名欄（每個法人類別都有買進/賣出/買賣超），
那會讓比對對那些欄位完全盲掉卻不報錯。`serialize.py` 改用位置序列化。

**Decimal 不要轉 float。** 漲跌停價的精度就是重點，轉 float 會把
`2415.50` 與 `2415.5000` 的差異抹掉。序列化成 `"D:2415.50"` 字串。

## tape 不進版控

錄好的 tape 是 100MB 量級（每筆都存完整回應本文），且上游資料會過期，
所以 `.gitignore` 掉。要用就重錄一次——三種模式合計約 560 次請求、20 分鐘。

不能靠「有 json 就丟掉 text」瘦身：`fetch_twse_stock_day` 會在 `json()` 之前
先檢查 `response.text` 是否空白（限流時 TWSE 回 200 + 空 body），丟掉 text
會讓那條防線在重放時失效。
