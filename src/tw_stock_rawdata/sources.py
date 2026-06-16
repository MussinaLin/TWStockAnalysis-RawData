from __future__ import annotations

import datetime as dt
import functools
import io
import os
import random
import re
import threading
import time
import urllib3
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TWSE_T86_URL = "https://www.twse.com.tw/fund/T86"
TWSE_STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
TPEX_DAILY_QUOTES_URL = (
    "https://www.tpex.org.tw/web/stock/aftertrading/DAILY_CLOSE_quotes/"
    "stk_quote_result.php?l=zh-tw&o=data"
)
TPEX_3INSTI_URL = (
    "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
    "3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&o=data"
)
TPEX_DAILY_QUOTES_V2_URL = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
)
TPEX_3INSTI_V2_URL = (
    "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
)
TWSE_COMPANY_BASIC_URL = "https://dts.twse.com.tw/opendata/t187ap03_L.csv"
TPEX_COMPANY_BASIC_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_TAIEX_OHLC_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"
TWSE_MARKET_VOLUME_URL = "https://www.twse.com.tw/exchangeReport/FMTQIK"
TWSE_FOREIGN_NET_URL = "https://www.twse.com.tw/fund/BFI82U"
TWSE_MARKET_MARGIN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
TWSE_MARGIN_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
TPEX_MARGIN_V2_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
MONEYDJ_MARGIN_URL = "https://concords.moneydj.com/z/zc/zcn/zcn.djhtm"
MONEYDJ_HOLDING_URL = "https://concords.moneydj.com/z/zc/zcl/zcl.djhtm"
TDCC_QRY_STOCK_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"


class DataUnavailableError(RuntimeError):
    pass


# www.twse.com.tw 對同 IP 高頻請求會限流，但不是回 5xx / 斷線，而是回一個
# HTTP 200 + {"stat":"很抱歉，沒有符合條件的資料!","total":0} 的空殼——這與「真休市」
# 的回應逐位元組相同，故 fetcher 會丟 DataUnavailableError，而該例外在 _retry_on_transient
# 是「不重試直接放棄」→ 限流被誤判成沒資料、整批跳過（使用者感受到「TWSE 常常連線失敗」）。
# 對策：對 www.twse.com.tw 強制最小請求間隔，從源頭避免觸發限流（不動 retry 語意）。
TWSE_WWW_PREFIX = "https://www.twse.com.tw"
DEFAULT_TWSE_MIN_INTERVAL = 1.0


class _MinIntervalAdapter(HTTPAdapter):
    """強制透過此 adapter 送出的請求之間至少間隔 min_interval 秒（序列化 + 最小間隔）。

    間隔以「上一次請求派送時刻」為基準量測；請求本身耗時已自然算進間隔內，
    min_interval 只是下限。lock 讓多執行緒下仍序列化 pacing（本批次為單執行緒，無妨）。
    """

    def __init__(self, *args, min_interval: float = DEFAULT_TWSE_MIN_INTERVAL, **kwargs):
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._last_ts = 0.0
        super().__init__(*args, **kwargs)

    def send(self, *args, **kwargs):
        with self._lock:
            if self._last_ts:
                wait = self._min_interval - (time.monotonic() - self._last_ts)
                if wait > 0:
                    time.sleep(wait)
            self._last_ts = time.monotonic()
        return super().send(*args, **kwargs)


def build_session(min_interval: float | None = None) -> requests.Session:
    """建立帶 www.twse.com.tw pacing 的 HTTP session。

    只對 www.twse.com.tw 加最小請求間隔（避免限流空殼回應，見 _MinIntervalAdapter）；
    openapi.twse.com.tw / TPEX / MoneyDJ / TDCC 等其他 host 不受影響。
    min_interval=None 時預設讀環境變數 TWSE_MIN_INTERVAL，否則用 DEFAULT_TWSE_MIN_INTERVAL，
    讓使用者免改 code 即可調參。
    """
    if min_interval is None:
        raw = os.environ.get("TWSE_MIN_INTERVAL")
        try:
            min_interval = float(raw) if raw is not None else DEFAULT_TWSE_MIN_INTERVAL
        except ValueError:
            min_interval = DEFAULT_TWSE_MIN_INTERVAL

    session = requests.Session()
    session.headers.update({"User-Agent": "tw-stock-rawdata/0.1"})
    session.mount(TWSE_WWW_PREFIX, _MinIntervalAdapter(min_interval=min_interval))
    return session


# Retry config for the once-per-date TWSE/TPEX fetches (T86 / MI_INDEX / TPEX quotes & 3insti).
# 窗口需撐得過 TPEX/Cloudflare 暫時性 5xx（例如 520，常需數十秒才恢復），
# 故 attempts 拉到 6、單次 backoff 以 max_delay 設上限，再疊加 additive jitter 去同步化固定間隔。
# deterministic capped delays 為 [2,4,8,16,16]（5 次 sleep）≈ 46s 保底，加 jitter 後上限 ~56s 才放棄。
# 註：超過分鐘級的長時間中斷不在此處理——當天 OTC 視為缺漏、由後續回補補齊（與既有 skip 語意一致）。
RETRY_ATTEMPTS = 6
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 16.0
# Additive jitter（上限秒數）疊加在 deterministic backoff 之上——只會等更久、不會更短，
# 故不會縮短任何 profile 既有的重試窗口，只用來打散固定間隔避免每次撞同一台復原中的伺服器。
RETRY_JITTER = 2.0

