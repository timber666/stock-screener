"""由日 K 建立 StockContext：均線、轉折點、趨勢（PROJECT_PLAN.md 5.1、第 6 節）。"""

from collections.abc import Sequence

import pandas as pd

from screener.analysis.indicators import sma
from screener.analysis.pivots import find_pivots
from screener.analysis.trend import classify_trend
from screener.config import TrendConfig
from screener.models import DailyBar, StockContext, StockInfo

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


def bars_to_frame(bars: Sequence[DailyBar]) -> pd.DataFrame:
    """把日 K 轉成以日期為索引的 DataFrame，欄位為 BAR_COLUMNS。"""
    return pd.DataFrame(
        {column: [getattr(bar, column) for bar in bars] for column in BAR_COLUMNS},
        index=pd.Index([bar.date for bar in bars], name="date"),
    )


def pivot_window(bars: pd.DataFrame, config: TrendConfig) -> pd.Index:
    """轉折點的計算區間：截至今日（含）的最近 lookback_days 根 K 棒的日期。"""
    return bars.index[-config.lookback_days:]


def build_context(
    info: StockInfo,
    bars: pd.DataFrame,
    config: TrendConfig,
    *,
    is_tentative: bool,
) -> StockContext:
    """bars 的最後一列為今日。轉折點與趨勢都納入今日（盤中為暫定 K 棒，等於假設現價就是收盤價）。"""
    if bars.empty:
        raise ValueError(f"{info.code} 沒有任何 K 棒")
    if not (bars.index.is_unique and bars.index.is_monotonic_increasing):
        raise ValueError(f"{info.code} 的 K 棒日期必須由舊到新且不重複")

    closes = bars["close"]
    ma = sma(closes, config.pivot_ma)               # 以完整歷史計算，不限於計算區間
    window = pivot_window(bars, config)
    pivots = find_pivots(closes.loc[window], ma.loc[window])
    return StockContext(
        info=info,
        bars=bars,
        is_tentative=is_tentative,
        pivots=pivots,
        trend=classify_trend(pivots, closes, config),
    )
