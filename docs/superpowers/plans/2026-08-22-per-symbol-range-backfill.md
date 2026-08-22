# `--backfill-stocks` 改 per-stock 區間抓取 — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `--backfill-stocks` 從「每個交易日打 4 發全市場批次」改成「每檔逐月／整段抓」，回補 1 檔 3 年的請求數從 ~2,900 降到 ~40。

**Architecture:** 抽出 `RowSourceProvider` 介面，`_build_daily_rows` 改成跟 provider 要 OHLCV 與三大法人，而不是直接吃全市場 DataFrame。`BatchSourceProvider` 包住現行邏輯（daily / 全市場 backfill，零行為變更）；新的 `PerSymbolRangeProvider` 用 TWSE `STOCK_DAY` / TPEX `tradingStock` 月表 + MoneyDJ `zcl` 供應同樣的資料。單一組列路徑，不產生雙軌。

**Tech Stack:** Python ≥ 3.13、pandas、requests、psycopg、pytest

**Spec:** `docs/superpowers/specs/2026-08-22-per-symbol-range-backfill-design.md`

## Global Constraints

- Python ≥ 3.13。所有新函式加型別註記，模組頂端已有 `from __future__ import annotations`。
- 註解與 docstring 用**繁體中文**，與既有程式碼一致。
- **commit message 不可加任何 `Co-Authored-By:` trailer。**
- `provider.insti()` 與 `provider.ohlcv().volume` 的契約單位一律是**股**。per-symbol 來源拿到的是「張」，一律在 provider 邊界內 `× 1000` 還原成股。`_build_daily_rows` 尾端的 `// 1000` **不得修改**。
- `upsert_daily_raw` / `upsert_market_daily` 的 `ON CONFLICT` 一律維持 `col = COALESCE(EXCLUDED.col, table.col)`，**本計畫完全不動 `db_utils.py`**。
- `--backfill-stocks` 維持 `write_market_daily=False`，不動 `market_daily`。
- 處置股欄位維持三態語意（TRUE / FALSE / NULL）與非處置日寫 `0` 的哨兵值，**本計畫不動處置邏輯**。
- 每個 task 結束前跑 `pytest tests/unit/ -q` 必須全綠才 commit。
- 完成後 `README.md` 必須同步更新（Task 7）。

---

### Task 1: TPEX 個股月表 fetcher

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（URL 常數區約 `:49`；新函式放在 `fetch_tpex_3insti_v2` 之後，約 `:678`）
- Test: `tests/unit/test_tpex_stock_day.py`（新檔）

**Interfaces:**
- Consumes: 既有 `_retry_on_transient`、`PER_SYMBOL_RETRY_ATTEMPTS`、`PER_SYMBOL_RETRY_MAX_DELAY`、`_extract_tpex_v2_table`、`DataUnavailableError`
- Produces: `fetch_tpex_stock_day(session: requests.Session, stock_no: str, date: dt.date) -> pd.DataFrame` — 回傳該月原始表（欄位 `日 期 / 成交張數 / 成交仟元 / 開盤 / 最高 / 最低 / 收盤 / 漲跌 / 筆數`）。該月無資料或月份不匹配時拋 `DataUnavailableError`。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_tpex_stock_day.py`：

```python
"""Unit tests: TPEX 個股日成交資訊月表（無網路）。

2026-08-22 實測到的三個坑，這裡逐一鎖住：
1. date 參數是「西元」yyyy/MM/dd，不是民國 —— 與 repo 其他 TPEX v2 端點相反。
2. 不可帶 response=json，帶了端點回「參數輸入錯誤」。
3. 參數名打錯不會報錯，會靜默 fallback 回「當月」資料且 stat=ok。
   ⇒ 必須驗證回應的 date 落在請求月份，否則歷史回補會被寫進當月數字而完全無聲。
"""

from __future__ import annotations

import datetime as dt

import pytest

from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import DataUnavailableError, fetch_tpex_stock_day


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """記錄 GET 參數並回固定 payload。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, **kwargs):  # noqa: ANN001 - 測試替身
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self._payload)


def _payload(echo_date: str, rows: list[list[str]] | None = None) -> dict:
    return {
        "tables": [{
            "title": "個股日成交資訊",
            "subtitle": "6488 環球晶 114年07月",
            "date": echo_date,
            "fields": ["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                       "收盤", "漲跌", "筆數"],
            "data": rows if rows is not None else [
                ["114/07/01", "1,985", "605,535", "301.50", "307.50",
                 "300.50", "307.50", "6.00", "2,441"],
            ],
        }],
        "date": echo_date,
        "code": "6488",
        "name": "環球晶",
        "stat": "ok",
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)


def test_date_param_is_gregorian_not_roc() -> None:
    """坑 1：民國格式會被端點拒絕，這裡鎖住送出去的是西元 yyyy/MM/01。"""
    session = _FakeSession(_payload("20250701"))
    fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    _url, params = session.calls[0]
    assert params["date"] == "2025/07/01"
    assert params["code"] == "6488"


def test_response_json_param_is_not_sent() -> None:
    """坑 2：帶了 response=json 端點會回「參數輸入錯誤」。"""
    session = _FakeSession(_payload("20250701"))
    fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    _url, params = session.calls[0]
    assert "response" not in params


def test_returns_month_table() -> None:
    session = _FakeSession(_payload("20250701"))
    df = fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))

    assert list(df.columns)[:2] == ["日 期", "成交張數"]
    assert df.iloc[0]["收盤"] == "307.50"


def test_month_mismatch_raises() -> None:
    """坑 3：請求 2025/07，端點靜默回當月（2026/08）→ 必須拋錯，不可當成資料。"""
    session = _FakeSession(_payload("20260801"))

    with pytest.raises(DataUnavailableError, match="月份不匹配"):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


def test_bad_stat_raises() -> None:
    session = _FakeSession({"stat": "參數輸入錯誤"})

    with pytest.raises(DataUnavailableError, match="參數輸入錯誤"):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


def test_empty_month_raises_data_unavailable() -> None:
    """該檔該月無資料（未上市 / 停牌）→ DataUnavailableError，呼叫端據此判斷。"""
    session = _FakeSession(_payload("20250701", rows=[]))

    with pytest.raises(DataUnavailableError):
        fetch_tpex_stock_day(session, "6488", dt.date(2025, 7, 15))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_tpex_stock_day.py -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_tpex_stock_day'`

- [ ] **Step 3: 加 URL 常數**

在 `src/tw_stock_rawdata/sources.py` 的 `TPEX_3INSTI_V2_URL` 定義之後（約 `:37`）加入：

```python
TPEX_STOCK_DAY_URL = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
)
```

- [ ] **Step 4: 實作 fetcher**

在 `fetch_tpex_3insti_v2` 之後（`_extract_tpex_v2_table` 已定義於其前）加入：

```python
@_retry_on_transient(
    attempts=PER_SYMBOL_RETRY_ATTEMPTS, max_delay=PER_SYMBOL_RETRY_MAX_DELAY
)
def fetch_tpex_stock_day(
    session: requests.Session,
    stock_no: str,
    date: dt.date,
) -> pd.DataFrame:
    """抓單檔上櫃股在 `date` 所屬整月的日成交資訊（TPEX 個股月表）。

    回傳原始 DataFrame，欄位為
    `日 期 / 成交張數 / 成交仟元 / 開盤 / 最高 / 最低 / 收盤 / 漲跌 / 筆數`。
    上市股對應的是 `fetch_twse_stock_day`（TWSE STOCK_DAY）。

    2026-08-22 實測到三個容易踩的坑，改動前必讀：

    1. `date` 參數是**西元** `yyyy/MM/dd`。repo 其他 TPEX v2 端點
       （`dailyQuotes` / `insti/dailyTrade` / `margin/balance` / `bulletin/disposal`）
       全部用 `_date_to_roc()` 的民國格式，**只有這一支相反**。若為了「一致性」
       改成民國，端點會回 `{"stat":"參數輸入錯誤"}`。實測 `114/07`、`114/07/01`、
       `1140701` 三種民國寫法全部失敗。
    2. **不可**帶 `response=json`。這端點本來就直接回 JSON，多帶這個參數同樣會回
       「參數輸入錯誤」。
    3. **參數名打錯不會報錯，會靜默 fallback 回「當月」資料且 `stat=ok`**
       （實測 `d=` / `ym=` / `yearMonth=` / `year=&month=` 全部被忽略）。所以回應的
       `date` 必須驗證落在請求月份 —— 否則整段歷史回補會被寫入當月數字，而且
       完全沒有任何錯誤訊號。這是本模組最容易靜默出錯的地方。

    該檔該月無資料時 `_extract_tpex_v2_table` 會拋 `DataUnavailableError`，
    而 `_retry_on_transient` 對它明確不重試，因此空月不會浪費請求配額。
    """
    month_start = date.replace(day=1)
    params = {
        "code": stock_no,
        # 坑 1：西元，不是 _date_to_roc()
        "date": f"{month_start.year}/{month_start.month:02d}/01",
        # 坑 2：不加 response=json
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_STOCK_DAY_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise DataUnavailableError("TPEX 個股月表回傳非 JSON")

    if payload.get("stat") not in {None, "ok", "OK"}:
        raise DataUnavailableError(payload.get("stat") or "TPEX 個股月表回傳異常")

    # 坑 3 的防線：回應的 date 是 yyyyMMdd，前 6 碼必須等於請求月份。
    echoed = str(payload.get("date") or "")
    if echoed[:6] != month_start.strftime("%Y%m"):
        raise DataUnavailableError(
            f"TPEX 個股月表月份不匹配：回傳 {echoed!r}，請求 {month_start:%Y%m}"
            "（端點對未知參數會靜默回當月，勿當成有效資料）"
        )

    return _extract_tpex_v2_table(payload, "個股日成交資訊")
```

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/unit/test_tpex_stock_day.py -v`
Expected: PASS（6 passed）

- [ ] **Step 6: 跑全部單元測試**

Run: `pytest tests/unit/ -q`
Expected: 全綠

- [ ] **Step 7: Commit**

```bash
git add src/tw_stock_rawdata/sources.py tests/unit/test_tpex_stock_day.py
git commit -m "feat: 新增 TPEX 個股月表 fetcher，強制驗證回傳月份"
```

---

### Task 2: 月表展開成逐日 OHLCV + change

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py`（放在 `find_twse_ohlcv` 之後，約 `:436`）
- Test: `tests/unit/test_month_table_expand.py`（新檔）

**Interfaces:**
- Consumes: 既有 `_roc_to_date`、`_clean_number`、`_clean_int`
- Produces:
  - `expand_twse_stock_day(df: pd.DataFrame) -> dict[dt.date, dict[str, float | int | None]]`
  - `expand_tpex_stock_day(df: pd.DataFrame) -> dict[dt.date, dict[str, float | int | None]]`

  兩者回傳同一種 dict：key 是該日日期，value 是
  `{"open":…, "high":…, "low":…, "close":…, "volume":…, "change":…}`，
  **`volume` 單位一律是「股」**（TPEX 月表的「成交張數」在此 × 1000）。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_month_table_expand.py`：

```python
"""Unit tests: 個股月表 → 逐日 OHLCV/change 展開。

