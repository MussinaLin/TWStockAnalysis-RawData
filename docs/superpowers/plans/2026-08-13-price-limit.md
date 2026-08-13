# 漲停 / 跌停價欄位 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `stock_daily_raw` 新增 `limit_up` / `limit_down` 兩欄，讓下游 `TWStockAnalysis` 能以 `close = limit_up` 直接判定漲停跌停。

**Architecture:** 新增純函式模組 `price_limit.py` 負責升降單位級距與 ±10% 換算；`prepare.py` 讓 MI_INDEX 與 TPEX quotes 多吐一個 `change`（漲跌價差）欄；`run.py` 以 `參考價 = 收盤 − 漲跌價差` 呼叫換算並寫入。另加 `--backfill-limits` 專用回補指令，只打兩個批量行情 API 並以 `UPDATE` 補既有 row。

**Tech Stack:** Python >= 3.13、pandas、psycopg 3、pytest。金額運算一律 `decimal.Decimal`。

規格書：`docs/superpowers/specs/2026-08-13-price-limit-design.md`

## Global Constraints

- Python `>=3.13`；所有新檔案開頭加 `from __future__ import annotations`。
- ruff `line-length = 100`。
- 測試放 `tests/unit/`，執行 `pytest tests/unit/`。
- 金額計算一律用 `Decimal`，禁止 `float` 算術。`float` → `Decimal` 一律走 `Decimal(str(x))`，不可 `Decimal(x)`。
- **`change`（漲跌價差）只能取自 `MI_INDEX` 與 TPEX quotes，永遠不可取自 `STOCK_DAY_ALL`**（它在除權息日給 `0.0000` 且無標記）。
- 推不出參考價時 `limit_up` / `limit_down` 一律寫 `None`，不以前一交易日收盤價推測。
- commit message 不加任何 `Co-Authored-By:` trailer。
- 本 repo 只寫 raw data，不做技術指標 / 選股 / 通知。

---

### Task 1: `price_limit.py` — 升降單位與漲跌停換算

**Files:**
- Create: `src/tw_stock_rawdata/price_limit.py`
- Test: `tests/unit/test_price_limit.py`

**Interfaces:**
- Consumes: 無（純函式，不依賴專案其他模組）
- Produces:
  - `tick_size(price: Decimal) -> Decimal`
  - `calc_limits(ref: Decimal | None) -> tuple[Decimal | None, Decimal | None]`（回傳 `(漲停價, 跌停價)`）

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/test_price_limit.py`：

```python
"""Unit tests for price_limit — 台股升降單位級距與漲跌停換算。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tw_stock_rawdata.price_limit import calc_limits, tick_size


class TestTickSize:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.01"),
            ("9.99", "0.01"),
            ("10", "0.05"),
            ("49.95", "0.05"),
            ("50", "0.1"),
            ("99.9", "0.1"),
            ("100", "0.5"),
            ("499.5", "0.5"),
            ("500", "1"),
            ("999", "1"),
            ("1000", "5"),
            ("2656.5", "5"),
        ],
    )
    def test_band_boundaries(self, price: str, expected: str) -> None:
        assert tick_size(Decimal(price)) == Decimal(expected)


class TestCalcLimits:
    def test_limit_up_floors_to_tick(self) -> None:
        # 2415 × 1.1 = 2656.5；1000 元以上檔位 5 → 無條件捨去 → 2655
        up, _ = calc_limits(Decimal("2415"))
        assert up == Decimal("2655")

    def test_limit_down_ceils_to_tick(self) -> None:
        # 2415 × 0.9 = 2173.5；檔位 5 → 無條件進位 → 2175
        _, down = calc_limits(Decimal("2415"))
        assert down == Decimal("2175")

    def test_regression_3605_ex_rights_day(self) -> None:
        """3605 宏致 2026-08-13：除息後參考價 120，當日實際收 132.00 漲停。"""
        up, down = calc_limits(Decimal("120"))
        assert up == Decimal("132.0")
        assert down == Decimal("108.0")

    def test_crosses_band_upward(self) -> None:
        # 9.5 × 1.1 = 10.45，落在 10~50 區間（檔位 0.05）而非參考價所屬的 0.01 檔
        up, _ = calc_limits(Decimal("9.5"))
        assert up == Decimal("10.45")

    def test_none_ref_returns_none_pair(self) -> None:
        assert calc_limits(None) == (None, None)

    def test_returns_decimal_not_float(self) -> None:
        up, down = calc_limits(Decimal("2415"))
        assert isinstance(up, Decimal)
        assert isinstance(down, Decimal)

    @pytest.mark.parametrize(
        "ref", ["9.5", "10", "45.2", "66.1", "120", "499", "500", "999", "2415"]
    )
    def test_limits_land_on_tick_multiples(self, ref: str) -> None:
        up, down = calc_limits(Decimal(ref))
        assert up % tick_size(up) == 0
        assert down % tick_size(down) == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_price_limit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tw_stock_rawdata.price_limit'`

- [ ] **Step 3: 寫最小實作**

建立 `src/tw_stock_rawdata/price_limit.py`：

```python
"""台股漲跌停價計算（升降單位級距 + ±10% 限制）。

參考價 × 1.1 無條件捨去到升降單位 = 漲停價；× 0.9 無條件進位 = 跌停價。
級距由「算出來的漲/跌停價」決定，不是由參考價決定。

只涵蓋普通股（含 KY、TDR）的升降單位。本 repo 的標的池來自上市 / 上櫃公司
基本資料，不含 ETF，故不需要 ETF 的級距分支。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

# (區間上界, 升降單位)：價格 < 上界即適用該單位；皆不符者用 _TICK_TOP。
_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
)
_TICK_TOP = Decimal("5")

_LIMIT_UP_RATE = Decimal("1.1")
_LIMIT_DOWN_RATE = Decimal("0.9")


def tick_size(price: Decimal) -> Decimal:
    """該價位適用的升降單位（普通股）。"""
    for upper, tick in _TICK_BANDS:
        if price < upper:
            return tick
    return _TICK_TOP


