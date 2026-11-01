"""台股的升降單位與漲跌停價（PROJECT_PLAN.md 14.1「漲跌停」）。

上市、上櫃股票相同：漲跌幅 10%，以參考價（昨收；除權息、減資日為事件後參考價）計算，
再依升降單位取到可成交的價位：漲停價無條件捨去、跌停價無條件進位。
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

PRICE_LIMIT = Decimal("0.10")
# (價格上限, 升降單位)：未滿 10 元 0.01、未滿 50 元 0.05、未滿 100 元 0.1、未滿 500 元 0.5、未滿 1000 元 1，其餘 5
_TICKS = ((Decimal(10), Decimal("0.01")), (Decimal(50), Decimal("0.05")), (Decimal(100), Decimal("0.1")),
          (Decimal(500), Decimal("0.5")), (Decimal(1000), Decimal(1)))
_TOP_TICK = Decimal(5)


def tick_size(price: Decimal) -> Decimal:
    for bound, tick in _TICKS:
        if price < bound:
            return tick
    return _TOP_TICK


def limit_prices(reference: float) -> tuple[float, float]:
    """(漲停價, 跌停價)。"""
    ref = Decimal(repr(reference))
    up = ref * (1 + PRICE_LIMIT)
    down = ref * (1 - PRICE_LIMIT)
    up = (up / tick_size(up)).to_integral_value(ROUND_FLOOR) * tick_size(up)
    down = (down / tick_size(down)).to_integral_value(ROUND_CEILING) * tick_size(down)
    return float(up), float(down)