# 較短的 retry profile，給「每檔個股各打一次」的 fetch_twse_stock_day 使用。
# 該函式在 _build_daily_rows 內對每個 holding 呼叫一次，若套用上面的長窗口，
# 單一上游中斷會被乘上數百檔 → 整批 stall 數小時。deterministic delays 為 [2,4]，
# 保留原本約 6s 的短窗口（additive jitter 只會略為加長，不會提早放棄）。
PER_SYMBOL_RETRY_ATTEMPTS = 3
PER_SYMBOL_RETRY_MAX_DELAY = 8.0


def _retry_backoff_delay(attempt: int, max_delay: float = RETRY_MAX_DELAY) -> float:
    """Additive-jitter exponential backoff for retry attempt index (0-based).

    deterministic backoff = min(max_delay, base * 2**attempt)，再疊加 uniform(0, RETRY_JITTER)。
    additive（而非 equal）jitter 確保等候時間「永遠 ≥ deterministic backoff」，
    所以不會把既定窗口縮短、讓最後一次重試提早觸發；jitter 只負責打散固定間隔。
    長 profile 的 deterministic delays 為 [2,4,8,16,16]，總窗口約 46–56s。
    """
    base = min(max_delay, RETRY_BASE_DELAY * (2 ** attempt))
    return base + random.uniform(0, RETRY_JITTER)


def _retry_on_transient(
    fn=None,
    *,
    attempts: int = RETRY_ATTEMPTS,
    max_delay: float = RETRY_MAX_DELAY,
):
    """Retry a fetch on偶發/格式異常的回應（可調 attempts 與 backoff 上限）。

    TWSE/TPEX 偶發回傳不一致的 JSON（例如 fields 與 data 欄數不符 → ValueError）或
    暫時性網路錯誤（含 5xx，如 Cloudflare 520）。重新發請求通常就正常，故以 backoff 重試數次。
    DataUnavailableError（合法的「沒資料/休市」）不重試、立即往上拋。

    用盡所有 attempts 後依例外型別決定處置：
    - ValueError（格式/解析異常）→ 轉成 DataUnavailableError，讓呼叫端跨過該日而非中斷。
    - requests.RequestException（網路）→ 原樣重拋，維持呼叫端既有的網路錯誤語意。

    可當 bare decorator（`@_retry_on_transient`，用長窗口預設值）或帶參數
    （`@_retry_on_transient(attempts=..., max_delay=...)`，給 per-symbol 短窗口）使用。
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except DataUnavailableError:
                    raise
                except (requests.RequestException, ValueError) as exc:
                    last_exc = exc
                    if attempt < attempts - 1:
                        time.sleep(_retry_backoff_delay(attempt, max_delay))
            if isinstance(last_exc, requests.RequestException):
                raise last_exc
            raise DataUnavailableError(
                f"{func.__name__} 連續 {attempts} 次取得失敗（疑似偶發 API 異常）：{last_exc}"
            )

        return wrapper

    # bare usage: @_retry_on_transient
    if fn is not None:
        return decorate(fn)
    # parametrized usage: @_retry_on_transient(attempts=..., max_delay=...)
    return decorate


def _parse_roc_date(value: str) -> dt.date | None:
    """Parse ROC date string (e.g. '114/03/18') to dt.date. Returns None on failure."""
    match = re.match(r"^(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})$", value.strip())
    if not match:
        return None
    year = int(match.group(1)) + 1911
    month = int(match.group(2))
    day = int(match.group(3))
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _parse_date_any(value: str) -> dt.date | None:
    """Parse date from multiple formats: YYYYMMDD, YYYY-MM-DD, YYYY/MM/DD, or ROC."""
    text = value.strip()
    if not text:
        return None

    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    roc_date = _parse_roc_date(text)
    if roc_date:
        return roc_date

    return None


def _extract_first_date(text: str) -> dt.date | None:
    """Extract the first recognizable date from free-form text (title, subtitle, etc.)."""
    patterns = [
        r"(?<!\d)(\d{4}[/-]\d{1,2}[/-]\d{1,2})(?!\d)",
        r"(?<!\d)(\d{2,3}[/-]\d{1,2}[/-]\d{1,2})(?!\d)",
        r"(?<!\d)(\d{8})(?!\d)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            date_value = _parse_date_any(match.group(1))
            if date_value:
                return date_value
    return None


def _clean_number(value: Any) -> float | None:
    """Convert value to float, handling commas, '--', NaN. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"--", "---", "", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean_int(value: Any) -> int | None:
    """Convert value to int via _clean_number, rounding. Returns None on failure."""
    number = _clean_number(value)
    if number is None:
        return None
    if pd.isna(number):
        return None
    return int(round(number))


def _roc_to_date(roc_date: str) -> dt.date | None:
    match = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", roc_date.strip())
    if not match:
        return None
    year = int(match.group(1)) + 1911
    month = int(match.group(2))
    day = int(match.group(3))
    return dt.date(year, month, day)


def _date_to_roc(date: dt.date) -> str:
    return f"{date.year - 1911}/{date.month:02d}/{date.day:02d}"