def calc_limits(ref: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """由參考價算出 (漲停價, 跌停價)。

    ref 為 None（除權息日 / 無成交，推不出參考價）時回傳 (None, None)，不做推測。
    """
    if ref is None:
        return None, None

    up_raw = ref * _LIMIT_UP_RATE
    up_tick = tick_size(up_raw)
    limit_up = (up_raw / up_tick).to_integral_value(rounding=ROUND_FLOOR) * up_tick

    down_raw = ref * _LIMIT_DOWN_RATE
    down_tick = tick_size(down_raw)
    limit_down = (down_raw / down_tick).to_integral_value(rounding=ROUND_CEILING) * down_tick

    return limit_up, limit_down
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/test_price_limit.py -v`
Expected: PASS（20 passed）

- [ ] **Step 5: Commit**

```bash
git add src/tw_stock_rawdata/price_limit.py tests/unit/test_price_limit.py
git commit -m "feat: 新增 price_limit 模組計算台股漲跌停價"
```

---

### Task 2: `prepare.py` — 三個行情來源吐出 `change` 欄

**Files:**
- Modify: `src/tw_stock_rawdata/prepare.py`（`prepare_tpex_quotes` / `prepare_twse_day_all` / `prepare_twse_mi_index`，新增 `_merge_change_sign`）
- Test: `tests/unit/test_prepare_change.py`

**Interfaces:**
- Consumes: `sources._clean_number`（已存在；對 `'X0.00'` / `'除息'` / `'---'` 皆回 `None`）
- Produces:
  - `prepare._merge_change_sign(sign_text, magnitude) -> float | None`
  - 上述三個 `prepare_*` 函式的回傳 DataFrame 都多一個 `change` 欄

**背景（實作者必讀）：** 三個來源的漲跌價差格式不同 ——
`STOCK_DAY_ALL` 的 `Change` 已帶正負號（`"20.0000"` / `"-5.0000"`）；
TPEX 的 `漲跌` 是 `"+0.46"`，除權息日直接放中文 `"除息"` / `"除權息"`；
`MI_INDEX` 的 `漲跌價差` 是絕對值，正負號在獨立欄位 `漲跌(+/-)`，
值為 HTML（`<p style= color:red>+</p>`），去標籤後只有 `+` / `-` / 空字串 / `X` 四種，
`X` 代表該檔當日除權息。

**注意 `_find_column` 是子字串比對**：MI_INDEX 同時有 `漲跌(+/-)` 與 `漲跌價差` 兩欄，
`change` 的關鍵字必須用 `漲跌價差`（不可用 `漲跌`，會先命中 `漲跌(+/-)`）。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/test_prepare_change.py`：

```python
"""Unit tests for 漲跌價差（change）欄位的 normalize。

三個來源格式不同：STOCK_DAY_ALL 自帶符號、TPEX 除權息日給中文字串、
MI_INDEX 符號在獨立 HTML 欄位且以 X 標記除權息。
"""

from __future__ import annotations

import pandas as pd

from tw_stock_rawdata.prepare import (
    _merge_change_sign,
    prepare_tpex_quotes,
    prepare_twse_day_all,
    prepare_twse_mi_index,
)


class TestMergeChangeSign:
    def test_positive_sign(self) -> None:
        assert _merge_change_sign("<p style= color:red>+</p>", "20.00") == 20.0

    def test_negative_sign(self) -> None:
        assert _merge_change_sign("<p style= color:green>-</p>", "5.00") == -5.0

    def test_ex_rights_marker_returns_none(self) -> None:
        """X = 該檔當日除權息，交易所未提供相對前日的漲跌 → 不推測。"""
        assert _merge_change_sign("<p>X</p>", "0.00") is None

    def test_empty_sign_is_flat(self) -> None:
        assert _merge_change_sign("<p></p>", "0.00") == 0.0

    def test_unparseable_magnitude_returns_none(self) -> None:
        assert _merge_change_sign("<p></p>", "--") is None


class TestPrepareMiIndexChange:
    def test_merges_sign_column(self) -> None:
        df = pd.DataFrame(
            [
                {
                    "證券代號": "2330", "證券名稱": "台積電",
                    "開盤價": "2,405.00", "最高價": "2,415.00",
                    "最低價": "2,390.00", "收盤價": "2,415.00",
                    "漲跌(+/-)": "<p style= color:red>+</p>", "漲跌價差": "20.00",
                    "成交股數": "19,448,153",
                },
                {
                    "證券代號": "3605", "證券名稱": "宏致",
                    "開盤價": "111.00", "最高價": "120.00",
                    "最低價": "111.00", "收盤價": "120.00",
                    "漲跌(+/-)": "<p>X</p>", "漲跌價差": "0.00",
                    "成交股數": "8,422,797",
                },
            ]
        )
        out = prepare_twse_mi_index(df)
        assert out.loc[out["symbol"] == "2330", "change"].iloc[0] == 20.0
        assert out.loc[out["symbol"] == "3605", "change"].isna().iloc[0]

    def test_sign_column_is_dropped(self) -> None:
        df = pd.DataFrame(
            [{
                "證券代號": "2330", "證券名稱": "台積電",
                "開盤價": "2,405.00", "最高價": "2,415.00",
                "最低價": "2,390.00", "收盤價": "2,415.00",
                "漲跌(+/-)": "<p style= color:red>+</p>", "漲跌價差": "20.00",
                "成交股數": "19,448,153",
            }]
        )
        assert "change_sign" not in prepare_twse_mi_index(df).columns


class TestPrepareTpexQuotesChange:
    def test_signed_change(self) -> None:
        df = pd.DataFrame(
            [{
                "代號": "6488", "名稱": "環球晶", "收盤": "45.21", "漲跌": "+1.41",
                "開盤": "44.20", "最高": "45.26", "最低": "44.20", "成交股數": "508,551",
            }]
        )
        assert prepare_tpex_quotes(df)["change"].iloc[0] == 1.41

    def test_ex_dividend_chinese_marker_returns_na(self) -> None:
        """TPEX 除權息日的漲跌欄直接放中文字串，不是數字。"""
        df = pd.DataFrame(
            [{
                "代號": "5903", "名稱": "全家", "收盤": "184.00", "漲跌": "除息 ",
                "開盤": "183.00", "最高": "185.00", "最低": "182.00", "成交股數": "100,000",
            }]
        )
        assert prepare_tpex_quotes(df)["change"].isna().iloc[0]


class TestPrepareDayAllChange:
    def test_signed_change(self) -> None:
        df = pd.DataFrame(
            [{
                "Code": "2330", "Name": "台積電", "TradeVolume": "19448153",
                "OpeningPrice": "2405.00", "HighestPrice": "2415.00",
                "LowestPrice": "2390.00", "ClosingPrice": "2415.00",
                "Change": "20.0000",
            }]
        )
        assert prepare_twse_day_all(df)["change"].iloc[0] == 20.0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_prepare_change.py -v`
Expected: FAIL — `ImportError: cannot import name '_merge_change_sign' from 'tw_stock_rawdata.prepare'`

- [ ] **Step 3: 寫最小實作**

在 `src/tw_stock_rawdata/prepare.py` 的 `_extract_standard_columns` 之後、
`prepare_tpex_quotes` 之前，新增：

```python
def _merge_change_sign(sign_text, magnitude) -> float | None:
    """把 MI_INDEX 分離的正負號欄併回漲跌價差。

    `漲跌(+/-)` 欄的值是 HTML，去標籤後只有 `+` / `-` / 空字串 / `X` 四種。
    `X` 表該檔當日除權息，交易所未提供相對前一交易日的漲跌，推不出參考價 → 回 None。
    """
    value = _clean_number(magnitude)
    if value is None:
        return None
    sign = re.sub(r"<[^>]*>", "", str(sign_text or "")).strip()
    if sign.upper() == "X":
        return None
    return -value if sign == "-" else value
```

`prepare_tpex_quotes`：`_find_columns` 的 spec 加一行

```python
        "change": [["漲跌"]],
```

並在 `temp["volume"]` 的處理區塊之後、`return temp` 之前加：

```python
    if "change" in temp.columns:
        temp["change"] = temp["change"].map(_clean_number)
    else:
        temp["change"] = None
```

`prepare_twse_day_all`：`_find_columns` 的 spec 加一行

```python
        "change": [["change"], ["漲跌價差"], ["漲跌"]],
```

並在 `return temp` 之前加上與 `prepare_tpex_quotes` 完全相同的那段 `change` 處理。

`prepare_twse_mi_index`：`_find_columns` 的 spec 加兩行（`change` 只能用
`漲跌價差` 當關鍵字，用 `漲跌` 會先命中 `漲跌(+/-)`）：

```python
        "change": [["漲跌價差"]],
        "change_sign": [["漲跌(+/-)"]],
```

並在 `return temp` 之前加：

```python
    if "change" in temp.columns:
        signs = (
            temp["change_sign"] if "change_sign" in temp.columns else [None] * len(temp)
        )
        temp["change"] = [
            _merge_change_sign(sign, value)
            for sign, value in zip(signs, temp["change"])
        ]
    else:
        temp["change"] = None
    temp = temp.drop(columns=["change_sign"], errors="ignore")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/test_prepare_change.py tests/unit/test_sources_parsing.py -v`
Expected: PASS（新測試全過，且既有 parsing 測試未被破壞）

- [ ] **Step 5: Commit**

```bash
git add src/tw_stock_rawdata/prepare.py tests/unit/test_prepare_change.py
git commit -m "feat: MI_INDEX / TPEX / STOCK_DAY_ALL 解析出漲跌價差欄"
```

---

### Task 3: `_fetch_ohlcv_with_fallback` 改 NamedTuple 並帶回 `change`

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（`_fetch_ohlcv_with_fallback` 及其在 `_build_daily_rows` 的呼叫端）
- Modify: `CLAUDE.md`（Gotchas 章節）
- Test: `tests/unit/test_run_ohlcv_change.py`

**Interfaces:**
- Consumes: `prepare.*` 產出的含 `change` 欄 DataFrame（Task 2）
- Produces:
  - `run.OhlcvResult`：`NamedTuple`，欄位依序 `open` / `close` / `high` / `low` / `volume` / `change`
  - `run._fetch_ohlcv_with_fallback(...) -> OhlcvResult`（參數簽章不變）

**背景（實作者必讀）：** `change` **不跟隨 OHLCV 的逐欄補洞**，只從 `MI_INDEX`
與 TPEX quotes 兩個批量來源取，且**必須跳過 `STOCK_DAY_ALL`** —— 它在除權息日
給 `Change = 0.0000` 且不帶任何標記，而它是 OHLCV fallback 的第一順位，
若讓它供應 change，除權息日會算出「參考價 = 收盤」這種看似合理的錯值。

另有一個效能陷阱：現有 `STOCK_DAY`（逐檔月表）區塊的觸發條件是
`any(v is None for v in [open, close, high, low, volume])`，那是**逐檔 HTTP 請求**。
**絕對不可以把 `change` 加進這個條件**，否則每檔都會多打一次 API。
`change` 的取得要寫成獨立區塊，不受 OHLCV 是否齊全影響。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/test_run_ohlcv_change.py`：

```python
"""Unit tests for _fetch_ohlcv_with_fallback 的 change 來源規則。

change 只從 MI_INDEX / TPEX quotes 取，永遠不從 STOCK_DAY_ALL 取
（後者在除權息日給 0.0000 且無標記）。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from tw_stock_rawdata import run

DATE = dt.date(2026, 8, 12)


def _day_all(symbol: str, close: float, change: float) -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "symbol": symbol, "name": "宏致", "open": 111.0, "close": close,
            "high": 120.0, "low": 111.0, "volume": 8422797, "change": change,
        }]
    )


def _mi_index(symbol: str, close: float, change: float | None) -> pd.DataFrame:
    return pd.DataFrame(
        [{
            "symbol": symbol, "name": "宏致", "open": 111.0, "close": close,
            "high": 120.0, "low": 111.0, "volume": 8422797, "change": change,
        }]
    )


def _empty_tpex() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "name", "open", "close", "high", "low", "volume", "change"]
    )


def test_change_is_never_taken_from_stock_day_all() -> None:
    """day_all 有 change 欄但 MI_INDEX 缺席 → change 必須是 None，close 仍取自 day_all。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="3605",
        twse_day_all=_day_all("3605", 120.0, 0.0),
        twse_mi_index=None,
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert result.close == 120.0
    assert result.change is None


def test_change_comes_from_mi_index() -> None:
    """close 取自 day_all、change 取自 MI_INDEX —— 跨來源組合是刻意且安全的。"""
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=_day_all("2330", 2415.0, 0.0),
        twse_mi_index=_mi_index("2330", 2415.0, 20.0),
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert result.close == 2415.0
    assert result.change == 20.0


def test_change_from_tpex_quotes() -> None:
    tpex = pd.DataFrame(
        [{
            "symbol": "6488", "name": "環球晶", "open": 44.2, "close": 45.21,
            "high": 45.26, "low": 44.2, "volume": 508551, "change": 1.41,
        }]
    )
    # day_all 與 mi_index 皆缺 → 會進 STOCK_DAY 逐檔月表區塊。預先塞 cache 讓它
    # 不會用 session=None 發 HTTP（該區塊只 catch DataUnavailableError）。
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="6488",
        twse_day_all=None, twse_mi_index=None,
        tpex_quotes=tpex,
        twse_month_cache={("6488", dt.date(2026, 8, 1)): pd.DataFrame()},
    )
    assert result.close == 45.21
    assert result.change == 1.41


