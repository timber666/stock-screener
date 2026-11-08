"""因子研究的面板資料：日期 × 代號的寬表（RESEARCH_PLAN.md 4.4）。

列為市場的交易日（market.db 的每一天）、欄為曾經入選股票池的股票，一次算完整個面板，不逐檔逐日迴圈。
價格一律用還原權息後的價格（R18）；每日規則仍用未還原價（D8）。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

from screener.analysis.market import limit_prices
from screener.analysis.numeric import normalize
from screener.backtest.signals import StockHistory

ADJUSTED_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class Panel:
    open: pd.DataFrame              # 還原權息；沒有成交的日子為 NaN
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    raw_close: pd.DataFrame         # 未還原收盤：只用來算市值（M9b）
    volume: pd.DataFrame            # 成交量（張），不調整；沒有成交的日子為 NaN
    value: pd.DataFrame             # 成交金額（元）：第一次出現在行情之後，沒有成交或不在行情表的日子為 0，之前為 NaN
    eligible: pd.DataFrame          # 當天可以列入研究：當月股票池內、掛牌第六個交易日起（D38）、當天有成交
    limit_up: pd.DataFrame          # 當天收在漲停價
    limit_down: pd.DataFrame        # 當天收在跌停價

    @property
    def days(self) -> pd.Index:
        return self.close.index

    @property
    def codes(self) -> pd.Index:
        return self.close.columns


def build_panel(histories: Sequence[StockHistory], values: pd.DataFrame) -> Panel:
    """histories：曾經入選股票池的股票（market.history）；values：成交金額，索引為全部交易日、欄位為代號。"""
    days = values.index
    frames: dict[str, dict[str, pd.Series]] = {column: {} for column in ADJUSTED_COLUMNS}
    raw_closes, volumes, eligibles, ups, downs = {}, {}, {}, {}, {}
    for history in histories:
        code = history.info.code
        for column in ADJUSTED_COLUMNS:
            frames[column][code] = history.adjusted[column]
        raw_closes[code] = history.bars["close"]
        volumes[code] = history.bars["volume"].astype(float)
        eligible = history.eligible()
        eligibles[code] = pd.Series(eligible, index=history.bars.index, dtype=bool)
        up, down = _limit_flags(history.bars["close"].to_numpy(), history.factors.to_numpy(), eligible)
        ups[code] = pd.Series(up, index=history.bars.index, dtype=bool)
        downs[code] = pd.Series(down, index=history.bars.index, dtype=bool)
    codes = sorted(frames["close"])

    def wide(series: dict[str, pd.Series]) -> pd.DataFrame:
        return pd.DataFrame(series, index=days, columns=codes)

    def flag(series: dict[str, pd.Series]) -> pd.DataFrame:
        return pd.DataFrame(series, index=days, columns=codes).fillna(False).astype(bool)

    subset = values.reindex(columns=codes)
    first_seen = subset.notna().cummax()
    return Panel(open=wide(frames["open"]), high=wide(frames["high"]), low=wide(frames["low"]),
                 close=wide(frames["close"]), raw_close=wide(raw_closes), volume=wide(volumes),
                 value=subset.fillna(0.0).where(first_seen), eligible=flag(eligibles), limit_up=flag(ups),
                 limit_down=flag(downs))


@lru_cache(maxsize=None)
def _limits(reference: float) -> tuple[float, float]:
    return limit_prices(reference)


def _limit_flags(closes: np.ndarray, factors: np.ndarray, eligible: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """收在漲跌停價：以「等於」判斷（PROJECT_PLAN.md 14.1）；參考價 = 前一日收盤 × 當天的調整因子。"""
    up, down = np.zeros(len(closes), dtype=bool), np.zeros(len(closes), dtype=bool)
    for i in np.flatnonzero(eligible):
        if i == 0:
            continue
        limit_up, limit_down = _limits(float(closes[i - 1] * factors[i]))
        close = normalize(closes[i])
        up[i] = close == normalize(limit_up)
        down[i] = close == normalize(limit_down)
    return up, down


def per_stock(frame: pd.DataFrame, function: Callable[[pd.Series], pd.Series]) -> pd.DataFrame:
    """對每檔股票有成交的 K 棒計算，再對回日期與代號：「最近 n 根」以該檔自己的 K 棒為準，沒有成交的日子不算。"""
    results = {}
    for code in frame.columns:
        series = frame[code].dropna()
        results[code] = function(series) if len(series) else series
    return pd.DataFrame(results, index=frame.index, columns=frame.columns)