def _format_template(template: str, date: dt.date) -> str:
    return template.format(date=date.isoformat(), roc=_date_to_roc(date))


def _extract_twse_table(payload: dict[str, Any]) -> pd.DataFrame:
    """Extract the OHLCV table from TWSE MI_INDEX JSON payload.

    Searches through 'tables' array or 'fieldsN'/'dataN' pairs for a table
    containing stock code and OHLC columns. Raises DataUnavailableError if not found.
    """
    tables = payload.get("tables")
    if isinstance(tables, list):
        for table in tables:
            fields = table.get("fields")
            data = table.get("data")
            if not isinstance(fields, list) or not isinstance(data, list):
                continue
            joined = "".join(map(str, fields))
            if ("證券代號" in joined or "代號" in joined) and ("開盤" in joined) and ("收盤" in joined):
                return pd.DataFrame(data, columns=fields)

    for key, fields in payload.items():
        if not key.startswith("fields"):
            continue
        if not isinstance(fields, list):
            continue
        suffix = key.replace("fields", "")
        data_key = f"data{suffix}"
        data = payload.get(data_key)
        if not isinstance(data, list):
            continue
        joined = "".join(map(str, fields))
        if ("證券代號" in joined or "代號" in joined) and ("開盤" in joined) and ("收盤" in joined):
            return pd.DataFrame(data, columns=fields)

    raise DataUnavailableError("TWSE MI_INDEX 無法找到行情表格。")