def test_result_is_named_tuple() -> None:
    result = run._fetch_ohlcv_with_fallback(
        session=None, date=DATE, symbol="2330",
        twse_day_all=_day_all("2330", 2415.0, 0.0),
        twse_mi_index=_mi_index("2330", 2415.0, 20.0),
        tpex_quotes=_empty_tpex(),
        twse_month_cache={},
    )
    assert isinstance(result, run.OhlcvResult)
    assert result.open == 111.0
    assert result.high == 120.0
    assert result.low == 111.0
    assert result.volume == 8422797
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_run_ohlcv_change.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.run' has no attribute 'OhlcvResult'`

- [ ] **Step 3: 寫最小實作**

`src/tw_stock_rawdata/run.py` 頂端 import 區加入：

```python
from typing import NamedTuple
```

在 `_fetch_ohlcv_with_fallback` 定義之前加入：

```python
class OhlcvResult(NamedTuple):
    """單檔單日的 OHLCV 與漲跌價差。

    change 有自己的來源規則（見 _fetch_ohlcv_with_fallback），不跟隨 OHLCV 補洞。
    """

    open: float | None
    close: float | None
    high: float | None
    low: float | None
    volume: int | None
    change: float | None
```

把 `_fetch_ohlcv_with_fallback` 的回傳型別標註改成 `-> OhlcvResult`，
函式開頭的初始化改成：

