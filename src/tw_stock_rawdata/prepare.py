"""Data preparation and normalization functions for stock data."""

from __future__ import annotations

import re

import pandas as pd

import datetime as dt

from .sources import _clean_int, _clean_number, _parse_roc_date, DataUnavailableError


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

    def _get_int_col(col_name: str) -> pd.Series:
        src_col = cols.get(col_name)
        if src_col:
            return df[src_col].map(_clean_int)
        return pd.Series([None] * len(df))

    result["margin_buy"] = _get_int_col("margin_buy")
    result["margin_sell"] = _get_int_col("margin_sell")
    result["margin_balance"] = _get_int_col("margin_balance")
    result["short_sell"] = _get_int_col("short_sell")
    result["short_buy"] = _get_int_col("short_buy")
    result["short_balance"] = _get_int_col("short_balance")

    # Calculate margin_change: buy - sell - cash_repay
    margin_buy = _get_int_col("margin_buy")
    margin_sell = _get_int_col("margin_sell")
    margin_cash = _get_int_col("margin_cash_repay")

    if margin_buy is not None and margin_sell is not None:
        margin_change = margin_buy - margin_sell
        if margin_cash is not None:
            margin_change = margin_change - margin_cash.fillna(0)
        result["margin_change"] = margin_change.map(lambda x: int(x) if pd.notna(x) else None)
    else:
        result["margin_change"] = None

    # Calculate short_change: sell - buy - stock_repay
    short_sell_raw = _get_int_col("short_sell")
    short_buy_raw = _get_int_col("short_buy")
    short_stock = _get_int_col("short_stock_repay")

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

    def _get_int_col(col_name: str) -> pd.Series:
        src_col = cols.get(col_name)
        if src_col and src_col in df.columns:
            return df[src_col].map(_clean_int)
        return pd.Series([None] * len(df))

    result["margin_buy"] = _get_int_col("margin_buy")
    result["margin_sell"] = _get_int_col("margin_sell")
    result["margin_balance"] = _get_int_col("margin_balance")
    result["short_sell"] = _get_int_col("short_sell")
    result["short_buy"] = _get_int_col("short_buy")
    result["short_balance"] = _get_int_col("short_balance")

    # Calculate margin_change: buy - sell - cash_repay
    margin_buy_col = cols.get("margin_buy")
    margin_sell_col = cols.get("margin_sell")
    margin_cash_col = cols.get("margin_cash_repay")

    if margin_buy_col and margin_sell_col:
        margin_buy = df[margin_buy_col].map(_clean_int)
        margin_sell = df[margin_sell_col].map(_clean_int)
        margin_change = margin_buy - margin_sell
        if margin_cash_col and margin_cash_col in df.columns:
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
        if short_stock_col and short_stock_col in df.columns:
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


# 大戶門檻：400 張 = 400,000 股。TDCC 分級在 400,000/400,001 之間切開，
# 故「400 張以上」= 分級下界 >= 400,001 的所有級距（400,001-600,000 起算）。
_MAJOR_HOLDER_MIN_SHARES = 400_001


def prepare_tdcc_major_holder_ratio(df: pd.DataFrame) -> float | None:
    """Compute 大戶持股佔比 from a TDCC 集保戶股權分散表 DataFrame.

    加總「持股/單位數分級」下界 >= 400,001 股（即 400 張以上）的「占集保庫存數比例」，
    自動排除「差異數調整」「合計」等無數字下界的列，回傳比例小數（如 0.7572）。

    Returns:
        ratio as decimal rounded to 6 places (e.g. 0.7572); None if columns missing.
    """
    grade_col = _find_column(df, ["持股"])
    pct_col = _find_column(df, ["占集保庫存數比例"])
    if not grade_col or not pct_col:
        return None

    def _grade_lower_bound(val) -> int | None:
        if pd.isna(val):
            return None
        match = re.match(r"^([\d,]+)", str(val).strip())
        if not match:
            return None
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None

    total_pct = 0.0
    n_parsed = 0
    for _, row in df.iterrows():
        lower = _grade_lower_bound(row[grade_col])
        if lower is None or lower < _MAJOR_HOLDER_MIN_SHARES:
            continue
        # TDCC 多以裸數字回傳，但保險起見先去掉可能存在的 "%"（字串值才需處理）。
        raw_pct = row[pct_col]
        pct = _clean_number(raw_pct.replace("%", "") if isinstance(raw_pct, str) else raw_pct)
        if pct is not None:
            total_pct += pct
            n_parsed += 1

    # 沒有任何一個 >= 400 張的級距解析出比例（欄位改版 / "--" / 空白）時，回 None
    # 表示「無法解析」，避免把假性的 0.0 寫進 stock_major_holder；
    # 真實的 0.00% 級距仍會被計入（n_parsed > 0）而正確回傳 0.0。
    if n_parsed == 0:
        return None

    return round(total_pct / 100, 6)
