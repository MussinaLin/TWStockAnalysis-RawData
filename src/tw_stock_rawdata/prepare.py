"""Data preparation and normalization functions for stock data."""

from __future__ import annotations

import re

import pandas as pd

import datetime as dt

from .sources import (
    _clean_int,
    _clean_number,
    _parse_roc_date,
    _parse_roc_date_compact,
    _roc_to_date,
    DataUnavailableError,
)


def _normalize_col(text: str) -> str:
    """Normalize column name by removing BOM, whitespace, and lowercasing."""
    cleaned = text.replace("\ufeff", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.lower()


def _find_column(df: pd.DataFrame, keywords: list[str]) -> str | None:
    """Find column that contains all keywords (normalized)."""
    normalized_keywords = [_normalize_col(keyword) for keyword in keywords]
    for col in df.columns:
        text = _normalize_col(str(col))
        if all(keyword in text for keyword in normalized_keywords):
            return col
    return None


def _find_columns(df: pd.DataFrame, col_specs: dict[str, list[list[str]]]) -> dict[str, str | None]:
    """Find multiple columns based on spec dict.

    Args:
        df: DataFrame to search
        col_specs: Dict mapping output name to list of keyword alternatives
                   e.g. {"symbol": [["證券代號"], ["代號"]], "open": [["開盤"], ["開盤價"]]}

    Returns:
        Dict mapping output name to found column name (or None)
    """
    result = {}
    for name, alternatives in col_specs.items():
        found = None
        for keywords in alternatives:
            found = _find_column(df, keywords)
            if found:
                break
        result[name] = found
    return result


def _extract_standard_columns(
    df: pd.DataFrame,
    cols: dict[str, str | None],
    required: list[str],
    error_msg: str,
) -> pd.DataFrame:
    """Extract and rename columns to standard names.

    Args:
        df: Source DataFrame
        cols: Mapping from standard name to source column name
        required: List of required standard names
        error_msg: Error message if required columns missing

    Returns:
        DataFrame with standardized column names
    """
    # Check required columns
    missing = [r for r in required if not cols.get(r)]
    if missing:
        available = ", ".join([str(c) for c in df.columns[:10]])
        raise DataUnavailableError(f"{error_msg}，缺少 {missing}，可用欄位={available}")

    # Build column mapping (only non-None)
    use_cols = []
    rename_map = {}
    for std_name, src_col in cols.items():
        if src_col:
            use_cols.append(src_col)
            rename_map[src_col] = std_name

    temp = df[use_cols].copy()
    temp = temp.rename(columns=rename_map)
    return temp


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
    if sign == "" and value != 0:
        # 空號只可能對應 0.00（spec 實測 130 筆皆然）。非 0 卻無號，代表正負號欄
        # 找不到或格式跑掉（欄位改名/消失時 sign_text 會是 None）——這種情況正負號
        # 未知，不可默認當正值，寧可回 None 讓上游把 change 寫成 NULL。
        return None
    return -value if sign == "-" else value


def prepare_tpex_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TPEX daily quotes into standard format."""
    cols = _find_columns(df, {
        "symbol": [["證券代號"], ["代號"]],
        "name": [["名稱"]],
        "open": [["開盤"], ["開盤價"]],
        "close": [["收盤"], ["收盤價"]],
        "high": [["最高"], ["最高價"]],
        "low": [["最低"], ["最低價"]],
        "volume": [["成交股數"], ["成交量"]],
        "change": [["漲跌"]],
    })

    temp = _extract_standard_columns(
        df, cols, required=["symbol", "open", "close"],
        error_msg="TPEX 行情欄位解析失敗"
    )

    # Clean and convert
    if "name" in temp.columns:
        temp["name"] = temp["name"].astype(str).str.strip().replace({"nan": ""})
    else:
        temp["name"] = ""
    temp["symbol"] = temp["symbol"].astype(str).str.strip()
    temp["open"] = temp["open"].map(_clean_number)
    temp["close"] = temp["close"].map(_clean_number)
    if "high" in temp.columns:
        temp["high"] = temp["high"].map(_clean_number)
    else:
        temp["high"] = None
    if "low" in temp.columns:
        temp["low"] = temp["low"].map(_clean_number)
    else:
        temp["low"] = None
    if "volume" in temp.columns:
        temp["volume"] = temp["volume"].map(_clean_int)
    else:
        temp["volume"] = None
    if "change" in temp.columns:
        temp["change"] = temp["change"].map(_clean_number)
    else:
        temp["change"] = None

    return temp


def prepare_tpex_3insti(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TPEX institutional investors data into standard format."""
    cols = _find_columns(df, {
        "symbol": [["證券代號"], ["代號"]],
        "name": [["名稱"]],
        "foreign_net": [["外資", "買賣超"], ["外資合計買賣超"]],
        "trust_net": [["投信", "買賣超"]],
        "dealer_net": [["自營商", "買賣超"], ["自營商合計買賣超"]],
    })

    temp = _extract_standard_columns(
        df, cols, required=["symbol", "foreign_net", "trust_net", "dealer_net"],
        error_msg="TPEX 三大法人欄位解析失敗"
    )

    if "name" in temp.columns:
        temp["name"] = temp["name"].astype(str).str.strip().replace({"nan": ""})
    else:
        temp["name"] = ""
    temp["symbol"] = temp["symbol"].astype(str).str.strip()
    temp["foreign_net"] = temp["foreign_net"].map(_clean_int)
    temp["trust_net"] = temp["trust_net"].map(_clean_int)
    temp["dealer_net"] = temp["dealer_net"].map(_clean_int)

    return temp


def prepare_twse_3insti(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TWSE institutional investors data into standard format."""
    cols = _find_columns(df, {
        "symbol": [["證券代號"], ["代號"]],
        "name": [["名稱"]],
        "foreign_net": [["外陸資", "買賣超"], ["外資", "買賣超"]],
        "trust_net": [["投信", "買賣超"]],
        "dealer_net": [["自營商買賣超"], ["自營商", "買賣超"]],
    })

    temp = _extract_standard_columns(
        df, cols, required=["symbol", "foreign_net", "trust_net", "dealer_net"],
        error_msg="TWSE 三大法人欄位解析失敗"
    )

    if "name" in temp.columns:
        temp["name"] = temp["name"].astype(str).str.strip().replace({"nan": ""})
    else:
        temp["name"] = ""
    temp["symbol"] = temp["symbol"].astype(str).str.strip()
    temp["foreign_net"] = temp["foreign_net"].map(_clean_int)
    temp["trust_net"] = temp["trust_net"].map(_clean_int)
    temp["dealer_net"] = temp["dealer_net"].map(_clean_int)

    return temp


def prepare_twse_day_all(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TWSE STOCK_DAY_ALL data into standard format."""
    cols = _find_columns(df, {
        "symbol": [["code"], ["證券代號"], ["代號"]],
        "name": [["name"], ["證券名稱"], ["名稱"]],
        "open": [["openingprice"], ["open"], ["開盤價"], ["開盤"]],
        "close": [["closingprice"], ["close"], ["收盤價"], ["收盤"]],
        "high": [["highestprice"], ["high"], ["最高價"], ["最高"]],
        "low": [["lowestprice"], ["low"], ["最低價"], ["最低"]],
        "volume": [["tradevolume"], ["成交股數"], ["成交量"]],
    })

    temp = _extract_standard_columns(
        df, cols, required=["symbol", "open", "close"],
        error_msg="TWSE STOCK_DAY_ALL 欄位解析失敗"
    )

    if "name" in temp.columns:
        temp["name"] = temp["name"].astype(str).str.strip().replace({"nan": ""})
    else:
        temp["name"] = ""
    temp["symbol"] = temp["symbol"].astype(str).str.strip()
    temp["open"] = temp["open"].map(_clean_number)
    temp["close"] = temp["close"].map(_clean_number)
    if "high" in temp.columns:
        temp["high"] = temp["high"].map(_clean_number)
    else:
        temp["high"] = None
    if "low" in temp.columns:
        temp["low"] = temp["low"].map(_clean_number)
    else:
        temp["low"] = None
    if "volume" in temp.columns:
        temp["volume"] = temp["volume"].map(_clean_int)
    else:
        temp["volume"] = None

    return temp


def prepare_twse_mi_index(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TWSE MI_INDEX data into standard format."""
    cols = _find_columns(df, {
        "symbol": [["證券代號"], ["代號"]],
        "name": [["證券名稱"], ["名稱"]],
        "open": [["開盤價"], ["開盤"]],
        "close": [["收盤價"], ["收盤"]],
        "high": [["最高價"], ["最高"]],
        "low": [["最低價"], ["最低"]],
        "volume": [["成交股數"], ["成交量"]],
        "change": [["漲跌價差"]],
        "change_sign": [["漲跌(+/-)"]],
    })

    temp = _extract_standard_columns(
        df, cols, required=["symbol", "open", "close"],
        error_msg="TWSE MI_INDEX 欄位解析失敗"
    )

    if "name" in temp.columns:
        temp["name"] = temp["name"].astype(str).str.strip().replace({"nan": ""})
    else:
        temp["name"] = ""
    temp["symbol"] = temp["symbol"].astype(str).str.strip()
    temp["open"] = temp["open"].map(_clean_number)
    temp["close"] = temp["close"].map(_clean_number)
    if "high" in temp.columns:
        temp["high"] = temp["high"].map(_clean_number)
    else:
        temp["high"] = None
    if "low" in temp.columns:
        temp["low"] = temp["low"].map(_clean_number)
    else:
        temp["low"] = None
    if "volume" in temp.columns:
        temp["volume"] = temp["volume"].map(_clean_int)
    else:
        temp["volume"] = None
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

    return temp


def prepare_twse_issued_shares(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TWSE company basic data to extract issued shares.

    Returns DataFrame with columns: symbol, name, issued_shares
    """
    cols = _find_columns(df, {
        "symbol": [["公司代號"], ["代號"]],
        "name": [["公司簡稱"], ["公司名稱"], ["名稱"]],
        "issued_shares": [["已發行普通股數"], ["發行股數"]],
        "paid_in_capital": [["實收資本額"]],
        "par_value": [["普通股每股面額"], ["每股面額"]],
    })

    # Try to get issued shares directly, or calculate from capital/par value
    symbol_col = cols.get("symbol")
    name_col = cols.get("name")
    issued_col = cols.get("issued_shares")
    capital_col = cols.get("paid_in_capital")
    par_col = cols.get("par_value")

    if not symbol_col:
        raise DataUnavailableError("TWSE 公司基本資料缺少代號欄位")

    result = pd.DataFrame()
    result["symbol"] = df[symbol_col].astype(str).str.strip()

    if name_col:
        result["name"] = df[name_col].astype(str).str.strip()
    else:
        result["name"] = ""

    if issued_col:
        result["issued_shares"] = df[issued_col].map(_clean_int)
    elif capital_col and par_col:
        # Calculate: issued_shares = paid_in_capital / par_value
        def _extract_par_value(val):
            if pd.isna(val):
                return None
            text = str(val)
            # Extract number from "新台幣 10.0000元"
            match = re.search(r"([\d.]+)", text)
            if match:
                return float(match.group(1))
            return _clean_number(text)

        capital = df[capital_col].map(_clean_int)
        par = df[par_col].map(_extract_par_value)
        result["issued_shares"] = (capital / par).map(
            lambda x: int(x) if pd.notna(x) else None
        )
    else:
        raise DataUnavailableError("TWSE 公司基本資料缺少發行股數或資本額/面額欄位")

    return result.dropna(subset=["issued_shares"])


def prepare_tpex_issued_shares(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TPEX company basic data to extract issued shares.

    Returns DataFrame with columns: symbol, name, issued_shares
    """
    # TPEX JSON API uses English field names
    symbol_col = None
    name_col = None
    issued_col = None

    for col in df.columns:
        col_lower = col.lower()
        if col_lower in ("securitiescompanycode", "companycode", "code"):
            symbol_col = col
        elif col_lower in ("companyabbreviation", "companyname"):
            name_col = col
        elif col_lower == "issueshares":
            issued_col = col

    if not symbol_col:
        # Fallback to Chinese column names
        cols = _find_columns(df, {
            "symbol": [["公司代號"], ["代號"]],
            "name": [["公司簡稱"], ["公司名稱"], ["名稱"]],
            "issued_shares": [["已發行普通股數"], ["發行股數"]],
        })
        symbol_col = cols.get("symbol")
        name_col = cols.get("name")
        issued_col = cols.get("issued_shares")

    if not symbol_col:
        raise DataUnavailableError("TPEX 公司基本資料缺少代號欄位")
    if not issued_col:
        raise DataUnavailableError("TPEX 公司基本資料缺少發行股數欄位")

    result = pd.DataFrame()
    result["symbol"] = df[symbol_col].astype(str).str.strip()
    if name_col:
        result["name"] = df[name_col].astype(str).str.strip()
    else:
        result["name"] = ""
    result["issued_shares"] = df[issued_col].map(_clean_int)

    return result.dropna(subset=["issued_shares"])


def _int_col_or_nulls(df: pd.DataFrame, cols: dict, col_name: str) -> pd.Series:
    """取出某個標準欄位並轉整數；該欄未對應到來源欄位時回一整排 None。

    兩個融資融券 normalize（TWSE / TPEX）共用。cols 的值必定是 df 的真實欄位
    或 None（TWSE 側來自 _find_columns、TPEX 側來自 df_cols_lower），故只需
    檢查真假，不需要再 `in df.columns`。
    """
    src_col = cols.get(col_name)
    if src_col:
        return df[src_col].map(_clean_int)
    return pd.Series([None] * len(df))


def prepare_twse_margin(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TWSE margin trading data into standard format.

    Columns: symbol, margin_buy, margin_sell, margin_balance, margin_change,
             short_sell, short_buy, short_balance, short_change
    Units: lots (張) — API already returns lots since ~2026-03
    """
    cols = _find_columns(df, {
        "symbol": [["股票代號"], ["代號"]],
        "margin_buy": [["融資買進"]],
        "margin_sell": [["融資賣出"]],
        "margin_cash_repay": [["融資現金償還"]],
        "margin_balance": [["融資今日餘額"], ["融資餘額"]],
        "short_sell": [["融券賣出"]],
        "short_buy": [["融券買進"]],
        "short_stock_repay": [["融券現券償還"]],
        "short_balance": [["融券今日餘額"], ["融券餘額"]],
    })

    symbol_col = cols.get("symbol")
    if not symbol_col:
        raise DataUnavailableError("TWSE 融資融券欄位解析失敗，缺少 symbol")

    result = pd.DataFrame()
    result["symbol"] = df[symbol_col].astype(str).str.strip()

    result["margin_buy"] = _int_col_or_nulls(df, cols, "margin_buy")
    result["margin_sell"] = _int_col_or_nulls(df, cols, "margin_sell")
    result["margin_balance"] = _int_col_or_nulls(df, cols, "margin_balance")
    result["short_sell"] = _int_col_or_nulls(df, cols, "short_sell")
    result["short_buy"] = _int_col_or_nulls(df, cols, "short_buy")
    result["short_balance"] = _int_col_or_nulls(df, cols, "short_balance")

    # Calculate margin_change: buy - sell - cash_repay
    margin_buy = _int_col_or_nulls(df, cols, "margin_buy")
    margin_sell = _int_col_or_nulls(df, cols, "margin_sell")
    margin_cash = _int_col_or_nulls(df, cols, "margin_cash_repay")

    if margin_buy is not None and margin_sell is not None:
        margin_change = margin_buy - margin_sell
        if margin_cash is not None:
            margin_change = margin_change - margin_cash.fillna(0)
        result["margin_change"] = margin_change.map(lambda x: int(x) if pd.notna(x) else None)
    else:
        result["margin_change"] = None

    # Calculate short_change: sell - buy - stock_repay
    short_sell_raw = _int_col_or_nulls(df, cols, "short_sell")
    short_buy_raw = _int_col_or_nulls(df, cols, "short_buy")
    short_stock = _int_col_or_nulls(df, cols, "short_stock_repay")

    if short_sell_raw is not None and short_buy_raw is not None:
        short_change = short_sell_raw - short_buy_raw
        if short_stock is not None:
            short_change = short_change - short_stock.fillna(0)
        result["short_change"] = short_change.map(lambda x: int(x) if pd.notna(x) else None)
    else:
        result["short_change"] = None

    return result


def prepare_tpex_margin(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TPEX margin trading data into standard format.

    Columns: symbol, margin_buy, margin_sell, margin_balance, margin_change,
             short_sell, short_buy, short_balance, short_change
    Units: lots (張) — API already returns lots since ~2026-03
    """
    # TPEX uses English column names
    col_mapping = {
        "symbol": "SecuritiesCompanyCode",
        "margin_buy": "MarginPurchase",
        "margin_sell": "MarginSales",
        "margin_cash_repay": "CashRedemption",
        "margin_balance": "MarginPurchaseBalance",
        "short_sell": "ShortSale",
        "short_buy": "ShortCovering",
        "short_stock_repay": "StockRedemption",
        "short_balance": "ShortSaleBalance",
    }

    # Find actual column names (case insensitive)
    # 不變量：df_cols_lower 的值全部取自 df.columns，故 cols 的每個值必定是
    # df 的真實欄位或 None。下面各處只需檢查真假，不需要再 `in df.columns`
    # ——那是恆為真的冗餘檢查（prepare_twse_margin 走 _find_columns，
    # 回傳的也是 df.columns 的成員或 None，同樣的不變量）。
    df_cols_lower = {c.lower(): c for c in df.columns}
    cols = {}
    for std_name, tpex_name in col_mapping.items():
        actual_col = df_cols_lower.get(tpex_name.lower())
        cols[std_name] = actual_col

    symbol_col = cols.get("symbol")
    if not symbol_col:
        raise DataUnavailableError("TPEX 融資融券欄位解析失敗，缺少 symbol")

    result = pd.DataFrame()
    result["symbol"] = df[symbol_col].astype(str).str.strip()

    result["margin_buy"] = _int_col_or_nulls(df, cols, "margin_buy")
    result["margin_sell"] = _int_col_or_nulls(df, cols, "margin_sell")
    result["margin_balance"] = _int_col_or_nulls(df, cols, "margin_balance")
    result["short_sell"] = _int_col_or_nulls(df, cols, "short_sell")
    result["short_buy"] = _int_col_or_nulls(df, cols, "short_buy")
    result["short_balance"] = _int_col_or_nulls(df, cols, "short_balance")

    # Calculate margin_change: buy - sell - cash_repay
    margin_buy_col = cols.get("margin_buy")
    margin_sell_col = cols.get("margin_sell")
    margin_cash_col = cols.get("margin_cash_repay")

    if margin_buy_col and margin_sell_col:
        margin_buy = df[margin_buy_col].map(_clean_int)
        margin_sell = df[margin_sell_col].map(_clean_int)
        margin_change = margin_buy - margin_sell
        if margin_cash_col:
            margin_cash = df[margin_cash_col].map(_clean_int)
            margin_change = margin_change - margin_cash.fillna(0)
        result["margin_change"] = margin_change.map(lambda x: int(x) if pd.notna(x) else None)
    else:
        result["margin_change"] = None

    # Calculate short_change: sell - buy - stock_repay
    short_sell_col = cols.get("short_sell")
    short_buy_col = cols.get("short_buy")
    short_stock_col = cols.get("short_stock_repay")

    if short_sell_col and short_buy_col:
        short_sell_raw = df[short_sell_col].map(_clean_int)
        short_buy_raw = df[short_buy_col].map(_clean_int)
        short_change = short_sell_raw - short_buy_raw
        if short_stock_col:
            short_stock = df[short_stock_col].map(_clean_int)
            short_change = short_change - short_stock.fillna(0)
        result["short_change"] = short_change.map(lambda x: int(x) if pd.notna(x) else None)
    else:
        result["short_change"] = None

    return result


def prepare_tpex_margin_v2(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare TPEX margin V2 data (Chinese column names) into standard format.

    Columns: symbol, margin_buy, margin_sell, margin_balance, margin_change,
             short_sell, short_buy, short_balance, short_change, short_margin_ratio
    Units: lots (張). margin_change/short_change are computed from balance diffs.
    """
    cols = _find_columns(df, {
        "symbol": [["代號"], ["證券代號"]],
        "margin_buy": [["資買"]],
        "margin_sell": [["資賣"]],
        "margin_balance": [["資餘額"]],
        "margin_cash_repay": [["現償"]],
        "prev_margin_balance": [["前資餘額"]],
        "short_sell": [["券賣"]],
        "short_buy": [["券買"]],
        "short_balance": [["券餘額"]],
        "short_stock_repay": [["券償"]],
        "prev_short_balance": [["前券餘額"]],
    })

    # Fix ambiguous substring matches: "資餘額" matches "前資餘額(張)" first because
    # it appears earlier in the API response. Resolve by re-finding the non-"前" column.
    for bal_key, prev_key in [
        ("margin_balance", "prev_margin_balance"),
        ("short_balance", "prev_short_balance"),
    ]:
        if cols.get(bal_key) and cols.get(prev_key) and cols[bal_key] == cols[prev_key]:
            prev_col = cols[prev_key]
            for col in df.columns:
                norm = _normalize_col(str(col))
                prev_norm = _normalize_col(str(prev_col))
                if norm != prev_norm and _normalize_col("餘額") in norm and "前" not in norm:
                    # Verify it's the right type (資 or 券)
                    prefix = "資" if "margin" in bal_key else "券"
                    if prefix in norm:
                        cols[bal_key] = col
                        break

    temp = _extract_standard_columns(
        df, cols,
        required=["symbol", "margin_buy", "margin_sell", "margin_balance",
                   "short_sell", "short_buy", "short_balance"],
        error_msg="TPEX V2 融資融券欄位解析失敗",
    )

    result = pd.DataFrame()
    result["symbol"] = temp["symbol"].astype(str).str.strip()

    for col in ["margin_buy", "margin_sell", "margin_balance",
                "short_sell", "short_buy", "short_balance"]:
        result[col] = temp[col].map(_clean_int)

    # margin_change = 資餘額 - 前資餘額 (fallback: buy - sell - 現償)
    if "prev_margin_balance" in temp.columns:
        prev_margin = temp["prev_margin_balance"].map(_clean_int)
        result["margin_change"] = result["margin_balance"] - prev_margin
    else:
        cash_repay = temp["margin_cash_repay"].map(_clean_int) if "margin_cash_repay" in temp.columns else 0
        result["margin_change"] = result["margin_buy"] - result["margin_sell"] - cash_repay

    # short_change = 券餘額 - 前券餘額 (fallback: sell - buy - 券償)
    if "prev_short_balance" in temp.columns:
        prev_short = temp["prev_short_balance"].map(_clean_int)
        result["short_change"] = result["short_balance"] - prev_short
    else:
        stock_repay = temp["short_stock_repay"].map(_clean_int) if "short_stock_repay" in temp.columns else 0
        result["short_change"] = result["short_sell"] - result["short_buy"] - stock_repay

    # short_margin_ratio = short_balance / margin_balance (None when margin_balance is 0)
    mb = result["margin_balance"].astype(float)
    sb = result["short_balance"].astype(float)
    ratio = sb / mb
    result["short_margin_ratio"] = ratio.where(mb != 0, other=None)

    return result


# 處置公告裡描述撮合頻率的句子。TWSE 用國字（「約每二十五分鐘撮合一次」），
# TPEX 用阿拉伯數字（「約每25分鐘撮合一次」），兩種都要吃。
_DISPOSITION_MINUTES_RE = re.compile(
    r"約每([0-9０-９一二三四五六七八九十]+)分鐘撮合一次"
)
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_to_int(text: str) -> int | None:
    """把「25」「２５」「十」「二十」「二十五」等轉成 int；無法解析回 None。

    只需支援處置公告出現過的量級（實測值域 5/10/20/25/45/60），故十進位規則
    只處理「X十Y」單一個十位，不做完整中文數字剖析。
    """
    normalized = text.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    if "十" not in normalized:
        return _CN_DIGITS.get(normalized)

    head, _, tail = normalized.partition("十")
    tens = 1 if not head else _CN_DIGITS.get(head)
    ones = 0 if not tail else _CN_DIGITS.get(tail)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _parse_disposition_period(value: str) -> tuple[dt.date | None, dt.date | None]:
    """拆解處置起訖字串成 (start, end)。

    分隔符兩家不同：TWSE 用全形「～」、TPEX v2 用半形「~」。日期為民國年，
    v2 給 `115/06/30` 形式，OpenAPI 快照版則是 `1150630`，兩種都接。
    """
    parts = re.split(r"[～~]", str(value).strip())
    if len(parts) != 2:
        return None, None
    bounds = []
    for part in parts:
        text = part.strip()
        bounds.append(_parse_roc_date(text) or _parse_roc_date_compact(text))
    return bounds[0], bounds[1]


_DISPOSITION_COLUMNS = ["symbol", "start_date", "end_date", "match_minutes"]


def prepare_disposition(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize 處置公告（TWSE / TPEX 通用）成 symbol/start_date/end_date/match_minutes。

    兩家欄位名幾乎一致，只有期間欄用字不同（TWSE「處置起迄時間」、TPEX「處置起訖時間」），
    故以關鍵字比對吃下兩者。

    丟棄的列：
    - 證券代號為空 —— TPEX 會夾帶「本日無處置資料」的佔位列。
    - 處置期間無法解析成合法且 start <= end 的民國日期區間。
    撮合分鐘數解析不到時仍保留該列、match_minutes 為 None（處置這件事仍然成立）。

    名單含權證（6 碼）與可轉債（5 碼），此處刻意不濾：呼叫端只寫 stocks 內的
    symbol，非個股自然被排除。可轉債的處置內容偶爾夾兩段撮合描述（併同標的股票
    處置），一律取第一個出現的數字＝該公告主區間的撮合頻率。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_DISPOSITION_COLUMNS)

    cols = _find_columns(df, {
        "symbol": [["證券代號"], ["代號"]],
        "period": [["處置起迄"], ["處置起訖"], ["處置期間"]],
        "detail": [["處置內容"]],
    })
    if not cols["symbol"] or not cols["period"]:
        raise DataUnavailableError("處置股資料缺少證券代號或處置期間欄位。")

    rows = []
    for _, row in df.iterrows():
        symbol = str(row[cols["symbol"]]).strip()
        if not symbol or symbol.lower() == "nan":
            continue

        start_date, end_date = _parse_disposition_period(row[cols["period"]])
        if start_date is None or end_date is None or end_date < start_date:
            continue

        match_minutes = None
        if cols["detail"]:
            found = _DISPOSITION_MINUTES_RE.search(str(row[cols["detail"]]))
            if found:
                match_minutes = _cn_to_int(found.group(1))

        rows.append({
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "match_minutes": match_minutes,
        })

    return pd.DataFrame(rows, columns=_DISPOSITION_COLUMNS)


def prepare_moneydj_margin(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare MoneyDJ margin trading data into standard format.

    Input DataFrame from fetch_moneydj_margin with columns:
    date, margin_buy, margin_sell, margin_balance, margin_change,
    short_sell, short_buy, short_balance, short_change

    Returns DataFrame with same columns but with parsed dates and cleaned integers.
    Units: lots/張 (already in lots from MoneyDJ)
    """
    if "date" not in df.columns:
        raise DataUnavailableError("MoneyDJ 融資融券欄位解析失敗，缺少 date")

    result = pd.DataFrame()

    # Parse ROC dates (民國, e.g., 115/02/11) to gregorian
    def _parse_moneydj_date(val) -> dt.date | None:
        if pd.isna(val):
            return None
        text = str(val).strip()
        if not text:
            return None
        return _parse_roc_date(text)

    result["date"] = df["date"].map(_parse_moneydj_date)

    # MoneyDJ values are already in lots (張), no conversion needed
    for col_name in ["margin_buy", "margin_sell", "margin_balance", "margin_change",
                     "short_sell", "short_buy", "short_balance", "short_change"]:
        if col_name in df.columns:
            result[col_name] = df[col_name].map(_clean_int)
        else:
            result[col_name] = None

    # 券資比: always calculate from short_balance / margin_balance
    # (MoneyDJ provides rounded integer % which is imprecise, so we ignore it)
    mb = result["margin_balance"].astype(float)
    sb = result["short_balance"].astype(float)
    ratio = sb / mb
    result["short_margin_ratio"] = ratio.where(mb != 0, other=None)

    # Drop rows with invalid dates
    result = result.dropna(subset=["date"])

    return result


def prepare_moneydj_holding_pct(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare MoneyDJ institutional holding percentage data.

    Input DataFrame from fetch_moneydj_holding_pct with columns:
    date, foreign_holding_pct, insti_holding_pct

    Returns DataFrame with parsed dates and percentages as decimals (e.g., 0.3503).
    """
    if "date" not in df.columns:
        raise DataUnavailableError("MoneyDJ 法人持股欄位解析失敗，缺少 date")

    result = pd.DataFrame()

    def _parse_moneydj_date(val) -> dt.date | None:
        if pd.isna(val):
            return None
        text = str(val).strip()
        if not text:
            return None
        return _parse_roc_date(text)

    result["date"] = df["date"].map(_parse_moneydj_date)

    # Parse percentage strings like "35.03%" to decimal 0.3503
    def _parse_pct_to_decimal(val):
        if pd.isna(val):
            return None
        text = str(val).strip().replace("%", "")
        try:
            return round(float(text) / 100, 6)
        except ValueError:
            return None

    for col in ["foreign_holding_pct", "insti_holding_pct"]:
        if col in df.columns:
            result[col] = df[col].map(_parse_pct_to_decimal)
        else:
            result[col] = None

    result = result.dropna(subset=["date"])

    return result


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


# 大戶門檻：400 張 = 400,000 股。TDCC 分級在 400,000/400,001 之間切開，
# 故「400 張以上」= 分級下界 >= 400,001 的所有級距（400,001-600,000 起算）。
_MAJOR_HOLDER_MIN_SHARES = 400_001
# 散戶門檻：20 張 = 20,000 股。TDCC 分級在 20,000/20,001 之間切開，
# 故「小於 20 張」= 分級下界 <= 20,000 的所有級距（含 15,001-20,000，排除 20,001-30,000 起）。
_RETAIL_HOLDER_MAX_SHARES = 20_000


def _grade_lower_bound(val) -> int | None:
    """解析 TDCC「持股/單位數分級」字串的數字下界（如 '400,001-600,000' → 400001）。

    「差異數調整」「合計」等無數字開頭的列回 None（呼叫端據此排除）。
    """
    if pd.isna(val):
        return None
    match = re.match(r"^([\d,]+)", str(val).strip())
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _sum_grade_ratio(df: pd.DataFrame, include) -> float | None:
    """加總符合 ``include(lower_bound)`` 的級距「占集保庫存數比例」。

    自動排除無數字下界（差異數調整 / 合計）的列。

    Returns:
        ratio as decimal rounded to 6 places; None if columns missing 或沒有任何
        符合且可解析比例的級距（表示「無法解析」，避免寫入假性的 0.0）。
        真實的 0.00% 級距仍會被計入（n_parsed > 0）而正確回傳 0.0。
    """
    grade_col = _find_column(df, ["持股"])
    pct_col = _find_column(df, ["占集保庫存數比例"])
    if not grade_col or not pct_col:
        return None

    total_pct = 0.0
    n_parsed = 0
    for _, row in df.iterrows():
        lower = _grade_lower_bound(row[grade_col])
        if lower is None or not include(lower):
            continue
        # TDCC 多以裸數字回傳，但保險起見先去掉可能存在的 "%"（字串值才需處理）。
        raw_pct = row[pct_col]
        pct = _clean_number(raw_pct.replace("%", "") if isinstance(raw_pct, str) else raw_pct)
        if pct is not None:
            total_pct += pct
            n_parsed += 1

    if n_parsed == 0:
        return None

    return round(total_pct / 100, 6)


def prepare_tdcc_major_ratio(df: pd.DataFrame) -> float | None:
    """Compute 大戶持股佔比 from a TDCC 集保戶股權分散表 DataFrame.

    加總「持股/單位數分級」下界 >= 400,001 股（即 400 張以上）的「占集保庫存數比例」，
    自動排除「差異數調整」「合計」等無數字下界的列，回傳比例小數（如 0.7572）。

    Returns:
        ratio as decimal rounded to 6 places (e.g. 0.7572); None if columns missing.
    """
    return _sum_grade_ratio(df, lambda lower: lower >= _MAJOR_HOLDER_MIN_SHARES)


def prepare_tdcc_retail_ratio(df: pd.DataFrame) -> float | None:
    """Compute 散戶持股佔比 from a TDCC 集保戶股權分散表 DataFrame.

    加總「持股/單位數分級」下界 <= 20,000 股（即小於 20 張）的「占集保庫存數比例」，
    自動排除「差異數調整」「合計」等無數字下界的列，回傳比例小數（如 0.1520）。

    Returns:
        ratio as decimal rounded to 6 places (e.g. 0.1520); None if columns missing.
    """
    return _sum_grade_ratio(df, lambda lower: lower <= _RETAIL_HOLDER_MAX_SHARES)


# ---------------------------------------------------------------------------
# 個股月表展開
#
# 這兩個函式不發任何 HTTP：輸入 DataFrame、輸出 dict，屬於 normalize 而非抓取，
# 故與其他 prepare_* 同住此檔。兩者結構同構但只重複兩次，未達 CLAUDE.md 的
# 「重複三次以上才抽象」門檻，刻意不合併。
# ---------------------------------------------------------------------------


def expand_twse_stock_day(
    df: pd.DataFrame,
) -> dict[dt.date, dict[str, float | int | None]]:
    """把 TWSE STOCK_DAY 月表展開成 `date -> {open/high/low/close/volume/change}`。

    `volume` 單位是**股**（STOCK_DAY 的「成交股數」本來就是股，直接沿用）。
    成交量欄位名稱吃「成交股數」與「成交量」兩種——與 `find_twse_ohlcv` 的容忍度
    一致。少了別名時，若 TWSE 改用「成交量」，OHLC 有值而 volume 為 None 的列
    仍會被寫進 DB（`_build_daily_rows` 只看價格決定跳不跳），`turnover_rate`
    也跟著變 NULL，靜默缺一欄。

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
        volume = _clean_int(row.get("成交股數"))
        if volume is None:
            volume = _clean_int(row.get("成交量"))
        out[date] = {
            "open": _clean_number(row.get("開盤價")),
            "high": _clean_number(row.get("最高價")),
            "low": _clean_number(row.get("最低價")),
            "close": _clean_number(row.get("收盤價")),
            "volume": volume,
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