```python
    open_price = close_price = high_price = low_price = volume = None
    change = None
```

`# Try TWSE STOCK_DAY_ALL` 區塊**完全不動**（不從中取 change），並在該區塊上方補註解：

```python
    # 注意：change 刻意不從 STOCK_DAY_ALL 取 —— 它在除權息日給 Change=0.0000
    # 且不帶任何標記，會算出「參考價 = 收盤」的錯值。change 只從 MI_INDEX /
    # TPEX quotes 取，見本函式末尾的獨立區塊。
```

`# Try TWSE STOCK_DAY`、`# Try TWSE MI_INDEX`、`# Try TPEX quotes` 三個區塊
**維持原樣**（其 `any(v is None ...)` 觸發條件不可加入 `change`）。

在 `# Try TPEX quotes` 區塊之後、`return` 之前，新增獨立的 change 區塊：

```python
    # change（漲跌價差）獨立取得：不受 OHLCV 是否齊全影響，也不觸發逐檔 HTTP。
    # MI_INDEX 涵蓋全部上市、TPEX quotes 涵蓋全部上櫃，兩者在 _run_for_date 都是
    # 每日必抓；兩邊都沒有時留 None，由呼叫端寫成 NULL（不以前日收盤推測）。
    if change is None and twse_mi_index is not None:
        row = twse_mi_index.loc[twse_mi_index["symbol"] == symbol]
        if not row.empty:
            change = row.iloc[0].get("change")
    if change is None and not tpex_quotes.empty:
        row = tpex_quotes.loc[tpex_quotes["symbol"] == symbol]
        if not row.empty:
            change = row.iloc[0].get("change")

    return OhlcvResult(
        open=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume=volume,
        change=change,
    )
```

並刪除原本結尾的 `return open_price, close_price, high_price, low_price, volume`。

`_build_daily_rows` 中的呼叫端由

```python
        open_price, close_price, high_price, low_price, volume = ohlcv
```

改為

```python
        open_price = ohlcv.open
        close_price = ohlcv.close
        high_price = ohlcv.high
        low_price = ohlcv.low
        volume = ohlcv.volume
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/ -v`
Expected: PASS（新測試全過，既有測試無回歸）

- [ ] **Step 5: 更新 CLAUDE.md Gotchas**

在 `CLAUDE.md` 的 `## Gotchas` 章節末尾加入：

```markdown
- **`change` 不可取自 `STOCK_DAY_ALL`**：漲跌價差只從 `MI_INDEX`（上市）與 TPEX
  quotes（上櫃）取。`STOCK_DAY_ALL` 在除權息日給 `Change=0.0000` 且無任何標記，
  而它是 `_fetch_ohlcv_with_fallback` 的第一順位；若讓它供應 change，除權息日會
  算出「參考價 = 收盤」的錯誤漲跌停價。另：change 的取得寫成獨立區塊，**不可**加進
  `STOCK_DAY` 逐檔月表區塊的 `any(v is None ...)` 觸發條件（那是逐檔 HTTP，
  加進去會讓每檔都多打一次 API）。
```