兩個關鍵不變量：
1. TWSE 在除權息日的漲跌價差是 "X0.00"（顯式標記），必須展開成 change=None，
   絕不可變成 0.0 —— 那會算出「參考價 = 收盤」的錯誤漲跌停。
2. volume 契約單位是「股」。TPEX 月表給的是「成交張數」，展開時要 × 1000。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata.sources import expand_tpex_stock_day, expand_twse_stock_day


def _twse_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["114/09/15", "24,000,000", "30,000,000", "1250.00", "1260.00",
             "1245.00", "1255.00", "-5.00", "50,000", ""],
            # 除息日：漲跌價差帶 X 標記
            ["114/09/16", "30,000,000", "38,000,000", "1270.00", "1285.00",
             "1265.00", "1280.00", "X0.00", "60,000", ""],
        ],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


def _tpex_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["114/07/15", "1,234", "380,000", "310.00", "312.00",
             "308.00", "310.00", "-1.50", "2,000"],
            # 除息日：TPEX 月表給的是相對除息參考價的正確漲跌
            ["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
             "307.50", "322.50", "18.50", "5,095"],
        ],
        columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                 "收盤", "漲跌", "筆數"],
    )


def test_twse_expand_basic() -> None:
    out = expand_twse_stock_day(_twse_df())

    row = out[dt.date(2025, 9, 15)]
    assert row["open"] == 1250.0
    assert row["high"] == 1260.0
    assert row["low"] == 1245.0
    assert row["close"] == 1255.0
    assert row["volume"] == 24_000_000
    assert row["change"] == -5.0


def test_twse_ex_dividend_change_is_none_not_zero() -> None:
    """X0.00 必須是 None。變成 0.0 會讓參考價 = 收盤，算出假的漲跌停。"""
    out = expand_twse_stock_day(_twse_df())

    row = out[dt.date(2025, 9, 16)]
    assert row["change"] is None
    assert row["close"] == 1280.0  # 其他欄位照常


def test_tpex_expand_converts_lots_to_shares() -> None:
    out = expand_tpex_stock_day(_tpex_df())

    row = out[dt.date(2025, 7, 15)]
    assert row["volume"] == 1_234_000  # 1,234 張 × 1000
    assert row["close"] == 310.0
    assert row["change"] == -1.5


def test_tpex_ex_dividend_change_is_reference_price_delta() -> None:
    """6488 2025-07-16 除息 6 元：322.50 - (310.00 - 6.00) = 18.50。"""
    out = expand_tpex_stock_day(_tpex_df())

    assert out[dt.date(2025, 7, 16)]["change"] == 18.5


def test_expand_skips_unparsable_date_rows() -> None:
    df = pd.DataFrame(
        [["合計", "1", "1", "1", "1", "1", "1", "1", "1"]],
        columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                 "收盤", "漲跌", "筆數"],
    )
    assert expand_tpex_stock_day(df) == {}


def test_expand_empty_frame_returns_empty_dict() -> None:
    assert expand_twse_stock_day(pd.DataFrame()) == {}
    assert expand_tpex_stock_day(pd.DataFrame()) == {}


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_month_table_expand.py -v`
Expected: FAIL — `ImportError: cannot import name 'expand_twse_stock_day'`

- [ ] **Step 3: 實作展開函式**

在 `src/tw_stock_rawdata/sources.py` 的 `find_twse_ohlcv` 之後加入：

```python
def expand_twse_stock_day(
    df: pd.DataFrame,
) -> dict[dt.date, dict[str, float | int | None]]:
    """把 TWSE STOCK_DAY 月表展開成 `date -> {open/high/low/close/volume/change}`。

    `volume` 單位是**股**（STOCK_DAY 的「成交股數」本來就是股，直接沿用）。

    除權息日的「漲跌價差」是 `X0.00`（X 為顯式除權息標記）。`_clean_number` 對它
    的 `float()` 會拋 ValueError 而回 None —— 這正是我們要的：change 為 None 時
    呼叫端算不出參考價，漲跌停寫 NULL。**絕不可**在此把 X 剝掉當成 0.0，那會讓
    參考價等於收盤價，算出完全錯誤的漲跌停區間。
    """
    if df.empty or "日期" not in df.columns:
        return {}

    out: dict[dt.date, dict[str, float | int | None]] = {}
    for _, row in df.iterrows():
        date = _roc_to_date(row.get("日期"))
        if date is None:
            continue
        out[date] = {
            "open": _clean_number(row.get("開盤價")),
            "high": _clean_number(row.get("最高價")),
            "low": _clean_number(row.get("最低價")),
            "close": _clean_number(row.get("收盤價")),
            "volume": _clean_int(row.get("成交股數")),
            "change": _clean_number(row.get("漲跌價差")),
        }
    return out


def expand_tpex_stock_day(
    df: pd.DataFrame,
) -> dict[dt.date, dict[str, float | int | None]]:
    """把 TPEX 個股月表展開成 `date -> {open/high/low/close/volume/change}`。

    TPEX 月表的成交量欄位是「成交張數」，**在此 × 1000 換算成股**，讓兩個市場的
    展開結果單位一致（呼叫端契約為股，`_build_daily_rows` 尾端再 `// 1000`）。
    代價是失去零股尾數：實測 3,709 張 × 1000 = 3,709,000，實際 3,709,228 股。
    對已寫入 DB 的「張」而言結果相同（3709 == 3709），僅 `turnover_rate` 有約
    0.006% 相對誤差，已於設計階段確認接受。

    日期欄位名稱是 `日 期`（中間有空白），不是 `日期`。

    與 TWSE 不同，TPEX 月表在除權息日給的是**相對除息參考價的正確漲跌**
    （6488 於 2025-07-16 除息 6 元，月表給 18.50 = 322.50 − 304.00），
    不是標記字串。因此上櫃股回補時漲跌停算得出來，daily 模式走的
    `dailyQuotes` 則因為漲跌欄是文字「除息」而為 NULL —— 這個不一致已於設計
    階段確認接受（backfill 較準，搭配 upsert COALESCE 不會被 daily 蓋掉）。
    """
    if df.empty or "日 期" not in df.columns:
        return {}

    out: dict[dt.date, dict[str, float | int | None]] = {}
    for _, row in df.iterrows():
        date = _roc_to_date(row.get("日 期"))
        if date is None:
            continue
        lots = _clean_int(row.get("成交張數"))
        out[date] = {
            "open": _clean_number(row.get("開盤")),
            "high": _clean_number(row.get("最高")),
            "low": _clean_number(row.get("最低")),
            "close": _clean_number(row.get("收盤")),
            "volume": None if lots is None else lots * 1000,
            "change": _clean_number(row.get("漲跌")),
        }
    return out
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/test_month_table_expand.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 跑全部單元測試**

Run: `pytest tests/unit/ -q`
Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add src/tw_stock_rawdata/sources.py tests/unit/test_month_table_expand.py
git commit -m "feat: 個股月表展開成逐日 OHLCV/change，統一 volume 單位為股"
```

---

### Task 3: MoneyDJ `zcl` 取出三大法人買賣超

**Files:**
- Modify: `src/tw_stock_rawdata/sources.py:1039-1110`（`fetch_moneydj_holding_pct` 的欄位對映）
- Modify: `src/tw_stock_rawdata/prepare.py`（`prepare_moneydj_holding_pct` 之後，約 `:830`）
- Test: `tests/unit/test_moneydj_insti.py`（新檔）

**Interfaces:**
- Consumes: 既有 `fetch_moneydj_holding_pct` 的表格定位邏輯
- Produces:
  - `fetch_moneydj_holding_pct(...)` 回傳的 DataFrame **新增三欄**：`foreign_net_lots` / `trust_net_lots` / `dealer_net_lots`（原始字串，單位張）。原有 `date` / `foreign_holding_pct` / `insti_holding_pct` 三欄**不變**，故 `prepare_moneydj_holding_pct` 無需修改。
  - `prepare_moneydj_insti(df: pd.DataFrame) -> pd.DataFrame` — 回傳欄位 `date`（`dt.date`）、`foreign_net` / `trust_net` / `dealer_net`（**int，單位股**）。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_moneydj_insti.py`：

