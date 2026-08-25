"""把抓取結果轉成可跨 branch 逐欄比對的 JSON 表示。

原則：**寧可保留精度差異，也不要抹平**。
- Decimal 轉字串而非 float——漲跌停價的精度就是重點，轉 float 會把
  `2415.50` 與 `2415.5000` 的差異抹掉。
- float 只 round 到小數 10 位，擋掉浮點尾數雜訊，但保留真實差異。
- NaN 一律轉 None，因為 `NaN != NaN` 會讓相同的兩份資料被判成不同。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

MAX_DEPTH = 5


def enc(v, depth: int = 0):
    """遞迴序列化。DataFrame / Series / NamedTuple / Decimal 都展開成純資料。"""
    if depth > MAX_DEPTH:
        return "<deep>"
    if v is None:
        return None
    if isinstance(v, pd.DataFrame):
        # 依「位置」序列化，不用 to_dict("records")——後者對重名欄位會**靜默丟掉**
        # 其中幾欄（TPEX 三大法人的表就是重名欄，每個法人類別都有買進/賣出/買賣超），
        # 那會讓比對對那些欄位完全盲掉，卻不會有任何錯誤訊息。
        return {"__df__": {"cols": [str(c) for c in v.columns],
                           "shape": list(v.shape),
                           "index": [enc(i, depth + 1) for i in v.index],
                           "rows": [[enc(x, depth + 1) for x in row]
                                    for row in v.itertuples(index=False, name=None)]}}
    if isinstance(v, pd.Series):
        return {"__series__": {str(k): enc(x, depth + 1) for k, x in v.items()}}
    if isinstance(v, Decimal):
        return f"D:{v}"
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (bool, str, int)):
        return v
    if isinstance(v, float):
        return None if v != v else round(v, 10)          # NaN -> None
    if isinstance(v, (list, tuple)):
        return [enc(x, depth + 1) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(str(x) for x in v)
    if isinstance(v, dict):
        return {str(k): enc(x, depth + 1)
                for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if hasattr(v, "_asdict"):                            # NamedTuple
        return {"__nt__": v.__class__.__name__, "f": enc(dict(v._asdict()), depth + 1)}
    if hasattr(v, "item"):                               # numpy scalar
        return enc(v.item(), depth + 1)
    return f"<{type(v).__name__}>"


def arg_summary(args, kwargs):
    """呼叫參數摘要。session 這種不可比對的物件用佔位字串代替；
    DataFrame 只留 shape——它的內容會在該次呼叫的 ret 裡出現，重複記一次沒有意義。"""
    def one(x):
        if hasattr(x, "get") and hasattr(x, "post"):
            return "<session>"
        if isinstance(x, pd.DataFrame):
            return {"__shape__": list(x.shape)}
        return enc(x, 4)
    return {"a": [one(x) for x in args],
            "k": {k: one(v) for k, v in sorted(kwargs.items())}}