- [ ] **Step 6: Commit**

```bash
git add src/tw_stock_rawdata/run.py CLAUDE.md tests/unit/test_run_ohlcv_change.py
git commit -m "refactor: _fetch_ohlcv_with_fallback 改回傳 NamedTuple 並帶回漲跌價差"
```

---

### Task 4: DB schema 與欄位清單新增 `limit_up` / `limit_down`

**Files:**
- Modify: `src/tw_stock_rawdata/db.py`（`_SCHEMA_SQL`）
- Modify: `src/tw_stock_rawdata/db_utils.py`（`_RAW_COLUMNS` / `_RAW_DF_COLS`）
- Modify: `README.md`（資料表章節）
- Test: `tests/unit/test_db_utils_upsert_raw.py`（既有檔案，新增測試）

**Interfaces:**
- Consumes: 無
- Produces: `stock_daily_raw` 多兩欄 `limit_up` / `limit_down`（`NUMERIC(12,2)`），
  且 `db_utils._RAW_COLUMNS` / `_RAW_DF_COLS` 包含這兩個名稱

- [ ] **Step 1: 寫失敗的測試**

在 `tests/unit/test_db_utils_upsert_raw.py` 末尾追加：

```python
def test_raw_columns_include_price_limits() -> None:
    """limit_up / limit_down 必須同時進 _RAW_COLUMNS 與 _RAW_DF_COLS，
    否則 upsert 的欄位數與 placeholder 數會對不上。"""
    assert "limit_up" in db_utils._RAW_COLUMNS
    assert "limit_down" in db_utils._RAW_COLUMNS
    assert "limit_up" in db_utils._RAW_DF_COLS
    assert "limit_down" in db_utils._RAW_DF_COLS


def test_raw_column_lists_stay_in_sync() -> None:
    """_RAW_DF_COLS 是 _RAW_COLUMNS 去掉 trade_date 後的同序清單。"""
    assert db_utils._RAW_DF_COLS == [
        c for c in db_utils._RAW_COLUMNS if c != "trade_date"
    ]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_db_utils_upsert_raw.py -v`
Expected: FAIL — `assert 'limit_up' in [...]`

- [ ] **Step 3: 寫最小實作**

`src/tw_stock_rawdata/db.py` 的 `_SCHEMA_SQL` 中，`stock_daily_raw` 的
`insti_holding_pct` 之後、`created_time` 之前插入兩欄：

```sql
    limit_up                    NUMERIC(12,2),
    limit_down                  NUMERIC(12,2),
```

並在 `stock_holder_percent` 的 `ALTER TABLE` 之後加上既有資料庫的線上 migration：

```sql
-- 既有資料庫的線上 migration：補上後加的漲跌停欄（idempotent）。
ALTER TABLE stock_daily_raw ADD COLUMN IF NOT EXISTS limit_up   NUMERIC(12,2);
ALTER TABLE stock_daily_raw ADD COLUMN IF NOT EXISTS limit_down NUMERIC(12,2);
```

`src/tw_stock_rawdata/db_utils.py`：`_RAW_COLUMNS` 與 `_RAW_DF_COLS` 兩份清單
的 `"insti_holding_pct"` 之後各加 `"limit_up", "limit_down",`。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/test_db_utils_upsert_raw.py -v`
Expected: PASS

- [ ] **Step 5: 更新 README**

`README.md` 目前沒有資料表 / 欄位章節（只有 職責邊界 / 安裝 / 設定 / CLI 指令 /
休市開關 / Docker / 測試）。在 `### 休市開關（config.is_trading_day）` **之前**
新增一個同層級小節：

```markdown
### 漲跌停價（limit_up / limit_down）

`stock_daily_raw` 的 `limit_up` / `limit_down` 記錄該檔該日的漲停價與跌停價，
下游可直接以 `close = limit_up` 判定收盤漲停、`high = limit_up` 判定盤中曾觸及。

- 計算方式：`參考價 = 收盤價 − 漲跌價差`；漲停 = 參考價 × 1.1 無條件捨去到升降單位，
  跌停 = 參考價 × 0.9 無條件進位到升降單位。
- **`NULL` 表該日推不出參考價**（除權息日交易所不提供漲跌價差，或該檔無成交），
  下游應跳過該檔該日的漲跌停判定，**不要**自行用前一交易日收盤價推算 —— 除權息日
  的正確基準是除權息參考價，用前日收盤會算出看不出來的錯值。
```

- [ ] **Step 6: Commit**

```bash
git add src/tw_stock_rawdata/db.py src/tw_stock_rawdata/db_utils.py README.md tests/unit/test_db_utils_upsert_raw.py
git commit -m "feat: stock_daily_raw 新增 limit_up / limit_down 欄位"
```

---

### Task 5: 每日路徑計算並寫入漲跌停

**Files:**
- Modify: `src/tw_stock_rawdata/run.py`（新增 `_reference_price`，`_build_daily_rows` 寫入兩欄）
- Test: `tests/unit/test_run_reference_price.py`

**Interfaces:**
- Consumes: `price_limit.calc_limits`（Task 1）、`run.OhlcvResult.change`（Task 3）、
  `db_utils._RAW_DF_COLS` 已含兩欄（Task 4）
- Produces: `run._reference_price(close, change) -> Decimal | None`；
  `_build_daily_rows` 產出的 row dict 多 `limit_up` / `limit_down` 兩個 key

**背景（實作者必讀）：** `_clean_number` 回的是 Python `float`，但這些值放進 pandas
欄位後，含 `None` 的欄會被推斷成 `float64`，**`None` 會變成 `NaN`**。所以
`_reference_price` 必須同時擋 `None` 與 `NaN`，只擋 `None` 會讓 `Decimal(str(nan))`
產生 `Decimal('NaN')` 並一路靜默傳下去。

