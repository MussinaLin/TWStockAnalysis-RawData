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
