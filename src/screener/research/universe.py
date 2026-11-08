"""全市場的動態股票池（RESEARCH_PLAN.md 4.3、R7）。

每月第一個交易日，依之前 lookback_days 個交易日（不含當天）的平均成交金額排名，取前 size 檔當作當月的
股票池。平均 = 成交金額合計 ÷ lookback_days，沒有成交或不在行情表的日子算 0：剛掛牌或停止交易的股票
不會因為只有幾天的資料就排在前面。只有前一個交易日還在行情表中的股票（含當天沒有成交的）可以入選，
已下市的不會入選。排名只用到當月之前的資料，沒有前視。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class Universe:
    months: dict[date, frozenset[str]]      # 該月第一個交易日 → 當月股票池；資料不足 lookback_days 的月份沒有

    @property
    def codes(self) -> frozenset[str]:
        """曾經入選的全部股票。"""
        return frozenset().union(*self.months.values())

    @property
    def first_day(self) -> date | None:
        return min(self.months, default=None)

    def members(self, day: date) -> frozenset[str]:
        """day 所在月份的股票池；沒有股票池的月份為空集合。"""
        for first, members in self.months.items():
            if (first.year, first.month) == (day.year, day.month):
                return members
        return frozenset()

    def mask(self, code: str, days: Sequence[date]) -> pd.Series:
        """每一天是否在當月股票池內，索引為 days。"""
        by_month = {(first.year, first.month): code in members for first, members in self.months.items()}
        return pd.Series([by_month.get((d.year, d.month), False) for d in days], index=pd.Index(days, name="date"),
                         dtype=bool)


def build_universe(values: pd.DataFrame, *, size: int, lookback_days: int) -> Universe:
    """values：索引為交易日（由舊到新）、欄位為代號的成交金額；不在行情表的日子為 NaN。"""
    calendar = list(values.index)
    filled = values.fillna(0.0)
    listed = values.notna()
    months: dict[date, frozenset[str]] = {}
    for position, day in enumerate(calendar):
        if position > 0 and (calendar[position - 1].year, calendar[position - 1].month) == (day.year, day.month):
            continue                            # 不是該月第一個交易日
        if position < lookback_days:
            continue                            # 之前的交易日不足，不建立股票池
        average = filled.iloc[position - lookback_days:position].sum() / lookback_days
        candidates = average[listed.iloc[position - 1]]
        ranked = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))     # 同分依代號
        months[day] = frozenset(code for code, _ in ranked[:size])
    return Universe(months)