`float` → `Decimal` 一律 `Decimal(str(x))`：`Decimal(2415.0)` 會把浮點誤差整包帶進來。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/test_run_reference_price.py`：

```python
"""Unit tests for _reference_price — 由收盤價與漲跌價差推當日參考價。"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from tw_stock_rawdata import run


class TestReferencePrice:
    def test_positive_change(self) -> None:
        # 2330 2026-08-12：收 2415.00、漲跌 +20.00 → 參考價 2395
        assert run._reference_price(2415.0, 20.0) == Decimal("2395")

    def test_negative_change(self) -> None:
        assert run._reference_price(100.0, -5.0) == Decimal("105")

    def test_none_change_returns_none(self) -> None:
        """除權息日：交易所未提供漲跌 → 不以前一交易日收盤推測。"""
        assert run._reference_price(120.0, None) is None

    def test_none_close_returns_none(self) -> None:
        assert run._reference_price(None, 1.0) is None

    def test_nan_is_rejected(self) -> None:
        """pandas 會把含 None 的 float 欄位轉成 NaN，必須擋掉。"""
        nan = float("nan")
        assert run._reference_price(120.0, nan) is None
        assert run._reference_price(nan, 1.0) is None

    def test_returns_decimal_without_float_error(self) -> None:
        result = run._reference_price(0.3, 0.1)
        assert isinstance(result, Decimal)
        assert result == Decimal("0.2")

    def test_accepts_numpy_scalars(self) -> None:
        """pandas 取值回來的是 numpy 純量，不是 Python float。"""
        row = pd.Series({"close": 2415.0, "change": 20.0})
        assert run._reference_price(row["close"], row["change"]) == Decimal("2395")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_run_reference_price.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.run' has no attribute '_reference_price'`

- [ ] **Step 3: 寫最小實作**

`src/tw_stock_rawdata/run.py` 頂端 import 區加入：

```python
from decimal import Decimal
```

以及：

```python
from .price_limit import calc_limits
```

在 `OhlcvResult` 定義之後新增：

```python
def _reference_price(close, change) -> Decimal | None:
    """由收盤價與漲跌價差推當日參考價；任一缺值即回 None，不以前日收盤推測。

    pandas 會把含 None 的 float 欄位轉成 NaN，故 None 與 NaN 都要擋。
    轉換一律走 Decimal(str(x))：Decimal(2415.0) 會把 float 誤差整包帶進來。
    """
    if close is None or change is None:
        return None
    if pd.isna(close) or pd.isna(change):
        return None
    return Decimal(str(close)) - Decimal(str(change))
```

在 `_build_daily_rows` 中，取完 `ohlcv` 各欄之後（`volume_lots` 計算之前）加入：

```python
        limit_up, limit_down = calc_limits(_reference_price(close_price, ohlcv.change))
```

並在 `rows.append({...})` 的 `"insti_holding_pct"` 之後加入兩個 key：

```python
            "limit_up": limit_up,
            "limit_down": limit_down,
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/ -v`
Expected: PASS（全部測試通過）

- [ ] **Step 5: Commit**

```bash
git add src/tw_stock_rawdata/run.py tests/unit/test_run_reference_price.py
git commit -m "feat: 每日抓取計算並寫入 limit_up / limit_down"
```

---

### Task 6: `--backfill-limits` 專用回補指令

**Files:**
- Modify: `src/tw_stock_rawdata/db_utils.py`（新增 `update_price_limits_batch`）
- Modify: `src/tw_stock_rawdata/run.py`（`_parse_args` / `_is_daily_mode` / `_main_inner`，新增 `_collect_limit_updates` 與 `_backfill_limits_command`）
- Modify: `README.md`（CLI 章節）
- Test: `tests/unit/test_backfill_limits.py`

**Interfaces:**
- Consumes: `run._reference_price`（Task 5）、`price_limit.calc_limits`（Task 1）、
  `prepare_twse_mi_index` / `prepare_tpex_quotes` 已含 `change` 欄（Task 2）
- Produces:
  - `db_utils.update_price_limits_batch(database_url, updates) -> int`，
    `updates` 為 `list[tuple[str, dt.date, Decimal, Decimal]]`，順序 `(symbol, trade_date, limit_up, limit_down)`
  - `run._collect_limit_updates(session, date) -> list[tuple[str, dt.date, Decimal, Decimal]]`

**背景（實作者必讀）：** 這裡**必須用 `UPDATE`，不可用 `upsert_daily_raw`**。
回補只有這兩欄有值，走 upsert 會 `INSERT` 出一批其餘欄位全 NULL 的半套 row，
正是 CLAUDE.md「逐檔跳過半套資料」要防的情況。

`STOCK_DAY_ALL` 無視 `date` 參數（永遠回最後交易日），故回補只能用
`fetch_twse_mi_index(session, date)` 與 `fetch_tpex_daily_quotes_v2(session, date)`。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/unit/test_backfill_limits.py`：

```python
"""Unit tests for --backfill-limits 的資料收集與批次 UPDATE。"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from tw_stock_rawdata import db_utils, run

DATE = dt.date(2026, 8, 12)


class _FakeCursor:
    def __init__(self, rowcounts: list[int]):
        self._rowcounts = list(rowcounts)
        self.rowcount = 0
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self._rowcounts:
            self.rowcount = self._rowcounts.pop(0)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.committed = True


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


