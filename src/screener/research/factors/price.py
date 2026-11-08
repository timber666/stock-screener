"""價量因子：動能、短期反轉、波動、流動性（RESEARCH_PLAN.md 4.4）。"""

import numpy as np
import pandas as pd

from screener.research.factors.base import FactorContext, register_factor
from screener.research.factors.panel import per_stock


@register_factor("momentum", category="動能")
def momentum(context: FactorContext, *, lookback: int, skip: int) -> pd.DataFrame:
    """t−lookback → t−skip 的報酬（跳過最近一段）。"""
    return per_stock(context.panel.close, lambda s: s.shift(skip) / s.shift(lookback) - 1)


@register_factor("past_return", category="短期反轉")
def past_return(context: FactorContext, *, days: int) -> pd.DataFrame:
    return per_stock(context.panel.close, lambda s: s / s.shift(days) - 1)


@register_factor("volatility", category="波動")
def volatility(context: FactorContext, *, days: int) -> pd.DataFrame:
    """日報酬的標準差。"""
    return per_stock(context.panel.close, lambda s: s.pct_change(fill_method=None).rolling(days).std())


@register_factor("max_daily_return", category="波動")
def max_daily_return(context: FactorContext, *, days: int) -> pd.DataFrame:
    return per_stock(context.panel.close, lambda s: s.pct_change(fill_method=None).rolling(days).max())


@register_factor("average_value", category="流動性")
def average_value(context: FactorContext, *, days: int) -> pd.DataFrame:
    """平均成交金額的對數；沒有成交的日子算 0（同股票池的算法，R7）。"""
    average = context.panel.value.rolling(days, min_periods=days).mean()
    return np.log(average.where(average > 0))


@register_factor("value_surge", category="流動性")
def value_surge(context: FactorContext, *, short: int, long: int) -> pd.DataFrame:
    """短期平均成交金額 ÷ 長期平均成交金額。"""
    value = context.panel.value
    baseline = value.rolling(long, min_periods=long).mean()
    return value.rolling(short, min_periods=short).mean() / baseline.where(baseline > 0)
