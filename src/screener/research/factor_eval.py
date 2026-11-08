"""單因子的評估：Rank IC、分位數報酬、換手率與產業中性（RESEARCH_PLAN.md 4.4）。

全部以 numpy 矩陣計算（列為交易日、欄為股票）。因子值只保留當天可以列入研究的股票（Panel.eligible），
報酬只用因子值當天之後的價格，因子值只用當天（含）以前的資料，沒有前視。
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from screener.research.factors.panel import Panel

CLOSE = "close"                     # 收盤 t 進場
NEXT_OPEN = "next_open"             # 隔日開盤進場的對照（R23）


def forward_returns(panel: Panel, horizon: int, entry: str = CLOSE) -> np.ndarray:
    """持有 horizon 個市場交易日的報酬（%，還原權息），列為交易日、欄為股票（R24）。

    出場價：t+h 當天沒有成交時取 t+h 以前最後一個收盤；下市的股票以最後一個收盤結算。之後還不到 h 個交易日
    （資料庫最後幾天）為 NaN。進場價：收盤 t，或隔日開盤 t+1（當天沒有成交時為 NaN）。
    """
    exit_prices = panel.close.ffill().shift(-horizon).to_numpy(dtype=float)
    if entry == CLOSE:
        entries = panel.close.to_numpy(dtype=float)
    elif entry == NEXT_OPEN:
        entries = panel.open.shift(-1).to_numpy(dtype=float)
    else:
        raise ValueError(f"未知的進場時點：{entry}")
    return (exit_prices / entries - 1) * 100


def restrict(factor: pd.DataFrame, panel: Panel) -> np.ndarray:
    """因子值只保留可以列入研究的股票；無限大視為沒有值。"""
    values = factor.reindex(index=panel.days, columns=panel.codes).to_numpy(dtype=float, copy=True)
    values[~panel.eligible.to_numpy(dtype=bool)] = np.nan
    values[np.isinf(values)] = np.nan
    return values


def _ranks(values: np.ndarray) -> np.ndarray:
    """平均排名（同值取平均），1 為最小。"""
    return pd.Series(values).rank(method="average").to_numpy()


def _correlation(x: np.ndarray, y: np.ndarray) -> float:
    x, y = x - x.mean(), y - y.mean()
    denominator = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / denominator) if denominator > 0 else np.nan


def daily_ic(factor: np.ndarray, returns: np.ndarray, min_stocks: int) -> np.ndarray:
    """每天的 Rank IC：當天因子值與之後報酬的 Spearman 相關（先排名再算 Pearson）；股票數不足時為 NaN。"""
    ics = np.full(len(factor), np.nan)
    for day in range(len(factor)):
        f, r = factor[day], returns[day]
        ok = ~(np.isnan(f) | np.isnan(r))
        if ok.sum() >= min_stocks:
            ics[day] = _correlation(_ranks(f[ok]), _ranks(r[ok]))
    return ics


def quantile_labels(factor: np.ndarray, quantiles: int, min_stocks: int) -> np.ndarray:
    """每天依因子值分組：0 為沒有因子值，1～quantiles，quantiles 為因子值最大的一組。"""
    labels = np.zeros(factor.shape, dtype=np.int8)
    for day in range(len(factor)):
        ok = ~np.isnan(factor[day])
        n = int(ok.sum())
        if n >= min_stocks:
            labels[day][ok] = np.ceil(_ranks(factor[day][ok]) / n * quantiles).astype(np.int8).clip(1, quantiles)
    return labels


@dataclass(frozen=True)
class GroupReturns:
    """每天各組的等權平均報酬（%）。"""

    groups: np.ndarray              # 列為交易日、欄為組（第 0 欄為第 1 組）；該組沒有股票時為 NaN
    universe: np.ndarray            # 當天有分組的全部股票的平均


def group_returns(labels: np.ndarray, returns: np.ndarray, group_ids: Sequence[int]) -> GroupReturns:
    groups = np.full((len(labels), len(group_ids)), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        for column, group in enumerate(group_ids):
            selected = np.where((labels == group) & ~np.isnan(returns), returns, np.nan)
            counts = (~np.isnan(selected)).sum(axis=1)
            groups[:, column] = np.where(counts > 0, np.nansum(selected, axis=1) / counts, np.nan)
        all_stocks = np.where((labels > 0) & ~np.isnan(returns), returns, np.nan)
        counts = (~np.isnan(all_stocks)).sum(axis=1)
        universe = np.where(counts > 0, np.nansum(all_stocks, axis=1) / counts, np.nan)
    return GroupReturns(groups, universe)


def top_turnover(labels: np.ndarray, top: int, horizon: int, *, start: int = 0) -> np.ndarray:
    """最高組的換手率：從第 start 列起每 horizon 列重新分組一次，新名單中上次不在最高組的比例。

    回傳與 labels 同列數的陣列，只有重新分組的日子有值（第一次重新分組沒有比較對象，為 NaN）。
    """
    turnover = np.full(len(labels), np.nan)
    previous = None
    for day in range(start, len(labels), horizon):
        current = labels[day] == top
        if not current.any():
            continue                            # 當天股票數不足（沒有分組），保留上一次的名單
        if previous is not None:
            turnover[day] = 1 - (current & previous).sum() / current.sum()
        previous = current
    return turnover


def neutralize(factor: np.ndarray, industries: np.ndarray, min_group: int) -> np.ndarray:
    """產業中性：每天把因子值減去同產業的平均。當天同產業少於 min_group 檔的併到「其他」（代號 −1，含未分類），一起減去平均。"""
    result = factor.copy()
    for day in range(len(factor)):
        ok = ~np.isnan(factor[day])
        if not ok.any():
            continue
        groups = industries[day][ok] + 1        # −1（未分類）→ 0
        counts = np.bincount(groups)
        groups = np.where(counts[groups] < min_group, 0, groups)
        sums = np.bincount(groups, weights=factor[day][ok])
        members = np.bincount(groups)
        result[day][ok] = factor[day][ok] - sums[groups] / members[groups]
    return result


def non_overlapping_t_stat(ic: np.ndarray, horizon: int) -> tuple[float, int]:
    """每 horizon 天取一次 IC 算 t 值（重疊的報酬會高估顯著性）。起點不同會取到不同的樣本，所以 horizon 個起點各算一次取平均，
    回傳 (t 值, 每個起點的樣本天數)。ic 已是單一段內的序列。"""
    available = ic[~np.isnan(ic)]
    values = []
    for offset in range(horizon):
        sample = available[offset::horizon]
        if len(sample) >= 2 and sample.std(ddof=1) > 0:
            values.append(float(sample.mean() / (sample.std(ddof=1) / np.sqrt(len(sample)))))
    return (float(np.mean(values)) if values else np.nan), len(available[::horizon])