```python
"""Unit tests: MoneyDJ zcl 頁面內的三大法人買賣超。

背景：repo 為了 holding_pct 本來就在打 zcl.djhtm，該頁 col 1-4 就是三大法人
買賣超（外資/投信/自營商/單日合計）。改用它供應三大法人 = 零額外 HTTP 請求。

單位：MoneyDJ 給「張」且四捨五入；prepare 一律 × 1000 還原成「股」以符合
provider 契約。與 T86 的差異僅來自 floor vs round，最多 1 張，已確認接受。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata.prepare import prepare_moneydj_insti


def _raw() -> pd.DataFrame:
    """模擬 fetch_moneydj_holding_pct 的輸出（含新增的三欄）。"""
    return pd.DataFrame({
        "date": ["114/07/31", "114/07/30"],
        "foreign_net_lots": ["9040", "12832"],
        "trust_net_lots": ["-1293", "-352"],
        "dealer_net_lots": ["1612", "592"],
        "foreign_holding_pct": ["73.54%", "73.51%"],
        "insti_holding_pct": ["76.79%", "76.76%"],
    })


def test_prepare_insti_parses_dates_and_converts_lots_to_shares() -> None:
    out = prepare_moneydj_insti(_raw())

    by_date = out.set_index("date")
    row = by_date.loc[dt.date(2025, 7, 31)]
    assert row["foreign_net"] == 9_040_000
    assert row["trust_net"] == -1_293_000
    assert row["dealer_net"] == 1_612_000


def test_prepare_insti_matches_t86_within_one_lot() -> None:
    """對照 T86 2330 / 2025-07-31 的實際股數，差異必須 <= 1 張。"""
    out = prepare_moneydj_insti(_raw())
    row = out.set_index("date").loc[dt.date(2025, 7, 31)]

    t86 = {"foreign_net": 9_039_647, "trust_net": -1_292_952, "dealer_net": 1_611_836}
    for col, exact in t86.items():
        assert abs(row[col] - exact) <= 1000, col


def test_prepare_insti_drops_unparsable_dates() -> None:
    df = _raw()
    df.loc[len(df)] = ["合計", "1", "1", "1", "0%", "0%"]

    out = prepare_moneydj_insti(df)
    assert len(out) == 2


def test_prepare_insti_missing_columns_yields_none() -> None:
    df = pd.DataFrame({"date": ["114/07/31"]})

    out = prepare_moneydj_insti(df)
    assert out.iloc[0]["foreign_net"] is None
    assert out.iloc[0]["trust_net"] is None
    assert out.iloc[0]["dealer_net"] is None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_moneydj_insti.py -v`
Expected: FAIL — `ImportError: cannot import name 'prepare_moneydj_insti'`

- [ ] **Step 3: 擴充 `fetch_moneydj_holding_pct` 的欄位對映**

在 `src/tw_stock_rawdata/sources.py` 中，把 `fetch_moneydj_holding_pct` 結尾的欄位對映區塊（約 `:1104-1110`）：

```python
    # Column mapping (0-indexed):
    # 0: 日期, 9: 外資持股比重, 10: 三大法人持股比重
    result = pd.DataFrame()
    result["date"] = data_rows.iloc[:, 0].values
    result["foreign_holding_pct"] = data_rows.iloc[:, 9].values
    result["insti_holding_pct"] = data_rows.iloc[:, 10].values

    return result
```

替換為：

```python
    # MoneyDJ zcl 欄位對映（0-indexed）：
    #   0     日期
    #   1-4   買賣超：外資 / 投信 / 自營商 / 單日合計（單位：張）
    #   5-8   估計持股：外資 / 投信 / 自營商 / 單日合計
    #   9-10  持股比重：外資 / 三大法人
    # 三大法人買賣超與持股比重在**同一頁**，所以取三大法人不需要額外 HTTP 請求。
    # 這裡只做欄位切出，型別轉換留給 prepare_moneydj_holding_pct /
    # prepare_moneydj_insti，維持 fetch 層只負責取得與定位的分工。
    result = pd.DataFrame()
    result["date"] = data_rows.iloc[:, 0].values
    result["foreign_net_lots"] = data_rows.iloc[:, 1].values
    result["trust_net_lots"] = data_rows.iloc[:, 2].values
    result["dealer_net_lots"] = data_rows.iloc[:, 3].values
    result["foreign_holding_pct"] = data_rows.iloc[:, 9].values
    result["insti_holding_pct"] = data_rows.iloc[:, 10].values

    return result
```

同時把該函式 docstring 的 Returns 段落改為：

```python
    Returns:
        DataFrame with columns: date, foreign_net_lots, trust_net_lots,
        dealer_net_lots (買賣超，單位張), foreign_holding_pct, insti_holding_pct
        (percentage strings like "35.03%")
```

- [ ] **Step 4: 實作 `prepare_moneydj_insti`**

在 `src/tw_stock_rawdata/prepare.py` 的 `prepare_moneydj_holding_pct` 之後加入：

```python
def prepare_moneydj_insti(df: pd.DataFrame) -> pd.DataFrame:
    """把 MoneyDJ zcl 的三大法人買賣超整理成標準格式。

    輸入來自 `fetch_moneydj_holding_pct`（同一份 HTML、同一次請求）：
    date / foreign_net_lots / trust_net_lots / dealer_net_lots。

    輸出 date（`dt.date`）+ foreign_net / trust_net / dealer_net，**單位為股**
    （張 × 1000），以符合 `RowSourceProvider.insti()` 的契約。

    MoneyDJ 的張數是四捨五入的，交易所 T86 的股數則會在 `_build_daily_rows` 被
    `// 1000` 無條件捨去，兩者最多差 1 張。實測 2330 / 2025-07-31：
    外資 T86 9,039,647 → 9039 張，MoneyDJ 9040 張。此誤差已於設計階段確認接受。

    MoneyDJ 的「外資」對應 T86 的「外陸資買賣超股數(不含外資自營商)」。實測
    2025-07-31 全市場 14,186 檔的「外資自營商」欄全為 0，兩種定義實務上等價。
    """
    if "date" not in df.columns:
        raise DataUnavailableError("MoneyDJ 三大法人欄位解析失敗，缺少 date")

    result = pd.DataFrame()
    result["date"] = df["date"].map(
        lambda v: None if pd.isna(v) else _parse_roc_date(str(v).strip())
    )

    def _lots_to_shares(val):
        lots = _clean_int(val)
        return None if lots is None else lots * 1000

    for out_col, in_col in [
        ("foreign_net", "foreign_net_lots"),
        ("trust_net", "trust_net_lots"),
        ("dealer_net", "dealer_net_lots"),
    ]:
        if in_col in df.columns:
            result[out_col] = df[in_col].map(_lots_to_shares)
        else:
            result[out_col] = None

    result = result.dropna(subset=["date"])

    return result
```

若 `prepare.py` 尚未 import `_clean_int`，在檔案頂端的 import 區補上（與 `_clean_number` 同一來源）。用以下指令確認：

```bash
grep -n "_clean_int\|_clean_number\|_parse_roc_date" src/tw_stock_rawdata/prepare.py | head -5
```

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/unit/test_moneydj_insti.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 確認既有 holding_pct 測試沒被打壞**

Run: `pytest tests/unit/ -q`
Expected: 全綠（`prepare_moneydj_holding_pct` 只讀具名欄位，多加三欄不影響它）

- [ ] **Step 7: Commit**

```bash
git add src/tw_stock_rawdata/sources.py src/tw_stock_rawdata/prepare.py tests/unit/test_moneydj_insti.py
git commit -m "feat: 從既有 MoneyDJ zcl 頁面取出三大法人買賣超，零額外請求"
```

---

### Task 4: `RowSourceProvider` 介面 + `BatchSourceProvider`（純重構）

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（新增 class 於 `_build_daily_rows` 之前，約 `:745`；改 `_build_daily_rows` 簽名 `:750-768`、內文 `:800-830`；改兩處呼叫端 `:1484-1503`、`:1815-1830`）
- Modify: `tests/unit/test_daily_rows_limit_seam.py`（呼叫端改建 provider）
- Modify: `tests/unit/test_run_ohlcv_source_order.py`（僅 `test_build_daily_rows_*` 兩則改呼叫端）
- Test: `tests/unit/test_row_source_provider.py`（新檔）

**Interfaces:**
- Consumes: 既有 `_fetch_ohlcv_with_fallback`、`_get_institutional_data`、`OhlcvResult`
- Produces:
  - `class RowSourceProvider(Protocol)`，四個方法：
    - `ohlcv(symbol: str, date: dt.date, market_type: str | None) -> OhlcvResult`
    - `insti(symbol: str, date: dt.date) -> tuple[int | None, int | None, int | None]`（單位股）
    - `insti_ok(symbol: str, market_type: str | None) -> bool`
    - `is_tpex(symbol: str, market_type: str | None) -> bool`
  - `class BatchSourceProvider` 實作上述介面
  - `_build_daily_rows(*, date, holdings, provider, issued_shares=None, twse_margin=None, tpex_margin=None, margin_cache=None, holding_pct_cache=None, name_map=None, disposition=None) -> pd.DataFrame` — **移除** `session` / `twse_3insti` / `twse_day_all` / `twse_mi_index` / `tpex_quotes` / `tpex_3insti` / `twse_month_cache` / `twse_insti_ok` / `tpex_insti_ok` 九個參數，改為單一 `provider`

**本 task 是純重構：`_build_daily_rows` 對任何輸入的輸出必須與重構前逐欄相同。**
`_fetch_ohlcv_with_fallback`、`_get_institutional_data`、`_stock_sources_ok`
三個自由函式**保留不動**（`BatchSourceProvider` 委派給它們），因此
`test_run_ohlcv_source_order.py` 的前 6 則與 `test_run_stock_sources.py` 完全不需修改。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_row_source_provider.py`：

```python
"""Unit tests: RowSourceProvider 介面與 BatchSourceProvider。

BatchSourceProvider 只是把現行的自由函式包起來，必須零行為變更 —— 這裡逐一
比對 provider 的回傳與直接呼叫自由函式的結果。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata import run

DATE = dt.date(2026, 8, 19)