class TestUpdatePriceLimitsBatch:
    def test_empty_updates_returns_zero(self) -> None:
        assert db_utils.update_price_limits_batch("postgres://x", []) == 0

    def test_uses_update_not_insert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """必須是 UPDATE：走 upsert 會 INSERT 出半套 row。"""
        cursor = _FakeCursor([1])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        db_utils.update_price_limits_batch(
            "postgres://x", [("2330", DATE, Decimal("2655"), Decimal("2175"))]
        )

        sql, params = cursor.executed[0]
        assert sql.startswith("UPDATE stock_daily_raw")
        assert "INSERT" not in sql
        assert params == [Decimal("2655"), Decimal("2175"), "2330", DATE]

    def test_returns_total_rowcount(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不存在的 (symbol, trade_date) 不計入。"""
        cursor = _FakeCursor([1, 0, 1])
        conn = _FakeConn(cursor)
        monkeypatch.setattr(db_utils, "get_pool", lambda url: _FakePool(conn))

        n = db_utils.update_price_limits_batch(
            "postgres://x",
            [
                ("2330", DATE, Decimal("2655"), Decimal("2175")),
                ("9999", DATE, Decimal("11"), Decimal("9")),
                ("3605", DATE, Decimal("132.0"), Decimal("108.0")),
            ],
        )
        assert n == 2
        assert conn.committed is True


class TestCollectLimitUpdates:
    def _patch_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mi_df: pd.DataFrame,
        tpex_df: pd.DataFrame,
    ) -> None:
        monkeypatch.setattr(run, "fetch_twse_mi_index", lambda s, d: (mi_df, d))
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (tpex_df, d))
        monkeypatch.setattr(run, "prepare_twse_mi_index", lambda df: df)
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

    def test_computes_limits_from_both_markets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mi = pd.DataFrame([{"symbol": "2330", "close": 2415.0, "change": 20.0}])
        tpex = pd.DataFrame([{"symbol": "6488", "close": 45.21, "change": 1.41}])
        self._patch_sources(monkeypatch, mi, tpex)

        updates = run._collect_limit_updates(None, DATE)

        by_symbol = {u[0]: u for u in updates}
        assert by_symbol["2330"][1] == DATE
        assert by_symbol["2330"][2] == Decimal("2655")
        assert by_symbol["2330"][3] == Decimal("2175")
        assert "6488" in by_symbol

    def test_skips_rows_without_reference_price(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """除權息日 change 為 NaN → 整檔跳過，不寫入也不覆蓋既有值。"""
        mi = pd.DataFrame(
            [
                {"symbol": "2330", "close": 2415.0, "change": 20.0},
                {"symbol": "3605", "close": 120.0, "change": float("nan")},
            ]
        )
        empty = pd.DataFrame(columns=["symbol", "close", "change"])
        self._patch_sources(monkeypatch, mi, empty)

        symbols = {u[0] for u in run._collect_limit_updates(None, DATE)}
        assert symbols == {"2330"}

    def test_one_market_failing_does_not_lose_the_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tw_stock_rawdata.sources import DataUnavailableError

        def _boom(session, date):
            raise DataUnavailableError("TWSE 掛了")

        tpex = pd.DataFrame([{"symbol": "6488", "close": 45.21, "change": 1.41}])
        monkeypatch.setattr(run, "fetch_twse_mi_index", _boom)
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (tpex, d))
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

        symbols = {u[0] for u in run._collect_limit_updates(None, DATE)}
        assert symbols == {"6488"}

    def test_date_mismatch_is_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """來源回傳的是別天的資料 → 不可寫進本日。"""
        mi = pd.DataFrame([{"symbol": "2330", "close": 2415.0, "change": 20.0}])
        empty = pd.DataFrame(columns=["symbol", "close", "change"])
        monkeypatch.setattr(
            run, "fetch_twse_mi_index", lambda s, d: (mi, dt.date(2026, 8, 11))
        )
        monkeypatch.setattr(run, "fetch_tpex_daily_quotes_v2", lambda s, d: (empty, d))
        monkeypatch.setattr(run, "prepare_twse_mi_index", lambda df: df)
        monkeypatch.setattr(run, "prepare_tpex_quotes", lambda df: df)

        assert run._collect_limit_updates(None, DATE) == []


def test_backfill_limits_is_not_daily_mode() -> None:
    """--backfill-limits 是手動模式，不該被 config.is_trading_day 休市開關擋住。"""
    import argparse

    args = argparse.Namespace(
        date=None, backfill_start="2025-01-01", backfill_end="2025-01-31",
        backfill_stocks=None, backfill_limits=True, update_shares=False, dahu=False,
    )
    assert run._is_daily_mode(args) is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/unit/test_backfill_limits.py -v`
Expected: FAIL — `AttributeError: module 'tw_stock_rawdata.db_utils' has no attribute 'update_price_limits_batch'`

- [ ] **Step 3: 寫最小實作**

`src/tw_stock_rawdata/db_utils.py`，在 `update_prev_day_margin_batch` 之後新增：

```python
def update_price_limits_batch(
    database_url: str,
    updates: list[tuple[str, dt.date, Decimal, Decimal]],
) -> int:
    """批次覆寫 stock_daily_raw 的 limit_up / limit_down。

    只 UPDATE 已存在的 row，不 INSERT：回補只有這兩欄有值，若走 upsert 會 INSERT 出
    一批其餘欄位全 NULL 的半套 row（見 CLAUDE.md「逐檔跳過半套資料」）。

    Args:
        database_url: PostgreSQL connection string.
        updates: list of (symbol, trade_date, limit_up, limit_down)。

    Returns:
        實際 UPDATE 成功的 row 數合計（不存在的 (symbol, trade_date) 不算）。
    """
    if not updates:
        return 0

    sql = (
        "UPDATE stock_daily_raw SET limit_up = %s, limit_down = %s"
        " WHERE symbol = %s AND trade_date = %s"
    )

    n_updated = 0
    pool = get_pool(database_url)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for symbol, trade_date, limit_up, limit_down in updates:
                cur.execute(sql, [limit_up, limit_down, symbol, trade_date])
                n_updated += cur.rowcount
        conn.commit()
    return n_updated
```

並在 `db_utils.py` 頂端 import 區加入 `from decimal import Decimal`。

`src/tw_stock_rawdata/run.py`：

`_parse_args` 中，`--backfill-stocks` 之後加入：

```python
    parser.add_argument(
        "--backfill-limits", action="store_true",
        help="只回補 limit_up / limit_down（需搭配 --backfill-start / --backfill-end）",
    )
```

`_is_daily_mode` 的條件加入一行 `or args.backfill_limits`：

```python
    return not (
        args.date
        or args.backfill_start
        or args.backfill_end
        or args.backfill_stocks
        or args.backfill_limits
        or args.update_shares
        or args.dahu
    )
```

新增兩個函式（放在 `_build_daily_rows` 之前）：

```python
def _collect_limit_updates(
    session: requests.Session,
    date: dt.date,
) -> list[tuple[str, dt.date, Decimal, Decimal]]:
    """抓單日兩市場行情，算出可寫入的 (symbol, date, limit_up, limit_down)。

    只用 MI_INDEX（上市）與 TPEX v2 dailyQuotes（上櫃）—— STOCK_DAY_ALL 無視 date
    參數永遠回最後交易日，不能用於回補。任一市場失敗只影響該市場，不中斷另一邊。
    推不出參考價的個股整檔跳過，不寫入也不覆蓋既有值。
    """
    frames: list[pd.DataFrame] = []

    try:
        mi_raw, mi_date = fetch_twse_mi_index(session, date)
        if mi_date == date:
            frames.append(prepare_twse_mi_index(mi_raw))
        else:
            print(f"{date.isoformat()} TWSE MI_INDEX 日期不匹配：{mi_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{date.isoformat()} TWSE MI_INDEX 取得失敗：{exc}")

    try:
        tpex_raw, tpex_date = fetch_tpex_daily_quotes_v2(session, date)
        if tpex_date == date:
            frames.append(prepare_tpex_quotes(tpex_raw))
        else:
            print(f"{date.isoformat()} TPEX 日行情日期不匹配：{tpex_date} != {date}")
    except (DataUnavailableError, requests.RequestException) as exc:
        print(f"{date.isoformat()} TPEX 日行情取得失敗：{exc}")

    updates: list[tuple[str, dt.date, Decimal, Decimal]] = []
    for frame in frames:
        for _, row in frame.iterrows():
            symbol = str(row.get("symbol", "")).strip()
            if not symbol:
                continue
            limit_up, limit_down = calc_limits(
                _reference_price(row.get("close"), row.get("change"))
            )
            if limit_up is None or limit_down is None:
                continue
            updates.append((symbol, date, limit_up, limit_down))
    return updates


def _backfill_limits_command(
    session: requests.Session,
    config: AppConfig,
    args: argparse.Namespace,
) -> None:
    """--backfill-limits：只回補 stock_daily_raw 的 limit_up / limit_down。"""
    if not args.backfill_start or not args.backfill_end:
        print("錯誤：--backfill-limits 需搭配 --backfill-start 和 --backfill-end")
        return

    dates = _build_date_range(
        _parse_date(args.backfill_start), _parse_date(args.backfill_end)
    )
    print(f"回補漲跌停 {len(dates)} 天：{dates[0]} ~ {dates[-1]}")

    total = 0
    for date in dates:
        if date.weekday() >= 5:
            continue
        updates = _collect_limit_updates(session, date)
        if not updates:
            print(f"{date.isoformat()} 無可回補資料")
            continue
        n_updated = update_price_limits_batch(config.database_url, updates)
        total += n_updated
        print(f"{date.isoformat()} 更新 {n_updated} 檔")

    print(f"漲跌停回補完成，共更新 {total} 列")
```

`run.py` 的 import：`fetch_twse_mi_index`、`fetch_tpex_daily_quotes_v2`、
`prepare_twse_mi_index`、`prepare_tpex_quotes`、`DataUnavailableError`
**都已經 import 過了，不用動**。只需在既有的 `from .db_utils import (...)`
區塊加入 `update_price_limits_batch`。

`_main_inner` 中，在 `# --dahu mode` 區塊之後、`# --backfill-stocks mode` 之前插入：

```python
    # --backfill-limits mode：只回補漲跌停兩欄
    if args.backfill_limits:
        _backfill_limits_command(session, config, args)
        return
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/unit/ -v`
Expected: PASS（全部測試通過）

- [ ] **Step 5: 更新 README CLI 章節**

在 `README.md` 的 `## CLI 指令` 程式碼區塊中，`# 強制覆蓋既有資料` 那段**之前**插入：

```bash
# 只回補 limit_up / limit_down（不重打法人、融資融券、持股，比全量回補快很多）
tw-stock-rawdata --backfill-limits --backfill-start 2025-01-01 --backfill-end 2026-08-12
```

並在 Task 4 新增的 `### 漲跌停價（limit_up / limit_down）` 小節末尾補一段：

```markdown
歷史資料可用 `--backfill-limits` 回補：每個交易日只打 `MI_INDEX`（上市）與 TPEX
`dailyQuotes`（上櫃）兩個批量行情 API，不重打三大法人 / 融資融券 / 持股。
它只 `UPDATE` 已存在的 row，不會新增 row；推不出參考價的個股整檔跳過，
不會把既有值蓋成 `NULL`。結果冪等，可重複執行，不需 `--force`，
也不支援 `--backfill-stocks`。
```

（`### 休市開關` 一節列的手動操作已寫成 `--backfill-*`，涵蓋 `--backfill-limits`，
該段不用改。）

- [ ] **Step 6: Commit**

```bash
git add src/tw_stock_rawdata/run.py src/tw_stock_rawdata/db_utils.py README.md tests/unit/test_backfill_limits.py
git commit -m "feat: 新增 --backfill-limits 回補歷史漲跌停價"
```

---

## 驗收

全部任務完成後執行：

```bash
pytest tests/unit/ -v
```

Expected: 全部 PASS。

手動驗收（需 DB 與網路）：

```bash
tw-stock-rawdata --date 2026-08-13
```

然後查詢確認 3605 當日 `limit_up = 132.00` 且 `close = limit_up`（該檔 2026-08-13 漲停），
以及 2026-08-12 的 3605 因除息而 `limit_up IS NULL`。
