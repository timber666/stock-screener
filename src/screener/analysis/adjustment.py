"""還原權息：後還原，最新一天的價格不變，把歷史價格往下折算（PROJECT_PLAN.md 14.2）。

計算方式沿用 Taiwan-Quant-Research 專案的 ingestion/adjustment.py（D33）。該專案以真實事件對照官方參考價
驗證過，並修正了下列實際踩過的問題，這裡一併保留：

- 事件當天不折算：累積因子只乘「之後」的事件，否則事件當天的價格會被多折一次（6669 的 2610 被算成 875）。
- 同一天的多筆除權息先合併：證交所公式同時代入現金與股票股利，分開套用會用錯前一日收盤（算出 95.0，應為 97.5）。
- 休市日的事件順延：原定除權息日遇颱風休市時，跳空發生在下一個交易日（2884 玉山金 2024-07-25）。
- 不四捨五入：還原價是算報酬的中間值，低價股四捨五入的誤差會直接變成報酬誤差。

本專案另外以官方公告的除權息前後價格為優先（D37）：股利資料只記錄股利金額，現金增資除權只有認購
比率與價格，以金額代入公式會漏掉（例：3321 同泰 2025-12-05 由 23.0 除權為 18.97）。
"""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Sequence
from datetime import date

import pandas as pd

from screener.models import CorporateAction, DividendEvent, DividendResult

PRICE_COLUMNS = ("open", "high", "low", "close")
STOCK_DIVIDEND_PAR = 10.0           # 股票股利以面額 10 元計：每股配 1 元 = 每 10 股配 1 股


def reference_price(prev_close: float, cash_dividend: float, stock_dividend: float) -> float:
    """證交所的除權息參考價：(前一日收盤 − 現金股利) / (1 + 股票股利 / 10)。"""
    return (prev_close - cash_dividend) / (1 + stock_dividend / STOCK_DIVIDEND_PAR)


def event_factors(
    bars: pd.DataFrame,
    dividends: Sequence[DividendEvent],
    actions: Sequence[CorporateAction],
    results: Sequence[DividendResult] = (),
) -> pd.Series:
    """每根 K 棒當天的調整因子（沒有事件為 1），索引同 bars（日期由舊到新）。

    除權息：有官方公告的前後價格（results）時，因子 = 參考價 / 除權息前收盤；沒有時以股利金額代入公式，
    因子 = 公式參考價 / 前一日收盤，同一天的多筆股利先把金額加總。減資、面額變更直接取官方公告的
    after_price / before_price；同一天的除權息與公司行動，兩個因子相乘。
    事件日不是交易日時順延到之後第一根 K 棒；晚於最後一根 K 棒的事件略過。第一根 K 棒當天的
    除權息沒有前一日收盤可算，也不影響任何報酬，視為 1。
    """
    days = list(bars.index)
    closes = bars["close"].to_numpy()
    official: dict[int, float] = {}
    for result in results:
        if (i := trading_day_index(days, result.date)) is not None:
            official[i] = result.after_price / result.before_price
    cash: dict[int, float] = defaultdict(float)
    stock: dict[int, float] = defaultdict(float)
    for event in dividends:
        if (i := trading_day_index(days, event.date)) is not None and i not in official:
            cash[i] += event.cash_dividend
            stock[i] += event.stock_dividend

    factors = [1.0] * len(days)
    for i, factor in official.items():
        if i > 0:
            factors[i] *= factor
    for i in cash.keys() | stock.keys():
        if i > 0:
            factors[i] *= reference_price(closes[i - 1], cash[i], stock[i]) / closes[i - 1]
    for action in actions:
        if (i := trading_day_index(days, action.date)) is not None:
            factors[i] *= action.after_price / action.before_price
    return pd.Series(factors, index=bars.index, dtype=float)


def cumulative_factors(factors: pd.Series) -> pd.Series:
    """每根 K 棒的累積因子 = 之後（不含當天）所有事件因子的乘積；最後一根恆為 1。"""
    later = factors.shift(-1, fill_value=1.0)
    return later[::-1].cumprod()[::-1]


def adjust_bars(
    bars: pd.DataFrame,
    dividends: Sequence[DividendEvent],
    actions: Sequence[CorporateAction],
    results: Sequence[DividendResult] = (),
) -> pd.DataFrame:
    """還原後的 K 棒：開高低收乘上累積因子，成交量不調整；不四捨五入。"""
    cumulative = cumulative_factors(event_factors(bars, dividends, actions, results))
    adjusted = bars.copy()
    for column in PRICE_COLUMNS:
        adjusted[column] = bars[column] * cumulative
    return adjusted


def trading_day_index(days: Sequence[date], day: date) -> int | None:
    """day 當天或之後第一根 K 棒在 days（由舊到新）中的位置；晚於最後一根時為 None。休市日的事件因此順延。"""
    i = bisect_left(days, day)
    return i if i < len(days) else None