_EMPTY_WITH_SYMBOL = pd.DataFrame(columns=["symbol"])
_EMPTY_TPEX_QUOTES = pd.DataFrame(
    columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
)


def _mi_index_row(symbol: str) -> dict:
    return {
        "symbol": symbol, "name": "台積電", "open": 100.0, "close": 105.0,
        "high": 106.0, "low": 99.0, "volume": 1_234_000, "change": 5.0,
    }


def _provider(**kwargs) -> run.BatchSourceProvider:
    defaults = dict(
        session=None,
        twse_3insti=_EMPTY_WITH_SYMBOL,
        twse_day_all=None,
        twse_mi_index=None,
        tpex_quotes=_EMPTY_TPEX_QUOTES,
        tpex_3insti=_EMPTY_WITH_SYMBOL,
        twse_month_cache={},
        twse_insti_ok=True,
        tpex_insti_ok=True,
    )
    defaults.update(kwargs)
    return run.BatchSourceProvider(**defaults)


def test_batch_provider_ohlcv_matches_free_function() -> None:
    mi = pd.DataFrame([_mi_index_row("2330")])
    provider = _provider(twse_mi_index=mi)

    got = provider.ohlcv("2330", DATE, "twse")
    expected = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=None, twse_mi_index=mi,
        tpex_quotes=_EMPTY_TPEX_QUOTES, twse_month_cache={},
        market_type="twse",
    )
    assert got == expected


def test_batch_provider_insti_matches_free_function() -> None:
    twse_3insti = pd.DataFrame([{
        "symbol": "2330", "foreign_net": 9_039_647,
        "trust_net": -1_292_952, "dealer_net": 1_611_836,
    }])
    provider = _provider(twse_3insti=twse_3insti)

    assert provider.insti("2330", DATE) == run._get_institutional_data(
        "2330", twse_3insti, _EMPTY_WITH_SYMBOL
    )


def test_batch_provider_is_tpex_uses_todays_tpex_quotes() -> None:
    """batch 模式的 is_tpex 反映「今天價格誰供應的」，來源是當日 tpex_quotes，
    刻意不看 stocks.market_type —— 這是既有設計，不可改。"""
    quotes = pd.DataFrame([{
        "symbol": "3105", "name": "穩懋", "open": 1.0, "close": 1.0,
        "high": 1.0, "low": 1.0, "volume": 1, "change": 0.0,
    }])
    provider = _provider(tpex_quotes=quotes)

    assert provider.is_tpex("3105", market_type=None) is True
    assert provider.is_tpex("2330", market_type="tpex") is False


def test_batch_provider_insti_ok_follows_market() -> None:
    provider = _provider(twse_insti_ok=False, tpex_insti_ok=True)
    quotes = pd.DataFrame([{
        "symbol": "3105", "name": "穩懋", "open": 1.0, "close": 1.0,
        "high": 1.0, "low": 1.0, "volume": 1, "change": 0.0,
    }])
    provider_tpex = _provider(
        twse_insti_ok=False, tpex_insti_ok=True, tpex_quotes=quotes
    )

    assert provider.insti_ok("2330", "twse") is False
    assert provider_tpex.insti_ok("3105", "tpex") is True


def test_build_daily_rows_accepts_provider() -> None:
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])
    mi = pd.DataFrame([_mi_index_row("2330")])

    result = run._build_daily_rows(
        date=DATE,
        holdings=holdings,
        provider=_provider(twse_mi_index=mi),
    )

    assert len(result) == 1
    assert result.iloc[0]["close"] == 105.0
    assert result.iloc[0]["volume"] == 1234  # 股 // 1000


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_row_source_provider.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.run' has no attribute 'BatchSourceProvider'`

- [ ] **Step 3: 新增 Protocol 與 `BatchSourceProvider`**

在 `src/tw_stock_rawdata/run.py` 頂端 import 區把 `from typing import NamedTuple` 改成：

```python
from typing import NamedTuple, Protocol
```

在 `_build_daily_rows` 定義之前加入：

```python
class RowSourceProvider(Protocol):
    """`_build_daily_rows` 取得「該檔該日」原始資料的唯一管道。

    有兩種實作，差別只在資料從哪來，組列邏輯共用同一條路徑（不產生雙軌）：
    - `BatchSourceProvider`：全市場批次來源（daily / 全市場 backfill）
    - `PerSymbolRangeProvider`：per-stock 區間來源（`--backfill-stocks`）

    **單位契約：`ohlcv().volume` 與 `insti()` 回傳的三個值一律是「股」。**
    per-symbol 來源拿到的是「張」，一律在 provider 內 × 1000 還原，讓
    `_build_daily_rows` 尾端的 `// 1000` 不需要任何 per-mode 分支。
    """

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult: ...

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]: ...

    def insti_ok(self, symbol: str, market_type: str | None) -> bool: ...

    def is_tpex(self, symbol: str, market_type: str | None) -> bool: ...


class BatchSourceProvider:
    """全市場批次來源的 provider（daily / 全市場 backfill 用）。

    純委派給既有的自由函式，**零行為變更**。自由函式刻意保留在模組層級，
    既有的 `test_run_ohlcv_source_order` / `test_run_stock_sources` 因此不需改動。
    """

    def __init__(
        self,
        *,
        session: requests.Session | None,
        twse_3insti: pd.DataFrame,
        twse_day_all: pd.DataFrame | None,
        twse_mi_index: pd.DataFrame | None,
        tpex_quotes: pd.DataFrame,
        tpex_3insti: pd.DataFrame,
        twse_month_cache: dict[tuple[str, dt.date], pd.DataFrame],
        twse_insti_ok: bool = True,
        tpex_insti_ok: bool = True,
    ) -> None:
        self._session = session
        self._twse_3insti = twse_3insti
        self._twse_day_all = twse_day_all
        self._twse_mi_index = twse_mi_index
        self._tpex_quotes = tpex_quotes
        self._tpex_3insti = tpex_3insti
        self._twse_month_cache = twse_month_cache
        self._twse_insti_ok = twse_insti_ok
        self._tpex_insti_ok = tpex_insti_ok

        if not tpex_quotes.empty and "symbol" in tpex_quotes.columns:
            self._tpex_symbols = set(tpex_quotes["symbol"].astype(str).str.strip())
        else:
            self._tpex_symbols = set()

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult:
        return _fetch_ohlcv_with_fallback(
            self._session, date, symbol,
            self._twse_day_all, self._twse_mi_index,
            self._tpex_quotes, self._twse_month_cache,
            market_type=market_type,
        )

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]:
        return _get_institutional_data(symbol, self._twse_3insti, self._tpex_3insti)

    def insti_ok(self, symbol: str, market_type: str | None) -> bool:
        return _stock_sources_ok(
            is_tpex=self.is_tpex(symbol, market_type),
            twse_insti_ok=self._twse_insti_ok,
            tpex_insti_ok=self._tpex_insti_ok,
        )

    def is_tpex(self, symbol: str, market_type: str | None) -> bool:
        """batch 模式用「該檔今天有沒有出現在 tpex_quotes」判定。

        這反映的是「這檔今天的價格是誰供應的」，必須跟著當日實際來源走，
        刻意**不**用 `stocks.market_type`（那反映的是「本質上屬於哪個市場」，
        用途不同，見 `_build_daily_rows` docstring）。
        """
        return symbol in self._tpex_symbols
```

- [ ] **Step 4: 改 `_build_daily_rows` 簽名與內文**

把 `_build_daily_rows` 的簽名改為：

```python
def _build_daily_rows(
    *,
    date: dt.date,
    holdings: pd.DataFrame,
    provider: RowSourceProvider,
    issued_shares: dict[str, int] | None = None,
    twse_margin: pd.DataFrame | None = None,
    tpex_margin: pd.DataFrame | None = None,
    margin_cache: dict[str, dict[dt.date, dict]] | None = None,
    holding_pct_cache: dict[str, dict[dt.date, dict]] | None = None,
    name_map: dict[str, str] | None = None,
    disposition: DispositionData | None = None,
) -> pd.DataFrame:
```

docstring 的「市場別有兩個獨立訊號」段落改為：

```
    市場別有兩個獨立訊號，用途不同、不要互相取代：
    - gating 用 `provider.is_tpex()`。batch provider 以「該檔是否出現在當日
      tpex_quotes」回答，反映「這檔今天的價格是誰供應的」；per-symbol provider
      直接回答 stocks.market_type，因為那個模式下市場別是已知事實。
    - OHLCV fallback 用 holdings 的 `stocks.market_type`（見
      `_fetch_ohlcv_with_fallback`），反映「這檔本質上屬於哪個市場」，不能依賴
      當日 tpex_quotes 是否抓成功 —— 否則 TPEX 整批失敗時，上櫃股又會退回去打
      註定沒資料的 TWSE 月表。
```

移除函式開頭的 `tpex_symbols` 計算區塊：

```python
    if not tpex_quotes.empty and "symbol" in tpex_quotes.columns:
        tpex_symbols = set(tpex_quotes["symbol"].astype(str).str.strip())
    else:
        tpex_symbols = set()
```

把迴圈內的三處呼叫改為走 provider：

```python
        market_type = _row_market_type(item)

        ohlcv = provider.ohlcv(symbol, date, market_type)
```

```python
        # 逐檔跳過 2：該檔市場別的三大法人來源失敗（融資融券例外，不 gating）
        if not provider.insti_ok(symbol, market_type):
            skipped += 1
            continue
```

```python
        foreign_net, trust_net, dealer_net = provider.insti(symbol, date)
```

迴圈內原本的 `is_tpex = symbol in tpex_symbols` 一行刪除；
處置註記區塊原本的 `_row_market_type(item)` 改用上面已取好的 `market_type` 變數。

- [ ] **Step 5: 改兩處呼叫端**

`_run_for_date` 內（約 `:1484`）的呼叫改為：

```python
    with _phase(f"{sheet_name} 逐檔組列（{len(holdings)} 檔）"):
        provider = BatchSourceProvider(
            session=session,
            twse_3insti=twse_3insti,
            twse_day_all=twse_day_all,
            twse_mi_index=twse_mi_index,
            tpex_quotes=tpex_quotes,
            tpex_3insti=tpex_3insti,
            twse_month_cache=twse_month_cache,
            twse_insti_ok=twse_insti_ok,
            tpex_insti_ok=tpex_insti_ok,
        )
        output_df = _build_daily_rows(
            date=date,
            holdings=holdings,
            provider=provider,
            issued_shares=issued_shares,
            twse_margin=twse_margin,
            tpex_margin=tpex_margin,
            margin_cache=margin_cache,
            holding_pct_cache=holding_pct_cache,
            name_map=name_map,
            disposition=disposition,
        )
```

`_run_for_date_no_write` 內（約 `:1813`）的呼叫改為（注意這個函式**沒有** `disposition` 參數，不要憑空加）：

```python
    provider = BatchSourceProvider(
        session=session,
        twse_3insti=twse_3insti,
        twse_day_all=twse_day_all,
        twse_mi_index=twse_mi_index,
        tpex_quotes=tpex_quotes,
        tpex_3insti=tpex_3insti,
        twse_month_cache=twse_month_cache,
        twse_insti_ok=twse_insti_ok,
        tpex_insti_ok=tpex_insti_ok,
    )
    output_df = _build_daily_rows(
        date=date,
        holdings=holdings,
        provider=provider,
        issued_shares=issued_shares,
        twse_margin=twse_margin,
        tpex_margin=tpex_margin,
        margin_cache=margin_cache,
        holding_pct_cache=holding_pct_cache,
        name_map=name_map,
    )
```

- [ ] **Step 6: 改兩則既有測試的呼叫端（斷言不動）**

`tests/unit/test_daily_rows_limit_seam.py` 的 `run._build_daily_rows(...)` 改為：

```python
    result = run._build_daily_rows(
        date=DATE,
        holdings=holdings,
        provider=run.BatchSourceProvider(
            session=None,
            twse_3insti=_EMPTY_WITH_SYMBOL,
            twse_day_all=twse_day_all,
            twse_mi_index=twse_mi_index,
            tpex_quotes=_EMPTY_TPEX_QUOTES,
            tpex_3insti=_EMPTY_WITH_SYMBOL,
            twse_month_cache={},
        ),
    )
```

`tests/unit/test_run_ohlcv_source_order.py` 的
`test_build_daily_rows_passes_market_type_through` 與
`test_build_daily_rows_without_market_type_column` 兩則做同樣改寫
（把原本傳給 `_build_daily_rows` 的來源參數原封不動搬進 `BatchSourceProvider(...)`）。
**兩檔的 assert 一律不改。**

- [ ] **Step 7: 跑測試確認通過**

Run: `pytest tests/unit/ -q`
Expected: 全綠。若 `test_run_ohlcv_source_order.py` 前 6 則或 `test_run_stock_sources.py` 有任何一則變紅，代表自由函式被動到了 —— 回退該處改動，本 task 必須零行為變更。

- [ ] **Step 8: Commit**

```bash
git add src/tw_stock_rawdata/run.py tests/unit/
git commit -m "refactor: _build_daily_rows 改吃 RowSourceProvider，行為不變"
```

---

### Task 5: 單檔區間 OHLCV 預取（含限流交叉比對）

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（放在 `_prefetch_holding_pct_cache` 之後，約 `:1268`）
- Test: `tests/unit/test_prefetch_symbol_ohlcv.py`（新檔）

**Interfaces:**
- Consumes: `fetch_twse_stock_day`、`fetch_tpex_stock_day`、`expand_twse_stock_day`、`expand_tpex_stock_day`（Task 1、2）、`OhlcvResult`
- Produces:
  - `class SymbolOhlcv(NamedTuple)`：`by_date: dict[dt.date, OhlcvResult]`、`market_type: str | None`、`failed_months: list[dt.date]`
  - `_month_starts(start: dt.date, end: dt.date) -> list[dt.date]`
  - `_prefetch_symbol_ohlcv(session, symbol: str, market_type: str | None, start: dt.date, end: dt.date, traded_dates: set[dt.date]) -> SymbolOhlcv`

  `traded_dates` 是該檔在 MoneyDJ `zcl` 有出現的日期集合，用於區分「該月真的沒交易」與「限流／取得失敗」。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_prefetch_symbol_ohlcv.py`：

```python
"""Unit tests: 單檔區間 OHLCV 預取（無網路）。

