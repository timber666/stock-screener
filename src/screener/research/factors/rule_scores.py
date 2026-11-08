"""把現有的技術規則改寫成連續分數（RESEARCH_PLAN.md 4.4）。

規則只回答「成立／不成立」；這裡保留程度的資訊。名詞的意思同 PROJECT_PLAN.md 第 3 節，但價格用還原權息後的
價格（R18），均線與前高、前低都以該檔自己的 K 棒計算。
"""

import pandas as pd

from screener.research.factors.base import FactorContext, register_factor
from screener.research.factors.panel import per_stock


@register_factor("ma_gap", category="現有規則")
def ma_gap(context: FactorContext, *, ma: int) -> pd.DataFrame:
    """收盤 / MA − 1：站上均線的程度（月線是前置條件，五日線是 E1 與交易回測的「距 MA5 最近」）。"""
    return per_stock(context.panel.close, lambda s: s / s.rolling(ma).mean() - 1)


@register_factor("volume_ratio", category="現有規則")
def volume_ratio(context: FactorContext) -> pd.DataFrame:
    """今日量 / 昨日量（E2 的爆量、交易回測的「量比最大」）；昨日 0 張時算不出來（D39）。"""
    return per_stock(context.panel.volume, lambda s: s / s.shift(1).where(lambda v: v > 0))


@register_factor("candle_body", category="現有規則")
def candle_body(context: FactorContext) -> pd.DataFrame:
    """(收盤 − 開盤) / 昨收：長紅為正、長黑為負。"""
    panel = context.panel
    previous = per_stock(panel.close, lambda s: s.shift(1))
    return (panel.close - panel.open) / previous


@register_factor("high_gap", category="現有規則")
def high_gap(context: FactorContext, *, days: int) -> pd.DataFrame:
    """收盤 / 前高 − 1：前高不含今日（E2）。"""
    panel = context.panel
    previous_high = per_stock(panel.high, lambda s: s.shift(1).rolling(days).max())
    return panel.close / previous_high - 1


@register_factor("low_gap", category="現有規則")
def low_gap(context: FactorContext, *, days: int) -> pd.DataFrame:
    """收盤 / 前低 − 1：前低不含今日（E6）。"""
    panel = context.panel
    previous_low = per_stock(panel.low, lambda s: s.shift(1).rolling(days).min())
    return panel.close / previous_low - 1


@register_factor("ma_spread", category="現有規則")
def ma_spread(context: FactorContext, *, short: int, long: int) -> pd.DataFrame:
    """短均線 / 長均線 − 1：三線排列的程度。"""
    return per_stock(context.panel.close, lambda s: s.rolling(short).mean() / s.rolling(long).mean() - 1)
