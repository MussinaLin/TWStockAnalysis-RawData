"""台股漲跌停價計算（升降單位級距 + ±10% 限制）。

參考價 × 1.1 無條件捨去到升降單位 = 漲停價；× 0.9 無條件進位 = 跌停價。
級距由「算出來的漲/跌停價」決定，不是由參考價決定。

升降單位分兩套：
- 普通股（含 KY、TDR）：六級（10 / 50 / 100 / 500 / 1000 元分界）。
- ETF（含主動式，如 00981A）：兩級，未滿 50 元 0.01、50 元以上 0.05。
  ETF 由代號判定（`is_etf`），呼叫端以 `etf=True` 切換。

ETF 的漲跌幅只有國內成分 ETF 是 ±10%；含海外股票的 ETF 無漲跌幅限制，這裡
**不分辨**，一律照 ±10% 算。這類 ETF 只有在成交價跑出算出來的區間時，才會被
`run._price_limits` 的無拘束力判斷清成 NULL，其餘日子會留下假區間 —— 目前標的
池只有國內成分 ETF，加入含海外股票的 ETF 前要先處理。
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

# ETF：未滿 50 元 0.01、50 元以上 0.05（TWSE ETF 交易制度）。
_ETF_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("50"), Decimal("0.01")),
)
_ETF_TICK_TOP = Decimal("0.05")

_LIMIT_UP_RATE = Decimal("1.1")
_LIMIT_DOWN_RATE = Decimal("0.9")


def is_etf(symbol: str | None) -> bool:
    """台股 ETF 代號一律 `00` 開頭（如 0050、006208、00981A），普通股不會。"""
    return symbol is not None and str(symbol).strip().startswith("00")


def tick_size(price: Decimal, *, etf: bool = False) -> Decimal:
    """該價位適用的升降單位；etf=True 用 ETF 的兩級制。"""
    bands, top = (_ETF_TICK_BANDS, _ETF_TICK_TOP) if etf else (_TICK_BANDS, _TICK_TOP)
    for upper, tick in bands:
        if price < upper:
            return tick
    return top


def calc_limits(
    ref: Decimal | None, *, etf: bool = False
) -> tuple[Decimal | None, Decimal | None]:
    """由參考價算出 (漲停價, 跌停價)；etf=True 用 ETF 的升降單位。

    ref 為 None（除權息日 / 無成交，推不出參考價）時回傳 (None, None)，不做推測。
    """
    if ref is None:
        return None, None

    up_raw = ref * _LIMIT_UP_RATE
    up_tick = tick_size(up_raw, etf=etf)
    limit_up = (up_raw / up_tick).to_integral_value(rounding=ROUND_FLOOR) * up_tick

    down_raw = ref * _LIMIT_DOWN_RATE
    down_tick = tick_size(down_raw, etf=etf)
    limit_down = (down_raw / down_tick).to_integral_value(rounding=ROUND_CEILING) * down_tick

    return limit_up, limit_down