核心風險：www.twse.com.tw 被限流時回 HTTP 200 + stat「很抱歉，沒有符合條件的資料!」，
與「該月真的沒資料」是同一個字串，無法從回應區分（見
memory/twse-rate-limit-ambiguous-response.md）。

唯一可用的外部訊號是 MoneyDJ zcl：它整段只打一發、不經 TWSE，若它證明該月有交易
而交易所月表回空，就判定為限流／取得失敗，該月不寫並警告 —— 而不是靜默當成沒交易。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

START = dt.date(2025, 6, 10)
END = dt.date(2025, 8, 5)


def test_month_starts_covers_partial_edges() -> None:
    assert run._month_starts(START, END) == [
        dt.date(2025, 6, 1), dt.date(2025, 7, 1), dt.date(2025, 8, 1),
    ]


def test_month_starts_single_month() -> None:
    assert run._month_starts(dt.date(2025, 7, 3), dt.date(2025, 7, 20)) == [
        dt.date(2025, 7, 1)
    ]


def test_month_starts_crosses_year_boundary() -> None:
    assert run._month_starts(dt.date(2025, 12, 20), dt.date(2026, 1, 5)) == [
        dt.date(2025, 12, 1), dt.date(2026, 1, 1),
    ]


def _twse_month_df(day: int) -> pd.DataFrame:
    return pd.DataFrame(
        [["114/07/%02d" % day, "24,000,000", "30,000,000", "1250.00",
          "1260.00", "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


def test_twse_symbol_fetches_month_tables(monkeypatch) -> None:
    calls: list[dt.date] = []

    def fake(session, stock_no, date):  # noqa: ANN001 - 測試替身
        calls.append(date)
        return _twse_month_df(15)

    monkeypatch.setattr(run, "fetch_twse_stock_day", fake)

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert calls == [dt.date(2025, 7, 1)]
    assert out.market_type == "twse"
    assert out.failed_months == []
    assert out.by_date[dt.date(2025, 7, 15)].close == 1255.0


def test_tpex_symbol_never_calls_twse(monkeypatch) -> None:
    """上櫃股不可打 TWSE 月表 —— 那支 API 沒有上櫃資料，只是白耗限流配額。"""
    twse_calls: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: twse_calls.append("x") or pd.DataFrame(),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type="tpex",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert twse_calls == []
    assert out.by_date[dt.date(2025, 7, 16)].volume == 3_709_000
    assert out.by_date[dt.date(2025, 7, 16)].change == 18.5


def test_empty_month_without_moneydj_evidence_is_treated_as_no_trading(
    monkeypatch,
) -> None:
    """MoneyDJ 也沒有該月資料 → 該檔那個月本來就沒交易，不算失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert out.by_date == {}
    assert out.failed_months == []


def test_empty_month_with_moneydj_evidence_is_flagged_as_failure(
    monkeypatch,
) -> None:
    """MoneyDJ 證明該月有交易，交易所月表卻回空 → 判定限流／取得失敗。"""
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type="twse",
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates={dt.date(2025, 7, 15)},
    )

    assert out.by_date == {}
    assert out.failed_months == [dt.date(2025, 7, 1)]


def test_unknown_market_type_probes_twse_first(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse") or _twse_month_df(15),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda *a, **k: order.append("tpex") or pd.DataFrame(),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="2330", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse"]
    assert out.market_type == "twse"


def test_unknown_market_type_falls_back_to_tpex(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        run, "fetch_twse_stock_day",
        lambda *a, **k: order.append("twse")
        or (_ for _ in ()).throw(DataUnavailableError("無資料")),
    )
    monkeypatch.setattr(
        run, "fetch_tpex_stock_day",
        lambda session, stock_no, date: order.append("tpex") or pd.DataFrame(
            [["114/07/16", "3,709", "1,184,624", "310.00", "326.50",
              "307.50", "322.50", "18.50", "5,095"]],
            columns=["日 期", "成交張數", "成交仟元", "開盤", "最高", "最低",
                     "收盤", "漲跌", "筆數"],
        ),
    )

    out = run._prefetch_symbol_ohlcv(
        session=None, symbol="6488", market_type=None,
        start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        traded_dates=set(),
    )

    assert order == ["twse", "tpex"]
    assert out.market_type == "tpex"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_prefetch_symbol_ohlcv.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.run' has no attribute '_month_starts'`

- [ ] **Step 3: 補 import**

在 `src/tw_stock_rawdata/run.py` 的 `from .sources import (...)` 區塊加入
（維持字母序）：

```python
    expand_tpex_stock_day,
    expand_twse_stock_day,
    fetch_tpex_stock_day,
```

在 `from .prepare import (...)` 區塊加入：

```python
    prepare_moneydj_insti,
```

- [ ] **Step 4: 實作預取函式**

在 `_prefetch_holding_pct_cache` 之後加入：

```python
class SymbolOhlcv(NamedTuple):
    """單檔在整段區間的 OHLCV 預取結果。

    failed_months 是「判定為限流／取得失敗」的月份（月初日期）。這些月份的日期
    在 by_date 裡不存在，因此 `_build_daily_rows` 會因為無價格而跳過該檔該日
    ——與現行「無價格就不寫」的不變量一致，不需要新的 gating 分支。
    market_type 是定調後的市場別（輸入為 None 時由探測結果填入）。
    """

    by_date: dict[dt.date, OhlcvResult]
    market_type: str | None
    failed_months: list[dt.date]


def _month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    """回傳涵蓋 [start, end] 的所有月份的月初日期（含頭尾的不完整月）。"""
    months: list[dt.date] = []
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        months.append(cur)
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return months


def _fetch_month_ohlcv(
    session: requests.Session,
    symbol: str,
    market_type: str,
    month: dt.date,
) -> dict[dt.date, OhlcvResult]:
    """抓單檔單月的月表並展開。該月無資料時拋 DataUnavailableError。"""
    if market_type == "tpex":
        raw = fetch_tpex_stock_day(session, symbol, month)
        expanded = expand_tpex_stock_day(raw)
    else:
        raw = fetch_twse_stock_day(session, symbol, month)
        expanded = expand_twse_stock_day(raw)

    return {
        date: OhlcvResult(
            open=vals["open"], close=vals["close"],
            high=vals["high"], low=vals["low"],
            volume=vals["volume"], change=vals["change"],
        )
        for date, vals in expanded.items()
    }


def _prefetch_symbol_ohlcv(
    session: requests.Session,
    symbol: str,
    market_type: str | None,
    start: dt.date,
    end: dt.date,
    traded_dates: set[dt.date],
) -> SymbolOhlcv:
    """逐月抓單檔在 [start, end] 的 OHLCV + change。

    每月 1 發請求，取代原本「每個交易日 4 發全市場批次」的成本結構。

    market_type 為 None（`--backfill-stocks` 直接給代號、DB 查不到市場別）時，
    用第一個有資料的月份定調：先試 TWSE，回空再試 TPEX，之後整段沿用。

    **限流誤判防護**：`www.twse.com.tw` 被限流時回 HTTP 200 +
    「很抱歉，沒有符合條件的資料!」，與「該月真的沒資料」是同一個字串，無法從
    回應本身區分（見 memory/twse-rate-limit-ambiguous-response.md）。這裡用
    `traded_dates`（該檔在 MoneyDJ zcl 出現過的日期，整段只打一發、不經 TWSE）
    當外部證據：若 MoneyDJ 證明該月有交易而月表回空，就記進 failed_months 並
    警告，該月不寫；兩邊都沒有才當成「該檔那個月本來就沒交易」。
    """
    by_date: dict[dt.date, OhlcvResult] = {}
    failed_months: list[dt.date] = []
    resolved = market_type

    for month in _month_starts(start, end):
        month_rows: dict[dt.date, OhlcvResult] = {}

        if resolved is None:
            # 市場別未知：先 TWSE 後 TPEX，以先取得資料者定調。
            for candidate in ("twse", "tpex"):
                try:
                    month_rows = _fetch_month_ohlcv(session, symbol, candidate, month)
                except (DataUnavailableError, requests.RequestException):
                    continue
                if month_rows:
                    resolved = candidate
                    break
        else:
            try:
                month_rows = _fetch_month_ohlcv(session, symbol, resolved, month)
            except (DataUnavailableError, requests.RequestException) as exc:
                print(f"    {symbol} {month:%Y-%m} 月表取得失敗：{exc}")

        if month_rows:
            by_date.update(month_rows)
            continue

        # 月表沒給東西 —— 是「沒交易」還是「被限流」？用 MoneyDJ 當外部證據。
        month_end = (month + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        if any(month <= d <= month_end for d in traded_dates):
            failed_months.append(month)
            print(
                f"    ⚠ {symbol} {month:%Y-%m} 月表回空，但 MoneyDJ 顯示該月有交易"
                "：判定為限流／取得失敗，該月不寫入（請稍後重跑此區間）"
            )

    return SymbolOhlcv(
        by_date=by_date, market_type=resolved, failed_months=failed_months
    )
```

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/unit/test_prefetch_symbol_ohlcv.py -v`
Expected: PASS（8 passed）

- [ ] **Step 6: 跑全部單元測試**

Run: `pytest tests/unit/ -q`
Expected: 全綠

- [ ] **Step 7: Commit**

```bash
git add src/tw_stock_rawdata/run.py tests/unit/test_prefetch_symbol_ohlcv.py
git commit -m "feat: 單檔區間 OHLCV 逐月預取，並用 MoneyDJ 交叉比對擋限流誤判"
```

---

### Task 6: `PerSymbolRangeProvider`

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（接在 `BatchSourceProvider` 之後）
- Test: `tests/unit/test_per_symbol_provider.py`（新檔）

**Interfaces:**
- Consumes: `RowSourceProvider`、`SymbolOhlcv`、`_prefetch_symbol_ohlcv`（Task 5）、`fetch_moneydj_holding_pct`、`prepare_moneydj_insti`（Task 3）
- Produces:
  - `class PerSymbolRangeProvider` 實作 `RowSourceProvider`
  - classmethod `PerSymbolRangeProvider.build(session, symbols: list[str], market_types: dict[str, str], start: dt.date, end: dt.date) -> PerSymbolRangeProvider`
  - property `resolved_market_types: dict[str, str]` — 探測後定調的市場別（`main()` 用來組 holdings 的 `market_type` 欄）

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_per_symbol_provider.py`：

```python
"""Unit tests: PerSymbolRangeProvider（無網路）。

契約重點：
- ohlcv().volume 與 insti() 一律回「股」（per-symbol 來源是張，provider 內 ×1000）
- is_tpex 直接回 stocks.market_type —— per-stock 模式下市場別是已知事實，
  不需要 batch 模式那個「靠當日 tpex_quotes 推市場別」的 workaround
- MoneyDJ 整段取得失敗 → insti_ok 為 False → 該檔每一天都跳過不寫
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run
from tw_stock_rawdata.sources import DataUnavailableError

START = dt.date(2025, 7, 1)
END = dt.date(2025, 7, 31)
D = dt.date(2025, 7, 31)


def _moneydj_raw() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["114/07/31"],
        "foreign_net_lots": ["9040"],
        "trust_net_lots": ["-1293"],
        "dealer_net_lots": ["1612"],
        "foreign_holding_pct": ["73.54%"],
        "insti_holding_pct": ["76.79%"],
    })


def _twse_month_df() -> pd.DataFrame:
    return pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    )


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct", lambda *a, **k: _moneydj_raw()
    )


