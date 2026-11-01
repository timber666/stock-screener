"""訊號回測的統計（PROJECT_PLAN.md 14.1）。"""

from collections.abc import Sequence

import numpy as np
import pandas as pd

OBSERVE, VALIDATE, ALL = "觀察段", "驗證段", "全期間"
INCLUDE, EXCLUDE = "含", "排除"


def summarize(signals: pd.DataFrame, horizons: Sequence[int], cost_pct: float,
              rule_order: Sequence[str]) -> pd.DataFrame:
    """所有分組的統計，每列一個組合：規則 × 段 × 樣本（全部／去重）× 漲跌停鎖死（含／排除）
    × 跳空事件（含／排除）× 持有天數。報酬單位為 %，勝率為方向報酬 > 0 的比例。"""
    rows = []
    for sample, by_sample in (("全部", signals), ("去重", signals[signals["kept"]])):
        for limit, by_limit in ((INCLUDE, by_sample), (EXCLUDE, by_sample[~by_sample["limit_locked"]])):
            for gap, by_gap in ((INCLUDE, by_limit), (EXCLUDE, by_limit[~by_limit["gap_event"]])):
                for segment, subset in _segments(by_gap):
                    for rule_id in rule_order:
                        group = subset[subset["rule_id"] == rule_id]
                        for n in horizons:
                            rows.append({"rule_id": rule_id, "segment": segment, "sample": sample,
                                         "limit_locked": limit, "gap_event": gap, "horizon": n,
                                         **_stats(group, n, cost_pct)})
    return pd.DataFrame(rows)


def select(summary: pd.DataFrame, *, sample: str = "去重", limit_locked: str = EXCLUDE,
           gap_event: str = INCLUDE, **filters: object) -> pd.DataFrame:
    """取出某一種分組；預設為終端摘要使用的「去重、排除漲跌停鎖死、含跳空事件」。"""
    mask = ((summary["sample"] == sample) & (summary["limit_locked"] == limit_locked)
            & (summary["gap_event"] == gap_event))
    for column, value in filters.items():
        mask &= summary[column] == value
    return summary[mask]


def _segments(signals: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [(ALL, signals), (OBSERVE, signals[signals["segment"] == OBSERVE]),
            (VALIDATE, signals[signals["segment"] == VALIDATE])]


def _stats(group: pd.DataFrame, n: int, cost_pct: float) -> dict[str, float]:
    returns = group[f"ret_{n}"].dropna()
    if returns.empty:
        return {"count": 0, "win_rate": np.nan, "mean": np.nan, "median": np.nan, "excess": np.nan,
                "net": np.nan}
    return {
        "count": len(returns),
        "win_rate": float((returns > 0).mean() * 100),
        "mean": float(returns.mean()),
        "median": float(returns.median()),
        "excess": float(group[f"excess_{n}"].dropna().mean()),
        "net": float(returns.mean() - cost_pct),
    }
