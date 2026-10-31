"""趨勢分類與破位修正（PROJECT_PLAN.md 4.2）。"""

from collections.abc import Sequence

import pandas as pd

from screener.config import TrendConfig
from screener.models import Pivot, PivotKind, Trend


def basic_trend(pivots: Sequence[Pivot]) -> Trend:
    """步驟一：以最近兩個頭與最近兩個底判斷基本趨勢。"""
    peaks = [p.close for p in pivots if p.kind == "peak"]
    troughs = [p.close for p in pivots if p.kind == "trough"]
    if len(peaks) < 2 or len(troughs) < 2:
        return Trend.UNKNOWN
    if peaks[-1] > peaks[-2] and troughs[-1] > troughs[-2]:
        return Trend.BULL
    if peaks[-1] < peaks[-2] and troughs[-1] < troughs[-2]:
        return Trend.BEAR
    return Trend.RANGE


def last_pivot(pivots: Sequence[Pivot], kind: PivotKind) -> Pivot:
    """最近一個指定種類（頭或底）的轉折點。"""
    return next(p for p in reversed(pivots) if p.kind == kind)


def closes_since_last_pivot(pivots: Sequence[Pivot], closes: pd.Series) -> pd.Series:
    """最近一個已確認轉折點之後（不含）到今日的收盤價；轉折點就在今日時為空。closes 的索引為日期。"""
    return closes[closes.index > pivots[-1].date]


def classify_trend(pivots: Sequence[Pivot], closes: pd.Series, config: TrendConfig) -> Trend:
    """基本趨勢套用步驟二的破位修正。closes 含今日（盤中為現價），索引為日期。"""
    trend = basic_trend(pivots)
    if trend not in (Trend.BULL, Trend.BEAR):
        return trend
    since = closes_since_last_pivot(pivots, closes)
    if since.empty:                 # 最近一個轉折點就是今日，之後沒有收盤價可檢查
        return trend
    if trend is Trend.BULL and config.bull_break_to_range and since.min() < last_pivot(pivots, "trough").close:
        return Trend.RANGE
    if trend is Trend.BEAR and config.bear_break_to_range and since.max() > last_pivot(pivots, "peak").close:
        return Trend.RANGE
    return trend