def test_ohlcv_returns_shares_not_lots(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    result = provider.ohlcv("2330", D, "twse")
    assert result.close == 1255.0
    assert result.volume == 24_000_000
    assert result.change == -5.0


def test_insti_returns_shares(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.insti("2330", D) == (9_040_000, -1_293_000, 1_612_000)


def test_missing_date_yields_empty_ohlcv(wired) -> None:
    """該日不在月表裡（沒交易 / 停牌）→ 全 None，_build_daily_rows 會跳過該列。"""
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    result = provider.ohlcv("2330", dt.date(2025, 7, 1), "twse")
    assert result.open is None
    assert result.close is None
    assert result.volume is None


def test_is_tpex_uses_market_type_directly(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.is_tpex("2330", "twse") is False
    assert provider.is_tpex("6488", "tpex") is True


def test_insti_ok_false_when_moneydj_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )
    monkeypatch.setattr(
        run, "fetch_moneydj_holding_pct",
        lambda *a, **k: (_ for _ in ()).throw(DataUnavailableError("down")),
    )

    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=START, end=END,
    )

    assert provider.insti_ok("2330", "twse") is False
    assert provider.insti("2330", D) == (None, None, None)


def test_resolved_market_types_exposed(wired) -> None:
    provider = run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={},
        start=START, end=END,
    )

    assert provider.resolved_market_types["2330"] == "twse"


def test_moneydj_fetched_once_per_symbol(monkeypatch) -> None:
    """整段只打一發 MoneyDJ —— 這是本設計省下請求數的關鍵之一。"""
    calls: list[tuple] = []

    monkeypatch.setattr(
        run, "fetch_twse_stock_day", lambda *a, **k: _twse_month_df()
    )

    def fake(session, symbol, start, end):  # noqa: ANN001 - 測試替身
        calls.append((symbol, start, end))
        return _moneydj_raw()

    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", fake)

    run.PerSymbolRangeProvider.build(
        session=None, symbols=["2330"], market_types={"2330": "twse"},
        start=dt.date(2023, 1, 1), end=dt.date(2025, 12, 31),
    )

    assert calls == [("2330", dt.date(2023, 1, 1), dt.date(2025, 12, 31))]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_per_symbol_provider.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.run' has no attribute 'PerSymbolRangeProvider'`

- [ ] **Step 3: 實作 provider**

在 `BatchSourceProvider` 之後加入：

