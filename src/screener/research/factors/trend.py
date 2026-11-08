"""趨勢標籤當作類別因子（RESEARCH_PLAN.md 4.4、R22）：多頭、盤整、空頭、資料不足。

類別沒有順序，所以只列各組報酬，不計算 IC。轉折點要逐日計算，所以用多行程；只算股票池內的日子。
"""

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from screener.analysis.indicators import sma
from screener.analysis.pivots import find_pivots
from screener.analysis.trend import classify_trend
from screener.config import TrendConfig
from screener.models import Trend
from screener.research.factors.base import FactorContext, register_factor

TREND_CODES = {Trend.BULL: 1, Trend.RANGE: 2, Trend.BEAR: 3, Trend.UNKNOWN: 4}
TREND_GROUPS = {code: trend.label for trend, code in TREND_CODES.items()}
CHUNK_SIZE = 16                     # 每個行程一次處理幾檔


@dataclass(frozen=True)
class _Task:
    code: str
    closes: pd.Series               # 該檔有成交的還原收盤
    positions: np.ndarray           # 要計算的 K 棒位置（股票池內的日子）
    config: TrendConfig


@register_factor("trend", category="現有規則", groups=TREND_GROUPS)
def trend(context: FactorContext) -> pd.DataFrame:
    panel = context.panel
    tasks = []
    for code in panel.codes:
        closes = panel.close[code].dropna()
        if closes.empty:
            continue
        eligible = panel.eligible[code].reindex(closes.index).to_numpy(dtype=bool)
        positions = np.flatnonzero(eligible)
        if len(positions):
            tasks.append(_Task(code, closes, positions, context.trend))
    workers = min(os.cpu_count() or 1, max(len(tasks), 1))
    if workers > 1 and len(tasks) > CHUNK_SIZE:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_labels, tasks, chunksize=CHUNK_SIZE))
    else:
        results = [_labels(task) for task in tasks]
    return pd.DataFrame(dict(results), index=panel.days, columns=panel.codes)


def _labels(task: _Task) -> tuple[str, pd.Series]:
    """每根 K 棒以當天當作「今日」判斷趨勢（同 PROJECT_PLAN.md 4.1、4.2），只用到當天（含）以前的收盤。"""
    closes = task.closes
    averages = sma(closes, task.config.pivot_ma)
    window = task.config.lookback_days
    labels = np.full(len(closes), np.nan)
    for i in task.positions:
        start = max(0, i + 1 - window)
        segment = closes.iloc[start:i + 1]
        pivots = find_pivots(segment, averages.iloc[start:i + 1])
        labels[i] = TREND_CODES[classify_trend(pivots, segment, task.config)]
    return task.code, pd.Series(labels, index=closes.index)
