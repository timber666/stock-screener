"""核心資料型別（PROJECT_PLAN.md 10.1）。"""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

import pandas as pd

from screener.analysis.indicators import sma


class Market(str, Enum):
    TSE = "tse"    # 上市
    OTC = "otc"    # 上櫃


class Trend(str, Enum):
    BULL = "bull"          # 多頭
    RANGE = "range"        # 盤整
    BEAR = "bear"          # 空頭
    UNKNOWN = "unknown"    # 資料不足

    @property
    def label(self) -> str:
        return _TREND_LABELS[self]


_TREND_LABELS = {Trend.BULL: "多頭", Trend.RANGE: "盤整", Trend.BEAR: "空頭", Trend.UNKNOWN: "資料不足"}

PivotKind = Literal["peak", "trough"]


@dataclass(frozen=True)
class Pivot:
    """轉折點，以收盤價計算（PROJECT_PLAN.md 4.1）。"""

    date: date
    close: float
    kind: PivotKind                 # peak = 頭，trough = 底

    @property
    def label(self) -> str:
        return "頭" if self.kind == "peak" else "底"


@dataclass(frozen=True)
class StockInfo:
    code: str
    name: str
    market: Market
    group: str                      # 族群，例：PCB
    subcategory: str | None         # 子分類，例：CCL；族群未列子分類時為 None
    themes: tuple[str, ...] = ()

    @property
    def industry(self) -> str:
        """產業鏈位置，格式同 watchlist.yaml：族群/子分類。"""
        return f"{self.group}/{self.subcategory}" if self.subcategory else self.group


@dataclass(frozen=True)
class DailyBar:
    """一根日 K，價格未還原（PROJECT_PLAN.md 8.3）。"""

    code: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int                     # 單位：張


@dataclass
class StockContext:
    """規則評估所需的一檔股票狀態，由 analysis.context.build_context 建立。"""

    info: StockInfo
    bars: pd.DataFrame              # 索引為日期，欄位 open/high/low/close/volume；最後一列為今日（盤中為暫定 K 棒）
    is_tentative: bool
    pivots: list[Pivot]             # 截至今日已確認的轉折點（盤中含暫定 K 棒）
    trend: Trend                    # 已套用 4.2 破位修正
    _ma_cache: dict[int, pd.Series] = field(default_factory=dict, init=False, repr=False)

    def ma(self, n: int) -> pd.Series:
        """MA n（含今日），計算後快取。"""
        if n not in self._ma_cache:
            self._ma_cache[n] = sma(self.bars["close"], n)
        return self._ma_cache[n]
