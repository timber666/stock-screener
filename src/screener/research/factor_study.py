"""把因子跑過評估，整理成報告用的表格（RESEARCH_PLAN.md 4.4）。

連續因子：Rank IC、分位數報酬、換手率、扣成本後，產業中性前後並列；隔日開盤進場只列 IC 當對照（R23）。
類別因子（趨勢、獲利／虧損）：沒有順序，只列各組報酬。觀察段與驗證段分列（D32）。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from screener.config import FactorsConfig
from screener.research.factor_eval import (CLOSE, NEXT_OPEN, GroupReturns, daily_ic, forward_returns, group_returns,
                                           neutralize, non_overlapping_t_stat, quantile_labels, restrict, top_turnover)
from screener.research.factors.base import Factor, FactorContext
from screener.research.factors.panel import Panel

logger = logging.getLogger(__name__)

OBSERVE = "觀察段"
VALIDATE = "驗證段"
SEGMENTS = (OBSERVE, VALIDATE)
RAW = "未中性"
INDUSTRY = "產業中性"
NEUTRALS = (RAW, INDUSTRY)
INCLUDE = "全部"
EXCLUDE = "排除收漲跌停"
LIMITS = (INCLUDE, EXCLUDE)


@dataclass
class StudyResult:
    summary: pd.DataFrame           # 因子 × 持有天數 × 段 × 中性前後 × 進場 × 漲跌停
    ic: pd.DataFrame                # 每天的 IC：欄位為 (因子, 中性前後, 持有天數)，收盤進場
    decay: pd.DataFrame             # 因子 × 段 × 持有天數的平均 IC（未中性）
    groups: pd.DataFrame            # 類別因子各組的報酬
    start: date
    end: date
    round_trip_pct: float
    factors: list[Factor] = field(default_factory=list)


@dataclass(frozen=True)
class _Inputs:
    panel: Panel
    config: FactorsConfig
    industries: np.ndarray          # 每天每檔的產業別，−1 為未分類
    locked: np.ndarray              # 當天收在漲停或跌停
    returns: dict[tuple[str, int], np.ndarray]
    masks: dict[str, np.ndarray]    # 段 → 每天是否屬於該段
    first_position: int             # 研究期間的第一個交易日
    round_trip_pct: float


def run_study(context: FactorContext, factors: Sequence[Factor], config: FactorsConfig, industries: np.ndarray, *,
              start: date, validation_start: date, round_trip_pct: float) -> StudyResult:
    panel = context.panel
    inputs = _prepare(panel, config, industries, start, validation_start, round_trip_pct)
    rows, ic_columns, decay_rows, group_rows = [], {}, [], []
    for n, factor in enumerate(factors, start=1):
        logger.info("因子 [%d/%d] %s", n, len(factors), factor.id)
        values = restrict(factor.compute(context), panel)
        if factor.is_categorical:
            group_rows += _categorical(factor, values, inputs)
        else:
            rows += _continuous(factor, values, inputs, ic_columns, decay_rows)
    days = panel.days
    in_study = inputs.masks[OBSERVE] | inputs.masks[VALIDATE]
    ic_table = pd.DataFrame(ic_columns, index=days)[in_study] if ic_columns else pd.DataFrame(index=days[in_study])
    return StudyResult(summary=pd.DataFrame(rows), ic=ic_table, decay=pd.DataFrame(decay_rows),
                       groups=pd.DataFrame(group_rows), start=days[in_study][0], end=days[in_study][-1],
                       round_trip_pct=round_trip_pct, factors=list(factors))


def _prepare(panel: Panel, config: FactorsConfig, industries: np.ndarray, start: date, validation_start: date,
             round_trip_pct: float) -> _Inputs:
    returns = {(CLOSE, h): forward_returns(panel, h, CLOSE) for h in sorted({*config.horizons, *config.decay_horizons})}
    returns |= {(NEXT_OPEN, h): forward_returns(panel, h, NEXT_OPEN) for h in config.horizons}
    days = np.array(panel.days, dtype=object)
    in_study = np.array([day >= start for day in days])
    validate = np.array([day >= validation_start for day in days])
    return _Inputs(panel=panel, config=config, industries=industries,
                   locked=(panel.limit_up.to_numpy(dtype=bool) | panel.limit_down.to_numpy(dtype=bool)),
                   returns=returns, masks={OBSERVE: in_study & ~validate, VALIDATE: in_study & validate},
                   first_position=int(np.argmax(in_study)), round_trip_pct=round_trip_pct)


def _continuous(factor: Factor, values: np.ndarray, inputs: _Inputs, ic_columns: dict, decay_rows: list[dict]) -> list[dict]:
    config = inputs.config
    rows: list[dict] = []
    stocks = (~np.isnan(values)).sum(axis=1)
    neutral_values = neutralize(values, inputs.industries, config.min_industry_stocks)
    for neutral, data in ((RAW, values), (INDUSTRY, neutral_values)):
        ics = {h: daily_ic(data, inputs.returns[(CLOSE, h)], config.min_stocks)
               for h in sorted({*config.horizons, *config.decay_horizons})}
        if neutral == RAW:
            for segment in SEGMENTS:
                decay_rows += [{"factor": factor.id, "category": factor.category, "segment": segment, "horizon": h,
                                "ic_mean": float(np.nanmean(ics[h][inputs.masks[segment]]))
                                if not np.isnan(ics[h][inputs.masks[segment]]).all() else np.nan}
                               for h in config.decay_horizons]
        labels = {limit: quantile_labels(np.where(inputs.locked, np.nan, data) if limit == EXCLUDE else data,
                                         config.quantiles, config.min_stocks) for limit in LIMITS}
        for h in config.horizons:
            ic_columns[(factor.id, neutral, h)] = ics[h]
            groups = {limit: group_returns(labels[limit], inputs.returns[(CLOSE, h)],
                                           list(range(1, config.quantiles + 1))) for limit in LIMITS}
            turnovers = {limit: top_turnover(labels[limit], config.quantiles, h, start=inputs.first_position)
                         for limit in LIMITS}
            next_open = daily_ic(data, inputs.returns[(NEXT_OPEN, h)], config.min_stocks)
            for segment in SEGMENTS:
                base = {"factor": factor.id, "category": factor.category, "horizon": h, "segment": segment,
                        "neutral": neutral}
                mask = inputs.masks[segment]
                ic_stats = _ic_stats(ics[h], mask, h)
                for limit in LIMITS:
                    rows.append({**base, "entry": CLOSE, "limit": limit, "stocks": _mean(stocks[mask]),
                                 **(ic_stats if limit == INCLUDE else _empty_ic()),
                                 **_group_stats(groups[limit], turnovers[limit], mask, config.quantiles,
                                                inputs.round_trip_pct)})
                rows.append({**base, "entry": NEXT_OPEN, "limit": INCLUDE, "stocks": _mean(stocks[mask]),
                             **_ic_stats(next_open, mask, h)})
    return rows


def _categorical(factor: Factor, values: np.ndarray, inputs: _Inputs) -> list[dict]:
    config = inputs.config
    assert factor.groups is not None
    group_ids = list(factor.groups)
    labels = np.nan_to_num(values, nan=0.0).astype(np.int8)
    rows = []
    for limit in LIMITS:
        data = np.where(inputs.locked, 0, labels) if limit == EXCLUDE else labels
        for h in config.horizons:
            result = group_returns(data, inputs.returns[(CLOSE, h)], group_ids)
            for segment in SEGMENTS:
                mask = inputs.masks[segment]
                for column, group in enumerate(group_ids):
                    series = result.groups[mask, column]
                    rows.append({"factor": factor.id, "category": factor.category, "group": factor.groups[group],
                                 "horizon": h, "segment": segment, "limit": limit,
                                 "days": int((~np.isnan(series)).sum()),
                                 "stocks": _mean((data[mask] == group).sum(axis=1)),
                                 "mean": _mean(series), "excess": _mean(series - result.universe[mask])})
    return rows


def _mean(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    return float(np.nanmean(values)) if (~np.isnan(values)).any() else np.nan


def _empty_ic() -> dict:
    return dict.fromkeys(("days", "ic_mean", "ic_std", "icir", "ic_t", "ic_t_days", "ic_positive"), np.nan)


def _ic_stats(ic: np.ndarray, mask: np.ndarray, horizon: int) -> dict:
    series = ic[mask]
    available = series[~np.isnan(series)]
    if len(available) < 2:
        return {**_empty_ic(), "days": int(len(available))}
    deviation = float(available.std(ddof=1))
    t_value, samples = non_overlapping_t_stat(series, horizon)
    return {"days": int(len(available)), "ic_mean": float(available.mean()), "ic_std": deviation,
            "icir": float(available.mean() / deviation) if deviation > 0 else np.nan, "ic_t": t_value,
            "ic_t_days": samples, "ic_positive": float((available > 0).mean())}


def _group_stats(group: GroupReturns, turnover: np.ndarray, mask: np.ndarray, quantiles: int,
                 round_trip_pct: float) -> dict:
    stats = {f"q{i + 1}": _mean(group.groups[mask, i]) for i in range(quantiles)}
    top, bottom = group.groups[mask, quantiles - 1], group.groups[mask, 0]
    excess = _mean(top - group.universe[mask])
    rate = _mean(turnover[mask])
    stats.update(top_bottom=_mean(top - bottom), top_excess=excess, turnover=rate,
                 top_net=excess - rate * round_trip_pct if not np.isnan(rate) else np.nan)
    return stats