```python
class PerSymbolRangeProvider:
    """per-stock 區間來源的 provider（`--backfill-stocks` 用）。

    建構時把整段區間的資料一次抓完，之後全部是記憶體查表、零 HTTP：
    - OHLCV + change：逐月抓 TWSE STOCK_DAY / TPEX 個股月表（每檔每月 1 發）
    - 三大法人：MoneyDJ zcl，**整段只打 1 發**（該頁同時含持股比重，
      所以三大法人是零額外請求）

    單位契約與 BatchSourceProvider 相同：`ohlcv().volume` 與 `insti()` 一律回「股」。
    per-symbol 來源給的是「張」，已在 `expand_tpex_stock_day` /
    `prepare_moneydj_insti` 內 × 1000 還原。
    """

    def __init__(
        self,
        *,
        ohlcv_by_symbol: dict[str, dict[dt.date, OhlcvResult]],
        insti_by_symbol: dict[str, dict[dt.date, tuple[int | None, int | None, int | None]]],
        insti_ok_by_symbol: dict[str, bool],
        resolved_market_types: dict[str, str],
    ) -> None:
        self._ohlcv = ohlcv_by_symbol
        self._insti = insti_by_symbol
        self._insti_ok = insti_ok_by_symbol
        self.resolved_market_types = resolved_market_types

    @classmethod
    def build(
        cls,
        session: requests.Session,
        symbols: list[str],
        market_types: dict[str, str],
        start: dt.date,
        end: dt.date,
    ) -> PerSymbolRangeProvider:
        """對每檔預取整段的 OHLCV 與三大法人。

        先抓 MoneyDJ（整段 1 發），再用它產出的日期集合當「該檔哪些日子有交易」的
        外部證據，交給 `_prefetch_symbol_ohlcv` 區分「月表回空」是沒交易還是被限流。
        順序不可對調。
        """
        ohlcv_by_symbol: dict[str, dict[dt.date, OhlcvResult]] = {}
        insti_by_symbol: dict[str, dict[dt.date, tuple]] = {}
        insti_ok_by_symbol: dict[str, bool] = {}
        resolved: dict[str, str] = {}

        total = len(symbols)
        for idx, symbol in enumerate(symbols, start=1):
            print(f"  預取個股區間資料 {idx}/{total} {symbol}")

            # 1) MoneyDJ zcl：三大法人（整段 1 發，與持股比重同一頁）
            insti_map: dict[dt.date, tuple] = {}
            insti_ok = False
            try:
                raw = fetch_moneydj_holding_pct(session, symbol, start, end)
                insti_df = prepare_moneydj_insti(raw)
                for _, row in insti_df.iterrows():
                    row_date = row["date"]
                    if not isinstance(row_date, dt.date):
                        continue
                    insti_map[row_date] = (
                        row.get("foreign_net"),
                        row.get("trust_net"),
                        row.get("dealer_net"),
                    )
                insti_ok = True
            except (DataUnavailableError, requests.RequestException) as exc:
                print(f"    {symbol} MoneyDJ 三大法人取得失敗：{exc}")

            insti_by_symbol[symbol] = insti_map
            insti_ok_by_symbol[symbol] = insti_ok

            # 2) 月表：OHLCV + change（每月 1 發），用 MoneyDJ 日期當限流判準
            fetched = _prefetch_symbol_ohlcv(
                session=session,
                symbol=symbol,
                market_type=market_types.get(symbol),
                start=start,
                end=end,
                traded_dates=set(insti_map),
            )
            ohlcv_by_symbol[symbol] = fetched.by_date
            if fetched.market_type is not None:
                resolved[symbol] = fetched.market_type
            if fetched.failed_months:
                months = "、".join(f"{m:%Y-%m}" for m in fetched.failed_months)
                print(f"    ⚠ {symbol} 以下月份判定為取得失敗、未寫入：{months}")

        return cls(
            ohlcv_by_symbol=ohlcv_by_symbol,
            insti_by_symbol=insti_by_symbol,
            insti_ok_by_symbol=insti_ok_by_symbol,
            resolved_market_types=resolved,
        )

    def ohlcv(
        self, symbol: str, date: dt.date, market_type: str | None
    ) -> OhlcvResult:
        found = self._ohlcv.get(symbol, {}).get(date)
        if found is not None:
            return found
        # 該日不在月表（沒交易 / 停牌 / 該月判定取得失敗）→ 全 None，
        # `_build_daily_rows` 會因為無價格而跳過該檔該日。
        return OhlcvResult(
            open=None, close=None, high=None, low=None, volume=None, change=None
        )

    def insti(
        self, symbol: str, date: dt.date
    ) -> tuple[int | None, int | None, int | None]:
        return self._insti.get(symbol, {}).get(date, (None, None, None))

    def insti_ok(self, symbol: str, market_type: str | None) -> bool:
        """該檔的 MoneyDJ 是否取得成功。

        整段失敗時該檔**每一天**都不通過 gating，等同整檔跳過不寫——與現行
        「逐檔跳過半套資料」的不變量一致，留待重跑補上。
        """
        return self._insti_ok.get(symbol, False)

    def is_tpex(self, symbol: str, market_type: str | None) -> bool:
        """per-stock 模式下市場別是已知事實，直接用 stocks.market_type。

        不需要 batch 模式那個「靠當日 tpex_quotes 推市場別」的 workaround。
        """
        resolved = self.resolved_market_types.get(symbol) or market_type
        return resolved == "tpex"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/test_per_symbol_provider.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 跑全部單元測試**

Run: `pytest tests/unit/ -q`
Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add src/tw_stock_rawdata/run.py tests/unit/test_per_symbol_provider.py
git commit -m "feat: 新增 PerSymbolRangeProvider，per-stock 區間供應 OHLCV 與三大法人"
```

---

### Task 7: `--backfill-stocks` 接上 provider + README

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（`main()` 的 `--backfill-stocks` 分支，約 `:1911-1968`；`_run_for_date` 新增 `provider` 參數）
- Modify: `README.md`
- Test: `tests/unit/test_backfill_stocks_wiring.py`（新檔）

**Interfaces:**
- Consumes: `PerSymbolRangeProvider.build`（Task 6）、既有 `_run_for_date`
- Produces: `_run_for_date(..., provider: RowSourceProvider | None = None)` — 傳入 provider 時**跳過所有全市場批次 HTTP**，直接用它組列；不傳時維持現行行為（自行抓批次來源並建 `BatchSourceProvider`）。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_backfill_stocks_wiring.py`：

```python
"""Unit tests: --backfill-stocks 走 per-stock provider 時不打全市場批次來源。

這是整個改造的驗收點：回補 1 檔 N 天，T86 / MI_INDEX / TPEX quotes / TPEX 3insti
的呼叫次數必須是 0（改造前是每天各 1 次）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tw_stock_rawdata import run


@pytest.fixture
def batch_spies(monkeypatch):
    """把四個全市場批次 fetch 換成計數器：被呼叫就代表改造沒生效。"""
    calls = {"t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0}

    monkeypatch.setattr(
        run, "_fetch_twse_3insti",
        lambda *a, **k: calls.__setitem__("t86", calls["t86"] + 1)
        or pd.DataFrame(columns=["symbol", "foreign_net", "trust_net", "dealer_net"]),
    )
    monkeypatch.setattr(
        run, "fetch_twse_mi_index",
        lambda *a, **k: calls.__setitem__("mi_index", calls["mi_index"] + 1)
        or (pd.DataFrame(), None),
    )
    monkeypatch.setattr(
        run, "_fetch_tpex_sources",
        lambda *a, **k: calls.__setitem__("tpex_quotes", calls["tpex_quotes"] + 1)
        or (None, None, None, None),
    )
    return calls


class _StubProvider:
    """最小 provider：固定回一天有價格、三大法人 OK。"""

    def ohlcv(self, symbol, date, market_type):  # noqa: ANN001 - 測試替身
        return run.OhlcvResult(
            open=100.0, close=105.0, high=106.0, low=99.0,
            volume=1_234_000, change=5.0,
        )

    def insti(self, symbol, date):  # noqa: ANN001 - 測試替身
        return (9_040_000, -1_293_000, 1_612_000)

    def insti_ok(self, symbol, market_type):  # noqa: ANN001 - 測試替身
        return True

    def is_tpex(self, symbol, market_type):  # noqa: ANN001 - 測試替身
        return False


def test_run_for_date_with_provider_skips_batch_fetches(
    batch_spies, monkeypatch
) -> None:
    written: list = []
    monkeypatch.setattr(
        run, "upsert_daily_raw",
        lambda url, date, df: written.append((date, len(df))),
    )

    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is True
    assert written == [(dt.date(2025, 7, 31), 1)]
    assert batch_spies == {
        "t86": 0, "mi_index": 0, "tpex_quotes": 0, "tpex_3insti": 0
    }


def test_run_for_date_without_provider_still_fetches_batches(
    batch_spies, monkeypatch
) -> None:
    """不傳 provider 時維持現行行為（daily / 全市場 backfill 不受影響）。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    run._run_for_date(
        session=None,
        date=dt.date(2025, 7, 31),
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
    )

    assert batch_spies["mi_index"] >= 1


def test_run_for_date_with_provider_skips_weekend(batch_spies) -> None:
    """週末照舊直接跳過，provider 不改變這個判斷。"""
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    class _Config:
        database_url = "postgresql://stub"

    wrote = run._run_for_date(
        session=None,
        date=dt.date(2025, 8, 2),  # 週六
        holdings=holdings,
        sheet_names=set(),
        twse_month_cache={},
        config=_Config(),
        today=dt.date(2026, 8, 22),
        write_market_daily=False,
        # 一定要傳，否則 _run_for_date 會自己去打處置股名單（真實 HTTP）
        disposition=run.DispositionData(by_date={}, ok_markets=frozenset()),
        provider=_StubProvider(),
    )

    assert wrote is False


