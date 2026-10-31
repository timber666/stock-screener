"""轉折點（頭與底）：以收盤價觸碰均線確認（PROJECT_PLAN.md 4.1）。"""

import numpy as np
import pandas as pd

from screener.models import Pivot, PivotKind


def find_pivots(closes: pd.Series, ma: pd.Series) -> list[Pivot]:
    """在計算區間內找出已確認的轉折點，依日期由舊到新。

    closes 與 ma 是計算區間內的收盤價與均線，索引為日期、由舊到新。均線必須事先以完整歷史
    計算，因此歷史足夠時區間的第一根就有均線值。
    """
    if not closes.index.equals(ma.index):
        raise ValueError("收盤價與均線的索引不一致")
    values = closes.to_numpy(dtype=float)
    averages = ma.to_numpy(dtype=float)
    dates = closes.index

    with_ma = np.flatnonzero(~np.isnan(averages))
    if len(with_ma) == 0:
        return []
    first = with_ma[0]
    above = values[first] > averages[first]     # 第一根有均線的 K 棒只決定初始狀態
    segment_start = 0                           # 第一個轉折點的區間從計算區間的起點開始

    pivots: list[Pivot] = []
    for i in range(first + 1, len(values)):
        if above and values[i] <= averages[i]:
            kind: PivotKind = "peak"
        elif not above and values[i] >= averages[i]:
            kind = "trough"
        else:
            continue
        j = _last_extreme(values, segment_start, i, kind)
        pivots.append(Pivot(date=dates[j], close=float(values[j]), kind=kind))
        segment_start = j + 1                   # 下一個轉折點的區間從這個轉折點的隔天開始
        above = not above
    return pivots


def _last_extreme(values: np.ndarray, start: int, end: int, kind: PivotKind) -> int:
    """values[start..end]（含）中的最高（頭）或最低（底）收盤所在位置；有多根同為極值時取最後一根。"""
    segment = values[start:end + 1]
    target = segment.max() if kind == "peak" else segment.min()
    return start + int(np.flatnonzero(segment == target)[-1])
