"""基本面因子：月營收、價值、規模與週轉率（RESEARCH_PLAN.md 4.5）。

前視偏差（第 2 節原則 4）：M 月的營收在 M+1 月 revenue_available_day 之後的第一個交易日才能用（R26）。
本益比、淨值比、殖利率與發行股數是每月第一個交易日的取樣值，當天收盤後就能用，中間的日子以還原價的變動更新：
取樣日的 1/PE 乘上「取樣日的還原收盤 / 當天的還原收盤」（R25）。
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from screener.config import MarketConfig
from screener.jobs import JobFailed
from screener.research.factors.base import FactorContext, register_factor
from screener.research.factors.panel import Panel, per_stock
from screener.research.market.models import SHARES_PER_LOT


SAME_INDUSTRY = {"金融業": "金融保險業"}     # 上櫃的月營收頁寫「金融業」，上市寫「金融保險業」，是同一類


def revenue_available_on(month: date, days: pd.Index, market: MarketConfig) -> date | None:
    """該月營收第一個可以使用的交易日；資料庫的交易日還沒到時為 None。"""
    following = date(month.year + month.month // 12, month.month % 12 + 1, market.revenue_available_day)
    position = days.searchsorted(following + timedelta(days=1))
    return days[position] if position < len(days) else None


def month_table(revenue: pd.DataFrame, values: pd.Series, panel: Panel, market: MarketConfig) -> pd.DataFrame:
    """每月一個值的資料（values 的索引同 revenue）→ 日期 × 代號，每天取當時可用的最新一個月。"""
    if revenue.empty:
        raise JobFailed("market.db 沒有月營收，請先執行 market-update")
    months = sorted(revenue["month"].unique())
    available = {month: revenue_available_on(date.fromisoformat(month), panel.days, market) for month in months}
    frame = pd.DataFrame({"code": revenue["code"].to_numpy(), "day": revenue["month"].map(available).to_numpy(),
                          "value": values.to_numpy()}).dropna(subset=["day"])
    table = frame.pivot_table(index="day", columns="code", values="value", aggfunc="last")
    return table.reindex(panel.days).reindex(columns=panel.codes).ffill()


def industry_codes(revenue: pd.DataFrame, panel: Panel, market: MarketConfig) -> tuple[np.ndarray, list[str]]:
    """每天每檔的產業別：(整數矩陣，−1 為未分類；產業名稱)。當時可用的最新一份月營收頁上的分類，沒有後見之明（R20）。"""
    industries = revenue["industry"].replace(SAME_INDUSTRY)
    names = sorted(industries.unique())
    code_of = {name: i for i, name in enumerate(names)}
    table = month_table(revenue, industries.map(code_of).astype(float), panel, market)
    return table.fillna(-1).to_numpy(dtype=np.int32), names


def _revenue_ratio(context: FactorContext, numerator: str, denominator: str) -> pd.DataFrame:
    revenue = context.revenue
    if revenue.empty:
        raise JobFailed("market.db 沒有月營收，請先執行 market-update")
    base = revenue[denominator].where(revenue[denominator] > 0)
    return month_table(revenue, revenue[numerator] / base - 1, context.panel, context.market)


@register_factor("revenue_yoy", category="月營收")
def revenue_yoy(context: FactorContext) -> pd.DataFrame:
    return _revenue_ratio(context, "revenue", "last_year")


@register_factor("revenue_mom", category="月營收")
def revenue_mom(context: FactorContext) -> pd.DataFrame:
    return _revenue_ratio(context, "revenue", "previous")


@register_factor("revenue_cum_yoy", category="月營收")
def revenue_cum_yoy(context: FactorContext) -> pd.DataFrame:
    return _revenue_ratio(context, "cumulative", "cumulative_last_year")


@register_factor("revenue_acceleration", category="月營收")
def revenue_acceleration(context: FactorContext, *, months: int) -> pd.DataFrame:
    """本月年增率 − months 個月前的年增率；中間缺月時算不出來。"""
    revenue = context.revenue
    if revenue.empty:
        raise JobFailed("market.db 沒有月營收，請先執行 market-update")
    yoy = (revenue["revenue"] / revenue["last_year"].where(revenue["last_year"] > 0) - 1).rename("yoy")
    frame = pd.concat([revenue[["code", "month"]], yoy], axis=1)
    frame["position"] = frame["month"].str[:4].astype(int) * 12 + frame["month"].str[5:7].astype(int)
    earlier = frame.assign(position=frame["position"] + months)[["code", "position", "yoy"]]
    merged = frame.merge(earlier, how="left", on=["code", "position"], suffixes=("", "_earlier"), validate="one_to_one")
    return month_table(revenue, merged["yoy"] - merged["yoy_earlier"], context.panel, context.market)


def _sampled_table(table: pd.DataFrame, column: str, panel: Panel) -> pd.DataFrame:
    """每月取樣的欄位 → 日期 × 代號，只有取樣日有值；不是正數的值（虧損沒有本益比、沒有配息）視為沒有值。"""
    if table.empty:
        raise JobFailed("market.db 沒有本益比等資料，請先執行 market-update")
    values = pd.DataFrame({"code": table["code"], "day": [date.fromisoformat(d) for d in table["date"]],
                           "value": table[column].where(table[column] > 0)}).dropna(subset=["value"])
    sampled = values.pivot_table(index="day", columns="code", values="value", aggfunc="last")
    return sampled.reindex(panel.days).reindex(columns=panel.codes)


def carry_forward(sampled: pd.DataFrame, price: pd.DataFrame, *, inverse: bool) -> pd.DataFrame:
    """取樣值以還原價的變動更新到每一天：inverse 時為「值 × 取樣日價格 / 當天價格」（殖利率類），否則為「值 × 當天價格 / 取樣日價格」（市值）。

    取樣日沒有成交的樣本不用（沒有那天的價格可以對應），沿用上一次的樣本。當天沒有成交時為 NaN。
    """
    usable = sampled.where(price.notna())
    value = usable.ffill()
    base = price.where(usable.notna()).ffill()
    return value * base / price if inverse else value * price / base


@register_factor("earnings_yield", category="價值")
def earnings_yield(context: FactorContext) -> pd.DataFrame:
    """E/P = 1 / 本益比；虧損（本益比沒有值）時為 NaN，另列於 profitable。"""
    panel = context.panel
    return 1 / carry_forward(_sampled_table(context.valuations, "pe", panel), panel.close, inverse=False)


@register_factor("profitable", category="價值", groups={1: "獲利", 2: "虧損"})
def profitable(context: FactorContext) -> pd.DataFrame:
    """每月取樣日有收盤的股票：本益比有值為獲利，沒有值為虧損（類別因子）。"""
    table = context.valuations
    if table.empty:
        raise JobFailed("market.db 沒有本益比等資料，請先執行 market-update")
    panel = context.panel
    label = np.where(table["pe"].notna() & (table["pe"] > 0), 1.0, 2.0)
    values = pd.DataFrame({"code": table["code"], "day": [date.fromisoformat(d) for d in table["date"]], "value": label})
    sampled = values.pivot_table(index="day", columns="code", values="value", aggfunc="last")
    return sampled.reindex(panel.days).reindex(columns=panel.codes).ffill()


@register_factor("book_yield", category="價值")
def book_yield(context: FactorContext) -> pd.DataFrame:
    """B/P = 1 / 股價淨值比。"""
    panel = context.panel
    return 1 / carry_forward(_sampled_table(context.valuations, "pb", panel), panel.close, inverse=False)


@register_factor("dividend_yield", category="價值")
def dividend_yield(context: FactorContext) -> pd.DataFrame:
    panel = context.panel
    return carry_forward(_sampled_table(context.valuations, "dividend_yield", panel), panel.close, inverse=True)


@register_factor("size", category="規模")
def size(context: FactorContext) -> pd.DataFrame:
    """log(市值)：取樣日的發行股數 × 未還原收盤，以還原價的變動更新到每一天。"""
    panel = context.panel
    capitalization = _sampled_table(context.shares, "shares", panel) * panel.raw_close
    updated = carry_forward(capitalization, panel.close, inverse=False)
    return np.log(updated.where(updated > 0))


@register_factor("share_turnover", category="流動性")
def share_turnover(context: FactorContext, *, days: int) -> pd.DataFrame:
    """平均週轉率：每天成交股數 ÷ 當時最新一次取樣的發行股數，取 days 根 K 棒的平均。"""
    panel = context.panel
    shares = _sampled_table(context.shares, "shares", panel).ffill()
    return per_stock(panel.volume * SHARES_PER_LOT / shares, lambda s: s.rolling(days).mean())