def test_per_symbol_and_batch_agree_on_same_row(monkeypatch) -> None:
    """同一天同一檔，兩種 provider 組出來的列必須一致（三大法人容許 ±1 張）。

    這是「單一組列路徑、不產生雙軌」的驗收點：差異只該來自來源精度，
    不該來自組列邏輯。
    """
    date = dt.date(2025, 7, 31)
    holdings = pd.DataFrame([{"symbol": "2330", "market_type": "twse"}])

    # batch 來源：交易所精確股數
    mi_index = pd.DataFrame([{
        "symbol": "2330", "name": "台積電", "open": 1250.0, "close": 1255.0,
        "high": 1260.0, "low": 1245.0, "volume": 24_000_000, "change": -5.0,
    }])
    twse_3insti = pd.DataFrame([{
        "symbol": "2330", "foreign_net": 9_039_647,
        "trust_net": -1_292_952, "dealer_net": 1_611_836,
    }])
    batch_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.BatchSourceProvider(
            session=None,
            twse_3insti=twse_3insti,
            twse_day_all=None,
            twse_mi_index=mi_index,
            tpex_quotes=pd.DataFrame(
                columns=["symbol", "name", "open", "close", "high", "low",
                         "volume", "change"]
            ),
            tpex_3insti=pd.DataFrame(columns=["symbol"]),
            twse_month_cache={},
        ),
    ).iloc[0]

    # per-symbol 來源：月表 + MoneyDJ（張級）
    monkeypatch.setattr(run, "fetch_twse_stock_day", lambda *a, **k: pd.DataFrame(
        [["114/07/31", "24,000,000", "30,000,000", "1250.00", "1260.00",
          "1245.00", "1255.00", "-5.00", "50,000", ""]],
        columns=["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價",
                 "收盤價", "漲跌價差", "成交筆數", "註記"],
    ))
    monkeypatch.setattr(run, "fetch_moneydj_holding_pct", lambda *a, **k: pd.DataFrame({
        "date": ["114/07/31"],
        "foreign_net_lots": ["9040"], "trust_net_lots": ["-1293"],
        "dealer_net_lots": ["1612"],
        "foreign_holding_pct": ["73.54%"], "insti_holding_pct": ["76.79%"],
    }))
    per_symbol_row = run._build_daily_rows(
        date=date,
        holdings=holdings,
        provider=run.PerSymbolRangeProvider.build(
            session=None, symbols=["2330"], market_types={"2330": "twse"},
            start=dt.date(2025, 7, 1), end=dt.date(2025, 7, 31),
        ),
    ).iloc[0]

    # 價格類完全相同
    for col in ("open", "high", "low", "close", "volume", "limit_up", "limit_down"):
        assert batch_row[col] == per_symbol_row[col], col

    # 三大法人容許 ±1 張（floor vs round）
    for col in ("foreign_net", "trust_net", "dealer_net"):
        assert abs(batch_row[col] - per_symbol_row[col]) <= 1, col


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_backfill_stocks_wiring.py -v`
Expected: FAIL — `TypeError: _run_for_date() got an unexpected keyword argument 'provider'`

- [ ] **Step 3: 讓 `_run_for_date` 接受 provider**

在 `_run_for_date` 簽名末端加入參數：

```python
    provider: RowSourceProvider | None = None,
```

在 docstring 補一段：

```
    provider: 已預取好的來源。傳入時**完全跳過所有全市場批次 HTTP**
    （T86 / MI_INDEX / TPEX quotes / TPEX 3insti）以及 twse_confirmed 判斷，
    直接用它組列——`--backfill-stocks` 走這條路，交易日由 provider 的月表資料
    決定（該日無價格就不寫該檔）。不傳時維持現行行為。
```

在「Skip weekends」與「Skip existing sheets」兩個區塊**之後**、
`# Fetch TWSE 3-institutional data` 之前，插入 per-stock 分支：

```python
    # per-stock 模式：來源已整段預取完畢，跳過所有全市場批次 HTTP。
    # 交易日不再靠 twse_confirmed 判定 —— 該檔該日有沒有交易由月表資料決定
    # （provider.ohlcv 回全 None → _build_daily_rows 因無價格跳過該列）。
    if provider is not None:
        if disposition is None:
            with _phase(f"{sheet_name} 處置股名單"):
                disposition = _fetch_disposition(session, date, date)

        with _phase(f"{sheet_name} 逐檔組列（{len(holdings)} 檔）"):
            output_df = _build_daily_rows(
                date=date,
                holdings=holdings,
                provider=provider,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
                disposition=disposition,
            )

        if output_df.empty or output_df["close"].isna().all():
            print(f"{sheet_name} 無可寫入資料（該日無交易或來源取得失敗）。")
            return False

        sheet_names.add(sheet_name)
        with _phase(f"{sheet_name} 寫入 stock_daily_raw（{len(output_df)} 列）"):
            upsert_daily_raw(config.database_url, date, output_df)

        # market_daily 與個股無關，per-stock 回補不動它（維持既有不變量）。
        return True
```

- [ ] **Step 4: 改 `main()` 的 `--backfill-stocks` 分支**

把該分支中「建 `stocks_holdings`」到「呼叫 `_run_for_date`」之間的程式碼改為：

```python
        # 市場別查 DB（CLI 只給代號）；查不到的留 None，由 provider 探測定調。
        market_types = load_market_types(db_url)
        start_date = _parse_date(args.backfill_start)
        end_date = _parse_date(args.backfill_end)
        backfill_dates = _build_date_range(start_date, end_date)
        print(
            f"回補特定股票 {','.join(stock_list)}"
            f" ({len(backfill_dates)} 天：{start_date} ~ {end_date})"
        )

        print("載入發行股數...")
        issued_shares = _get_issued_shares(session, config)
        print("載入股票名稱...")
        name_map = load_stock_names(db_url)

        margin_holdings = pd.DataFrame([
            {"symbol": s, "name": "", "market_type": market_types.get(s)}
            for s in stock_list
        ])
        margin_cache = _prefetch_margin_cache(
            session, margin_holdings, start_date, end_date
        )
        holding_pct_cache = _prefetch_holding_pct_cache(
            session, margin_holdings, start_date, end_date,
        )
        # 處置名單整段預取一次（兩市場各 1 次 HTTP），避免每天各打一次。
        with _phase("預取處置股名單"):
            disposition = _fetch_disposition(session, start_date, end_date)

        # per-stock 區間來源：OHLCV 每檔每月 1 發、三大法人每檔整段 1 發。
        # 取代原本「每個交易日 4 發全市場批次」的成本結構。
        with _phase("預取個股區間 OHLCV／三大法人"):
            provider = PerSymbolRangeProvider.build(
                session=session,
                symbols=stock_list,
                market_types=market_types,
                start=start_date,
                end=end_date,
            )

        # 用探測定調後的市場別組 holdings，讓處置註記與 gating 拿到正確市場別。
        stocks_holdings = pd.DataFrame([
            {
                "symbol": s,
                "name": name_map.get(s, ""),
                "market_type": provider.resolved_market_types.get(s)
                or market_types.get(s),
            }
            for s in stock_list
        ])

        sheet_names = set()  # 不需 dedup（force 模式忽略，沒 force 也沒 skip 邏輯）
        any_written = False
        for date in backfill_dates:
            if _run_for_date(
                session, date, stocks_holdings, sheet_names, twse_month_cache,
                config, today, skip_existing=False,
                issued_shares=issued_shares,
                margin_cache=margin_cache,
                holding_pct_cache=holding_pct_cache,
                name_map=name_map,
                write_market_daily=False,
                disposition=disposition,
                provider=provider,
            ):
                any_written = True
```

其後的 `_refresh_prev_day_margin` 區塊與 `return` **保持原樣不動**。

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/unit/test_backfill_stocks_wiring.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 跑全部單元測試**

Run: `pytest tests/unit/ -q`
Expected: 全綠

- [ ] **Step 7: 更新 README**

在 `README.md` 中 `--backfill-stocks` 的說明處，補上行為與成本描述：

```markdown
`--backfill-stocks` 走 **per-stock 區間抓取**，與其他回補模式的成本結構不同：

- OHLCV + 漲跌價差：每檔**每月 1 次**請求（上市走 TWSE `STOCK_DAY`、
  上櫃走 TPEX 個股月表 `afterTrading/tradingStock`）
- 三大法人：每檔**整段 1 次**請求（MoneyDJ `zcl`，與外資/法人持股佔比同一頁，
  故為零額外請求）
- 融資融券：每檔整段 1 次（MoneyDJ）
- 處置股名單：整段兩市場各 1 次

回補 1 檔 3 年約 **40 次請求**（一般日期區間回補是每個交易日 4 次全市場批次，
同樣範圍約 2,900 次），因此**不需要**分段執行或段間 sleep。

已知差異（設計時確認接受）：
- 三大法人與上櫃成交量來自「張」為單位的來源，與交易所股數經 `// 1000` 的結果
  最多差 1 張。
- 上櫃股**除權息日**的漲跌價差，此模式取得的是正確值（daily 模式的來源只給
  文字標記，寫 NULL）。搭配 upsert 的 `COALESCE`，回補過的日子資料較完整。
- `--backfill-stocks` 仍**不寫** `market_daily`。
```

同時確認 `README.md` 的資料來源清單有列入 TPEX 個股月表端點；若無則補上。

- [ ] **Step 8: Commit**

```bash
git add src/tw_stock_rawdata/run.py README.md tests/unit/test_backfill_stocks_wiring.py
git commit -m "feat: --backfill-stocks 改走 per-stock 區間抓取，請求數降至約 1/70"
```

---

## 完成後的手動驗證

單元測試不打網路，因此上線前用真實 API 跑一次小範圍確認。**依 `memory/twse-rate-limit-ambiguous-response.md`，不要短時間重複執行。**

```bash
# 單一上市股、一個月 —— 預期約 1(月表) + 1(MoneyDJ zcl) + 1(MoneyDJ 融資券) + 2(處置) 次請求
tw-stock-rawdata --backfill-stocks 2330 \
  --backfill-start 2025-07-01 --backfill-end 2025-07-31

# 單一上櫃股，含 2025-07-16 除息日 —— 確認 limit_up/limit_down 有值
tw-stock-rawdata --backfill-stocks 6488 \
  --backfill-start 2025-07-01 --backfill-end 2025-07-31
```

用 SQL 抽驗（對照本計畫記錄的實測值）：

```sql
SELECT symbol, trade_date, open, high, low, close, volume,
       foreign_net, trust_net, dealer_net, limit_up, limit_down
FROM stock_daily_raw
WHERE (symbol, trade_date) IN (('2330','2025-07-31'), ('6488','2025-07-16'));
```

預期：
- `2330 / 2025-07-31`：`foreign_net = 9040`、`trust_net = -1293`、`dealer_net = 1612`
  （T86 的 `// 1000` 是 9039 / -1293 / 1611，差 1 張屬已接受範圍）
- `6488 / 2025-07-16`：`close = 322.50`、`volume = 3709`、
  `limit_up` / `limit_down` **有值**（參考價 304.00）