def _read_tpex_csv(text: str) -> pd.DataFrame:
    """Parse TPEX CSV response text into DataFrame.

    Handles encoding quirks, skips header lines, and auto-detects the column header
    row (containing 代號/名稱). Raises DataUnavailableError on parse failure.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise DataUnavailableError("TPEX 回傳內容為空。")

    joined = "\n".join(lines)
    lower = joined.lower()
    if "<html" in lower or "<!doctype" in lower:
        raise DataUnavailableError("TPEX 回傳非 CSV（可能為網頁內容）。")
    if "查無資料" in joined or "沒有資料" in joined:
        raise DataUnavailableError("TPEX 查無資料。")

    header_idx = None
    for idx, line in enumerate(lines):
        if "," not in line:
            continue
        if line.lstrip().startswith(("註", "說明")):
            continue
        if ("代號" in line and "名稱" in line) or ("證券代號" in line and "收盤" in line):
            header_idx = idx
            break

    if header_idx is None:
        raise DataUnavailableError("TPEX CSV 解析失敗，未找到表頭。")

    csv_text = "\n".join(lines[header_idx:])
    return pd.read_csv(io.StringIO(csv_text))


@_retry_on_transient(
    attempts=PER_SYMBOL_RETRY_ATTEMPTS, max_delay=PER_SYMBOL_RETRY_MAX_DELAY
)
def fetch_twse_stock_day(
    session: requests.Session,
    stock_no: str,
    date: dt.date,
) -> pd.DataFrame:
    """Fetch monthly OHLCV data for a single stock from TWSE STOCK_DAY.

    Returns raw DataFrame with ROC-date columns (日期, 開盤價, 收盤價, etc.).
    Queries the full month containing `date`.
    """
    month_start = date.replace(day=1)
    params = {
        "response": "json",
        "date": month_start.strftime("%Y%m%d"),
        "stockNo": stock_no,
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_STOCK_DAY_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    if not response.text.strip():
        raise DataUnavailableError("TWSE STOCK_DAY 回傳空白")
    try:
        payload = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise DataUnavailableError("TWSE STOCK_DAY 回傳非 JSON")
    if payload.get("stat") != "OK":
        raise DataUnavailableError(payload.get("stat") or "TWSE STOCK_DAY 回傳異常")

    data = payload.get("data") or []
    fields = payload.get("fields") or []
    if not data or not fields:
        raise DataUnavailableError("TWSE STOCK_DAY 無資料")

    df = pd.DataFrame(data, columns=fields)
    return df


def find_twse_open_close(df: pd.DataFrame, date: dt.date) -> tuple[float | None, float | None]:
    """Extract (open, close) for a specific date from a STOCK_DAY DataFrame."""
    if "日期" not in df.columns:
        return None, None

    df = df.copy()
    df["_gregorian"] = df["日期"].map(_roc_to_date)
    row = df.loc[df["_gregorian"] == date]
    if row.empty:
        return None, None

    open_price = _clean_number(row.iloc[0].get("開盤價"))
    close_price = _clean_number(row.iloc[0].get("收盤價"))
    return open_price, close_price


def find_twse_ohlcv(
    df: pd.DataFrame, date: dt.date
) -> tuple[float | None, float | None, float | None, float | None, int | None]:
    """Extract (open, high, low, close, volume) for a date from a STOCK_DAY DataFrame."""
    if "日期" not in df.columns:
        return None, None, None, None, None

    df = df.copy()
    df["_gregorian"] = df["日期"].map(_roc_to_date)
    row = df.loc[df["_gregorian"] == date]
    if row.empty:
        return None, None, None, None, None

    open_price = _clean_number(row.iloc[0].get("開盤價"))
    high_price = _clean_number(row.iloc[0].get("最高價"))
    low_price = _clean_number(row.iloc[0].get("最低價"))
    close_price = _clean_number(row.iloc[0].get("收盤價"))
    volume = _clean_int(row.iloc[0].get("成交股數")) or _clean_int(row.iloc[0].get("成交量"))
    return open_price, high_price, low_price, close_price, volume


@_retry_on_transient
def fetch_twse_t86(session: requests.Session, date: dt.date) -> pd.DataFrame:
    """Fetch institutional investor buy/sell data from TWSE T86 (三大法人買賣超).

    Returns raw DataFrame with columns like 證券代號, 買進股數, 賣出股數, etc.
    """
    params = {
        "response": "json",
        "date": date.strftime("%Y%m%d"),
        "selectType": "ALL",
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_T86_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "OK":
        raise DataUnavailableError(payload.get("stat") or "TWSE T86 回傳異常")

    fields = payload.get("fields") or []
    data = payload.get("data") or []
    if not data or not fields:
        raise DataUnavailableError("TWSE T86 無資料")

    df = pd.DataFrame(data, columns=fields)
    return df


def fetch_twse_stock_day_all(session: requests.Session) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch all-stocks OHLCV from TWSE OpenAPI (STOCK_DAY_ALL).

    Returns (DataFrame of all stocks, data_date). data_date is extracted from
    the first record's Date field; None if unparseable.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_STOCK_DAY_ALL_URL, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise DataUnavailableError("TWSE STOCK_DAY_ALL 回傳格式異常")
    if not payload:
        raise DataUnavailableError("TWSE STOCK_DAY_ALL 無資料")
    data_date = None
    sample = payload[0]
    if isinstance(sample, dict):
        for key in ("Date", "date", "日期"):
            if key in sample:
                data_date = _parse_date_any(str(sample.get(key, "")))
                if data_date:
                    break
        if data_date is None:
            for key, value in sample.items():
                if "date" in str(key).lower() or "日期" in str(key):
                    data_date = _parse_date_any(str(value))
                    if data_date:
                        break
    return pd.DataFrame(payload), data_date


@_retry_on_transient
def fetch_twse_mi_index(session: requests.Session, date: dt.date) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch market index data from TWSE MI_INDEX (fallback for individual stock OHLCV).

    Returns (DataFrame with per-stock rows, data_date).
    """
    params = {
        "response": "json",
        "date": date.strftime("%Y%m%d"),
        "type": "ALLBUT0999",
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_MI_INDEX_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") not in {None, "OK"}:
        raise DataUnavailableError(payload.get("stat") or "TWSE MI_INDEX 回傳異常")

    if not isinstance(payload, dict):
        raise DataUnavailableError("TWSE MI_INDEX 回傳格式異常")

    data_date = None
    for key in ("date", "Date", "reportDate", "dataDate", "REPORTDATE", "DATADATE"):
        if key in payload:
            data_date = _parse_date_any(str(payload.get(key, "")))
            if data_date:
                break
    if data_date is None:
        for key, value in payload.items():
            if "date" in str(key).lower():
                data_date = _parse_date_any(str(value))
                if data_date:
                    break

    return _extract_twse_table(payload), data_date


@_retry_on_transient
def fetch_tpex_daily_quotes(
    session: requests.Session,
    date: dt.date | None = None,
    template: str | None = None,
) -> tuple[pd.DataFrame, dt.date | None]:
    if date is not None:
        if not template:
            raise DataUnavailableError(
                "未設定 TPEX_DAILY_QUOTES_URL_TEMPLATE，無法回補指定日期上櫃行情。"
            )
        url = _format_template(template, date)
    else:
        url = TPEX_DAILY_QUOTES_URL

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(url, timeout=30, verify=False)
    response.raise_for_status()

    content = response.content
    for encoding in ("utf-8-sig", "cp950"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            text = ""
    if not text:
        raise DataUnavailableError("TPEX 日行情解碼失敗")

    data_date = _extract_first_date(text)
    df = _read_tpex_csv(text)
    return df, data_date


@_retry_on_transient
def fetch_tpex_3insti(
    session: requests.Session,
    date: dt.date | None = None,
    template: str | None = None,
) -> tuple[pd.DataFrame, dt.date | None]:
    if date is not None:
        if not template:
            raise DataUnavailableError(
                "未設定 TPEX_3INSTI_URL_TEMPLATE，無法回補指定日期上櫃三大法人。"
            )
        url = _format_template(template, date)
    else:
        url = TPEX_3INSTI_URL

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(url, timeout=30, verify=False)
    response.raise_for_status()

    content = response.content
    for encoding in ("utf-8-sig", "cp950"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            text = ""
    if not text:
        raise DataUnavailableError("TPEX 三大法人解碼失敗")

    data_date = _extract_first_date(text)
    df = _read_tpex_csv(text)
    return df, data_date


def _extract_tpex_v2_table(
    payload: dict, title_keyword: str = "上櫃股票"
) -> pd.DataFrame:
    """Extract DataFrame from new TPEX JSON API (tables/fields/data format)."""
    tables = payload.get("tables", [])
    for table in tables:
        if not isinstance(table, dict):
            continue
        title = table.get("title", "")
        fields = table.get("fields", [])
        data = table.get("data", [])
        if title_keyword in title and fields and data:
            return pd.DataFrame(data, columns=fields)
    raise DataUnavailableError(f"TPEX V2 找不到包含「{title_keyword}」的表格。")


@_retry_on_transient
def fetch_tpex_daily_quotes_v2(
    session: requests.Session,
    date: dt.date,
) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch TPEX daily quotes using the new API that supports historical queries."""
    roc = _date_to_roc(date)
    params = {"date": roc, "response": "json"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_DAILY_QUOTES_V2_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") not in {None, "ok", "OK"}:
        raise DataUnavailableError(payload.get("stat") or "TPEX V2 行情回傳異常")

    data_date = _parse_date_any(str(payload.get("date", "")))
    df = _extract_tpex_v2_table(payload, "上櫃股票")
    return df, data_date


@_retry_on_transient
def fetch_tpex_3insti_v2(
    session: requests.Session,
    date: dt.date,
) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch TPEX 3-institutional-investors data using the new API."""
    roc = _date_to_roc(date)
    params = {"date": roc, "response": "json", "type": "Daily", "se": "EW"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_3INSTI_V2_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") not in {None, "ok", "OK"}:
        raise DataUnavailableError(payload.get("stat") or "TPEX V2 三大法人回傳異常")

    data_date = _parse_date_any(str(payload.get("date", "")))
    df = _extract_tpex_v2_table(payload, "三大法人")

    # The fields have duplicated names (買進股數/賣出股數/買賣超股數 repeated for each
    # institutional category). Rename by position:
    #   [0] 代號, [1] 名稱,
    #   [2-4] 外資及陸資(不含自營商),
    #   [5-7] 外資自營商,
    #   [8-10] 外資及陸資合計,
    #   [11-13] 投信,
    #   [14-16] 自營商(自行買賣),
    #   [17-19] 自營商(避險),
    #   [20-22] 自營商合計,
    #   [23] 三大法人合計
    if len(df.columns) >= 24:
        cols = list(df.columns)
        cols[4] = "外資及陸資買賣超股數"
        cols[10] = "外資合計買賣超股數"
        cols[13] = "投信買賣超股數"
        cols[22] = "自營商合計買賣超股數"
        cols[23] = "三大法人買賣超股數合計"
        df.columns = cols

    return df, data_date


def fetch_twse_company_basic(session: requests.Session) -> pd.DataFrame:
    """Fetch TWSE listed company basic info including issued shares."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_COMPANY_BASIC_URL, timeout=30, verify=False)
    response.raise_for_status()

    content = response.content
    for encoding in ("utf-8-sig", "cp950", "big5"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            text = ""
    if not text:
        raise DataUnavailableError("TWSE 公司基本資料解碼失敗")

    df = pd.read_csv(io.StringIO(text))
    return df


def fetch_tpex_company_basic(session: requests.Session) -> pd.DataFrame:
    """Fetch TPEX OTC company basic info including issued shares."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_COMPANY_BASIC_URL, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise DataUnavailableError("TPEX 公司基本資料回傳格式異常")
    if not payload:
        raise DataUnavailableError("TPEX 公司基本資料無資料")

    return pd.DataFrame(payload)


def _parse_roc_date_compact(value: str) -> dt.date | None:
    """Parse ROC date in compact format like '1150211' (YYYMMDD)."""
    text = value.strip()
    if not text or len(text) != 7:
        return None
    try:
        year = int(text[:3]) + 1911
        month = int(text[3:5])
        day = int(text[5:7])
        return dt.date(year, month, day)
    except (ValueError, IndexError):
        return None


@_retry_on_transient
def fetch_twse_margin(session: requests.Session) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch TWSE margin trading data for all listed stocks (today only).

    Returns DataFrame and data date. The DataFrame has Chinese column names.
    Key columns: 股票代號, 融資買進, 融資賣出, 融資現金償還, 融資今日餘額,
                 融券賣出, 融券買進, 融券現券償還, 融券今日餘額

    Note: TWSE OpenAPI does not return date in record, returns None for date.
    Caller should assume data is for today when date is None.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TWSE_MARGIN_URL, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise DataUnavailableError("TWSE MI_MARGN 回傳格式異常")
    if not payload:
        raise DataUnavailableError("TWSE MI_MARGN 無資料")

    # TWSE OpenAPI doesn't include date in records, return None
    # Caller should assume it's today's data
    return pd.DataFrame(payload), None


@_retry_on_transient
def fetch_tpex_margin(session: requests.Session) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch TPEX margin trading data for all OTC stocks (today only).

    Returns DataFrame and data date. The DataFrame has English column names.
    Key columns: SecuritiesCompanyCode, MarginPurchase, MarginSales, CashRedemption,
                 MarginPurchaseBalance, ShortSale, ShortCovering, StockRedemption,
                 ShortSaleBalance
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_MARGIN_URL, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list):
        raise DataUnavailableError("TPEX margin 回傳格式異常")
    if not payload:
        raise DataUnavailableError("TPEX margin 無資料")

    # Extract date from first record
    # TPEX uses compact ROC format like "1150211" (YYY/MM/DD without separators)
    data_date = None
    sample = payload[0]
    if isinstance(sample, dict):
        for key in ("Date", "date", "日期", "ReportDate"):
            if key in sample:
                date_str = str(sample.get(key, ""))
                # Try compact ROC format first (e.g., "1150211")
                data_date = _parse_roc_date_compact(date_str)
                if data_date:
                    break
                # Fallback to other formats
                data_date = _parse_date_any(date_str)
                if data_date:
                    break

    return pd.DataFrame(payload), data_date


@_retry_on_transient
def fetch_tpex_margin_v2(
    session: requests.Session,
    date: dt.date,
) -> tuple[pd.DataFrame, dt.date | None]:
    """Fetch TPEX margin trading data using the V2 API that supports historical dates.

    Returns DataFrame and data date. The DataFrame has Chinese column names
    (代號, 資買, 資賣, 資餘額, 前資餘額, 券賣, 券買, 券餘額, 前券餘額, etc.).
    """
    roc = _date_to_roc(date)
    params = {"date": roc, "response": "json"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(TPEX_MARGIN_V2_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") not in {None, "ok", "OK"}:
        raise DataUnavailableError(payload.get("stat") or "TPEX V2 融資融券回傳異常")

    data_date = _parse_date_any(str(payload.get("date", "")))
    df = _extract_tpex_v2_table(payload, "上櫃股票")
    return df, data_date


@_retry_on_transient(
    attempts=PER_SYMBOL_RETRY_ATTEMPTS, max_delay=PER_SYMBOL_RETRY_MAX_DELAY
)
def fetch_moneydj_margin(
    session: requests.Session,
    symbol: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Fetch historical margin trading data from MoneyDJ for a single stock.

    Args:
        session: HTTP session
        symbol: Stock symbol (e.g., "2330")
        start: Start date
        end: End date

    Returns:
        DataFrame with columns: date, margin_buy, margin_sell, margin_balance,
                               margin_change, short_sell, short_buy, short_balance,
                               short_change (units: lots/張)
    """
    # MoneyDJ uses YYYY-M-D format (no zero padding)
    start_str = f"{start.year}-{start.month}-{start.day}"
    end_str = f"{end.year}-{end.month}-{end.day}"

    params = {"a": symbol, "c": start_str, "d": end_str}

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(MONEYDJ_MARGIN_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()

    # Parse HTML tables。
    # 不在此攔截 read_html 的 ValueError：讓它往上拋給 _retry_on_transient 重試
    # （MoneyDJ 偶發回傳壞/不完整 HTML 的暫時性情境）；用盡 retry 後 decorator 會
    # 把 ValueError 轉成 DataUnavailableError，呼叫端語意不變。
    tables = pd.read_html(io.StringIO(response.text), encoding="utf-8")

    # Find the margin data table - it's typically the largest table with date data
    # MoneyDJ table structure:
    # - Row 5: top-level headers (融資, 融券)
    # - Row 6: sub-headers (日期, 買進, 賣出, 現償, 餘額, 增減, ...)
    # - Row 7+: data rows
    target_table = None
    for table in tables:
        # Check if table has enough rows and columns for margin data
        if len(table) < 8 or len(table.columns) < 12:
            continue
        # Check if row 6 contains "日期" (date header)
        row6 = table.iloc[6] if len(table) > 6 else None
        if row6 is not None:
            row6_str = " ".join(str(v) for v in row6.values if pd.notna(v))
            if "日期" in row6_str and ("買進" in row6_str or "賣出" in row6_str):
                target_table = table
                break

    if target_table is None:
        raise DataUnavailableError("MoneyDJ 找不到融資融券表格")

    # Extract data rows (skip header rows 0-6)
    data_rows = target_table.iloc[7:].copy()

    # Filter out summary rows (contain "合計" or non-date values in first column)
    def _is_valid_date_row(val):
        if pd.isna(val):
            return False
        text = str(val).strip()
        # Valid ROC date format: 115/02/11
        return bool(re.match(r"^\d{2,3}/\d{1,2}/\d{1,2}$", text))

    valid_mask = data_rows.iloc[:, 0].apply(_is_valid_date_row)
    data_rows = data_rows[valid_mask]

    if data_rows.empty:
        raise DataUnavailableError("MoneyDJ 融資融券無有效資料")

    # MoneyDJ column mapping (0-indexed):
    # 0: 日期, 1: 融資買進, 2: 融資賣出, 3: 融資現償, 4: 融資餘額, 5: 融資增減,
    # 6: 融資限額, 7: 融資使用率, 8: 融券賣出, 9: 融券買進, 10: 融券券償,
    # 11: 融券餘額, 12: 融券增減, 13: 券資比, 14: 資券相抵
    col_map = {
        0: "date",
        1: "margin_buy",
        2: "margin_sell",
        4: "margin_balance",
        5: "margin_change",
        8: "short_sell",
        9: "short_buy",
        11: "short_balance",
        12: "short_change",
        # 13: 券資比 — ignored, always calculated from short_balance / margin_balance
    }

    result = pd.DataFrame()
    for idx, col_name in col_map.items():
        if idx < len(data_rows.columns):
            result[col_name] = data_rows.iloc[:, idx].values

    return result


def fetch_moneydj_holding_pct(
    session: requests.Session,
    symbol: str,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Fetch institutional holding percentage from MoneyDJ for a single stock.

    Args:
        session: HTTP session
        symbol: Stock symbol (e.g., "2330")
        start: Start date
        end: End date

    Returns:
        DataFrame with columns: date, foreign_holding_pct, insti_holding_pct
        (percentage strings like "35.03%")
    """
    start_str = f"{start.year}-{start.month}-{start.day}"
    end_str = f"{end.year}-{end.month}-{end.day}"

    params = {"a": symbol, "c": start_str, "d": end_str}

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = session.get(MONEYDJ_HOLDING_URL, params=params, timeout=30, verify=False)
    response.raise_for_status()

    try:
        tables = pd.read_html(io.StringIO(response.text))
    except ValueError as exc:
        raise DataUnavailableError(f"MoneyDJ 法人持股頁面解析失敗：{exc}") from exc

    # Find the holding data table (11 columns, has header rows with 持股比重)
    # Table structure:
    # - Row 5: top-level headers (買賣超, 估計持股, 持股比重)
    # - Row 6: sub-headers (日期, 外資, 投信, 自營商, ...)
    # - Row 7+: data rows
    target_table = None
    for table in tables:
        if len(table) < 8 or len(table.columns) < 11:
            continue
        row6 = table.iloc[6] if len(table) > 6 else None
        if row6 is not None:
            row6_str = " ".join(str(v) for v in row6.values if pd.notna(v))
            if "日期" in row6_str and "外資" in row6_str:
                target_table = table
                break

    if target_table is None:
        raise DataUnavailableError("MoneyDJ 找不到法人持股表格")

    data_rows = target_table.iloc[7:].copy()

    def _is_valid_date_row(val):
        if pd.isna(val):
            return False
        text = str(val).strip()
        return bool(re.match(r"^\d{2,3}/\d{1,2}/\d{1,2}$", text))

    valid_mask = data_rows.iloc[:, 0].apply(_is_valid_date_row)
    data_rows = data_rows[valid_mask]

    if data_rows.empty:
        raise DataUnavailableError("MoneyDJ 法人持股無有效資料")

    # Column mapping (0-indexed):
    # 0: 日期, 9: 外資持股比重, 10: 三大法人持股比重
    result = pd.DataFrame()
    result["date"] = data_rows.iloc[:, 0].values
    result["foreign_holding_pct"] = data_rows.iloc[:, 9].values
    result["insti_holding_pct"] = data_rows.iloc[:, 10].values

    return result


# ---------------------------------------------------------------------------
# Market daily (大盤每日交易行情) fetch functions
# ---------------------------------------------------------------------------


def fetch_twse_taiex_ohlc(
    session: requests.Session, month_date: dt.date
) -> dict[dt.date, dict]:
    """Fetch monthly TAIEX OHLC from MI_5MINS_HIST.

    Args:
        month_date: Any date in the target month (day is ignored).

    Returns:
        dict mapping trade_date -> {taiex_open, taiex_high, taiex_low, taiex_close}
    """
    first = month_date.replace(day=1)
    params = {"response": "json", "date": first.strftime("%Y%m%d")}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.get(TWSE_TAIEX_OHLC_URL, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        raise DataUnavailableError(payload.get("stat") or "MI_5MINS_HIST 回傳異常")

    result: dict[dt.date, dict] = {}
    for row in payload.get("data", []):
        d = _roc_to_date(row[0])
        if d:
            result[d] = {
                "taiex_open": _clean_number(row[1]),
                "taiex_high": _clean_number(row[2]),
                "taiex_low": _clean_number(row[3]),
                "taiex_close": _clean_number(row[4]),
            }
    return result


def fetch_twse_market_volume(
    session: requests.Session, month_date: dt.date
) -> dict[dt.date, int | None]:
    """Fetch monthly market turnover (成交金額) from FMTQIK.

    Returns:
        dict mapping trade_date -> total_volume (元)
    """
    first = month_date.replace(day=1)
    params = {"response": "json", "date": first.strftime("%Y%m%d")}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.get(TWSE_MARKET_VOLUME_URL, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        raise DataUnavailableError(payload.get("stat") or "FMTQIK 回傳異常")

    result: dict[dt.date, int | None] = {}
    for row in payload.get("data", []):
        d = _roc_to_date(row[0])
        if d:
            result[d] = _clean_int(row[2])  # 成交金額(元)
    return result


def fetch_twse_foreign_net(
    session: requests.Session, date: dt.date
) -> int | None:
    """Fetch foreign investor net buy/sell (外資買賣超) from BFI82U for a single date.

    Returns:
        foreign net amount (元), positive = net buy, negative = net sell.
        None if data unavailable.
    """
    params = {"response": "json", "dayDate": date.strftime("%Y%m%d"), "type": "day"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.get(TWSE_FOREIGN_NET_URL, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None

    for row in payload["data"]:
        if "外資及陸資(不含外資自營商)" in str(row[0]):
            return _clean_int(row[3])  # 買賣差額
    return None


def _parse_market_margin_payload(payload: dict) -> dict | None:
    """Parse MI_MARGN JSON payload into margin balance fields.

    Returns:
        {margin_balance, margin_balance_change, prev_margin_balance} (元) or None.
        prev_margin_balance is row[4] × 1000 (TWSE 當下回報的前日餘額) — used to
        reconcile DB 上 D-1 的 margin_balance 是否需要套用 TWSE 事後修正版。
    """
    if payload.get("stat") != "OK":
        return None
    for table in payload.get("tables", []):
        for row in table.get("data", []):
            if "融資金額" in str(row[0]):
                # row: [項目, 買進, 賣出, 現金償還, 前日餘額, 今日餘額] (單位: 仟元)
                today_bal = _clean_int(row[5])
                yesterday_bal = _clean_int(row[4])
                if today_bal is None:
                    return None
                return {
                    "margin_balance": today_bal * 1000,
                    "margin_balance_change": (
                        (today_bal - yesterday_bal) * 1000
                        if yesterday_bal is not None
                        else None
                    ),
                    "prev_margin_balance": (
                        yesterday_bal * 1000 if yesterday_bal is not None else None
                    ),
                }
    return None


def fetch_twse_market_margin(
    session: requests.Session, date: dt.date
) -> dict | None:
    """Fetch market margin summary (融資融券彙總) from MI_MARGN for a single date.

    Returns:
        {margin_balance, margin_balance_change, prev_margin_balance} (元) or None.
    """
    params = {"response": "json", "date": date.strftime("%Y%m%d"), "selectType": "MS"}
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.get(TWSE_MARKET_MARGIN_URL, params=params, timeout=30, verify=False)
    resp.raise_for_status()
    return _parse_market_margin_payload(resp.json())


# ---------------------------------------------------------------------------
# TDCC 集保戶股權分散表（大戶持股佔比）fetch functions
# ---------------------------------------------------------------------------

_TDCC_TOKEN_RE = re.compile(
    r'name="SYNCHRONIZER_TOKEN"[^>]*value="([^"]*)"'
)
_TDCC_DATE_OPTION_RE = re.compile(r'<option[^>]*value="(\d{8})"')


def fetch_tdcc_token_and_dates(
    session: requests.Session,
) -> tuple[str, list[dt.date]]:
    """GET the TDCC qryStock page; return (synchronizer_token, available_dates).

    available_dates 是 scaDate 下拉選單中所有可查的週資料日期（皆為週五結算日），
    由新到舊排序。token 為 CSRF synchronizer token，需帶入後續 POST。
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.get(TDCC_QRY_STOCK_URL, timeout=30, verify=False)
    resp.raise_for_status()

    token_match = _TDCC_TOKEN_RE.search(resp.text)
    if not token_match:
        raise DataUnavailableError("TDCC 頁面找不到 SYNCHRONIZER_TOKEN")
    token = token_match.group(1)

    dates: list[dt.date] = []
    seen: set[dt.date] = set()
    for raw in _TDCC_DATE_OPTION_RE.findall(resp.text):
        parsed = _parse_date_any(raw)
        if parsed and parsed not in seen:
            seen.add(parsed)
            dates.append(parsed)
    if not dates:
        raise DataUnavailableError("TDCC 頁面找不到可查詢日期")

    return token, dates


def fetch_tdcc_distribution(
    session: requests.Session,
    token: str,
    symbol: str,
    date: dt.date,
) -> tuple[pd.DataFrame, str]:
    """Fetch TDCC 集保戶股權分散表 for a single stock and date.

    重點：
    - firDate 必須等於 scaDate（查詢日），否則 TDCC 回「查無此資料」。
    - SYNCHRONIZER_TOKEN 為單次有效：每次 POST 的回應都帶一個全新的 token，
      連續查詢時必須用「上一次回應回傳的新 token」，否則第二次起會回「查無此資料」。

    Returns:
        (分散表 DataFrame, next_token)。DataFrame 欄位含「持股/單位數分級」
        「占集保庫存數比例 (%)」等，通常 17 列（15 個分級 + 差異數調整 + 合計）；
        next_token 供呼叫端鏈接下一次查詢。
        查無資料或解析失敗 → DataUnavailableError。
    """
    date_str = date.strftime("%Y%m%d")
    data = {
        "SYNCHRONIZER_TOKEN": token,
        "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
        "method": "submit",
        "firDate": date_str,
        "scaDate": date_str,
        "sqlMethod": "StockNo",
        "stockNo": symbol,
        "stockName": "",
    }
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = session.post(TDCC_QRY_STOCK_URL, data=data, timeout=30, verify=False)
    resp.raise_for_status()

    # 取出回應內的新 token 供下一次查詢使用；找不到就退回沿用舊 token。
    token_match = _TDCC_TOKEN_RE.search(resp.text)
    next_token = token_match.group(1) if token_match else token

    try:
        tables = pd.read_html(io.StringIO(resp.text))
    except ValueError as exc:
        raise DataUnavailableError(f"TDCC {symbol} {date_str} 頁面解析失敗：{exc}") from exc

    distribution = None
    for table in tables:
        joined = "".join(str(c) for c in table.columns)
        if "持股" in joined and "占集保庫存數比例" in joined:
            distribution = table
            break

    if distribution is None:
        raise DataUnavailableError(f"TDCC {symbol} {date_str} 找不到股權分散表")

    # 「查無此資料」會回傳一個欄位相符、但只有單列佔位字串的表，須視為無資料。
    body = distribution.to_string()
    if "查無此資料" in body or len(distribution) < 2:
        raise DataUnavailableError(f"TDCC {symbol} {date_str} 查無此資料")

    return distribution, next_token
