"""規則共用的名詞定義（PROJECT_PLAN.md 第 3 節）。

「現價」為今日 K 棒的收盤（盤中為暫定 K 棒）；「昨收」為前一根 K 棒的收盤。
算不出所需數值（歷史不足）時，條件一律視為不成立（D23）。
"""

import math
from collections.abc import Sequence

from screener.analysis.numeric import normalize
from screener.models import StockContext


def is_red(ctx: StockContext) -> bool:
    """紅 K：現價 > 開盤。"""
    return bool(ctx.today["close"] > ctx.today["open"])


def is_black(ctx: StockContext) -> bool:
    """黑 K：現價 < 開盤。"""
    return bool(ctx.today["close"] < ctx.today["open"])


def body_pct(ctx: StockContext) -> float | None:
    """K 棒實體占昨收的百分比：(現價 − 開盤) / 昨收 × 100。"""
    if ctx.yesterday is None:
        return None
    return normalize((ctx.today["close"] - ctx.today["open"]) / ctx.yesterday["close"] * 100)


def is_long_red(ctx: StockContext, min_body_pct: float) -> bool:
    """長紅：紅 K，且 (現價 − 開盤) / 昨收 ≥ min_body_pct%。"""
    if ctx.yesterday is None:
        return False
    body = normalize((ctx.today["close"] - ctx.today["open"]) * 100)     # 以乘法比較，避免除法誤差
    return is_red(ctx) and bool(body >= normalize(min_body_pct * ctx.yesterday["close"]))


def is_long_black(ctx: StockContext, min_body_pct: float) -> bool:
    """長黑：黑 K，且 (開盤 − 現價) / 昨收 ≥ min_body_pct%。"""
    if ctx.yesterday is None:
        return False
    body = normalize((ctx.today["open"] - ctx.today["close"]) * 100)     # 以乘法比較，避免除法誤差
    return is_black(ctx) and bool(body >= normalize(min_body_pct * ctx.yesterday["close"]))


def volume_ratio(ctx: StockContext) -> float | None:
    """今日量 / 昨日量。"""
    if ctx.yesterday is None or ctx.yesterday["volume"] <= 0:
        return None
    return float(ctx.today["volume"] / ctx.yesterday["volume"])


def is_volume_spike(ctx: StockContext, ratio: float) -> bool:
    """爆量：今日成交量 ≥ 昨日成交量 × ratio。昨日成交量為 0 時算不出量比，視為不成立（D23、D39）。"""
    if ctx.yesterday is None or ctx.yesterday["volume"] <= 0:
        return False
    return bool(ctx.today["volume"] >= normalize(ctx.yesterday["volume"] * ratio))


def crosses_above_ma(ctx: StockContext, n: int) -> bool:
    """突破 MA n：昨收 ≤ 昨日 MA n，且現價 > 今日 MA n。"""
    if ctx.yesterday is None:
        return False
    ma = ctx.ma(n)
    if math.isnan(ma.iloc[-1]) or math.isnan(ma.iloc[-2]):
        return False
    return bool(ctx.yesterday["close"] <= ma.iloc[-2] and ctx.today["close"] > ma.iloc[-1])


def crosses_below_ma(ctx: StockContext, n: int) -> bool:
    """跌破 MA n：昨收 ≥ 昨日 MA n，且現價 < 今日 MA n。"""
    if ctx.yesterday is None:
        return False
    ma = ctx.ma(n)
    if math.isnan(ma.iloc[-1]) or math.isnan(ma.iloc[-2]):
        return False
    return bool(ctx.yesterday["close"] >= ma.iloc[-2] and ctx.today["close"] < ma.iloc[-1])


def is_above_ma(ctx: StockContext, n: int) -> bool:
    """站上 MA n：現價 > 今日 MA n。"""
    ma = ctx.ma(n).iloc[-1]
    return not math.isnan(ma) and bool(ctx.today["close"] > ma)


def is_below_ma(ctx: StockContext, n: int) -> bool:
    """在 MA n 之下：現價 < 今日 MA n。"""
    ma = ctx.ma(n).iloc[-1]
    return not math.isnan(ma) and bool(ctx.today["close"] < ma)


def prior_high(ctx: StockContext, days: int) -> float | None:
    """前高：不含今日，最近 days 根 K 棒的最高價；今日之前不足 days 根時為 None。"""
    highs = ctx.bars["high"].iloc[:-1]
    if len(highs) < days:
        return None
    return float(highs.iloc[-days:].max())


def prior_low(ctx: StockContext, days: int) -> float | None:
    """前低：不含今日，最近 days 根 K 棒的最低價；今日之前不足 days 根時為 None。"""
    lows = ctx.bars["low"].iloc[:-1]
    if len(lows) < days:
        return None
    return float(lows.iloc[-days:].min())


def ma_values(ctx: StockContext, mas: Sequence[int], days_ago: int = 0) -> list[float] | None:
    """days_ago 天前（0 為今日）的各條均線值；任一條算不出來時為 None。"""
    position = len(ctx.bars) - 1 - days_ago
    if position < 0:
        return None
    values = [float(ctx.ma(n).iloc[position]) for n in mas]
    return None if any(math.isnan(v) for v in values) else values


def is_strictly_descending(values: Sequence[float]) -> bool:
    """多頭排列：短均線 > 長均線，例 MA5 > MA10 > MA20。"""
    return all(a > b for a, b in zip(values, values[1:]))


def is_strictly_ascending(values: Sequence[float]) -> bool:
    """空頭排列：短均線 < 長均線，例 MA5 < MA10 < MA20。"""
    return all(a < b for a, b in zip(values, values[1:]))
