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


class Side(str, Enum):
    """規則的方向。前置條件只管同方向的進場規則（PROJECT_PLAN.md 5.1）。"""

    LONG = "long"          # 多方
    SHORT = "short"        # 空方

    @property
    def label(self) -> str:
        return "多方" if self is Side.LONG else "空方"

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


@dataclass(frozen=True)
class Listing:
    """證交所 ISIN 清單記載的掛牌資訊（PROJECT_PLAN.md 14.2、D38）。"""

    code: str
    market: Market
    listed_on: date                 # 在目前市場掛牌的日子（上櫃轉上市者為轉上市日）


@dataclass(frozen=True)
class DividendEvent:
    """除權息事件（PROJECT_PLAN.md 14.2）。股利為盈餘配發與公積配發的合計。"""

    code: str
    date: date                      # 除權息交易日（原定日期；遇休市時，還原計算順延到下一個交易日）
    cash_dividend: float            # 現金股利（元／股）
    stock_dividend: float           # 股票股利（元／股，以面額 10 元計，即每 10 股配發的股數）


class ActionKind(str, Enum):
    CAPITAL_REDUCTION = "capital_reduction"     # 減資
    SPLIT = "split"                             # 面額變更、分割、反分割


@dataclass(frozen=True)
class CorporateAction:
    """減資或面額變更、分割、反分割（PROJECT_PLAN.md 14.2）。調整因子直接取官方公告的 after_price / before_price。"""

    code: str
    date: date
    kind: ActionKind
    before_price: float             # 事件前一日收盤價
    after_price: float              # 事件後參考價


@dataclass(frozen=True)
class DividendResult:
    """官方公告的除權息前後價格（PROJECT_PLAN.md 14.2）。還原時優先使用（D37），並用來交叉驗證股利資料。"""

    code: str
    date: date                      # 除權息交易日
    before_price: float             # 除權息前一日收盤價
    after_price: float              # 除權息參考價


@dataclass(frozen=True)
class Quote:
    """盤中即時報價（MIS 快照，PROJECT_PLAN.md 8.2）。MIS 沒有值（"-"）的欄位為 None。"""

    code: str
    date: date                      # 資料日期；非交易日為最後一個交易日
    open: float | None              # None：今日尚無成交
    high: float | None
    low: float | None
    last: float | None              # 最新成交價（MIS 的 z，為 "-" 時用最後一筆成交 trade.z，D47）；None：兩者都沒有
    prev_close: float | None        # 參考價（昨收；除權息日為除權息參考價）
    volume: int | None              # 累積成交量（張），只含一般交易
    best_bid: float | None          # 最佳買價第一檔；跌停鎖死時沒有買單
    best_ask: float | None          # 最佳賣價第一檔；漲停鎖死時沒有賣單


class PriceSubstitute(str, Enum):
    """盤中沒有成交價時，現價的替代來源（PROJECT_PLAN.md 8.2）。"""

    BEST_BID = "best_bid"
    BEST_ASK = "best_ask"

    @property
    def label(self) -> str:
        return "最佳買價" if self is PriceSubstitute.BEST_BID else "最佳賣價"


@dataclass(frozen=True)
class Signal:
    """一條進場規則的觸發結果。"""

    rule_id: str
    rule_name: str
    detail: str                     # 例："量比 1.80x，突破 40 日高 123.5"
    side: Side


@dataclass
class StockContext:
    """規則評估所需的一檔股票狀態，由 analysis.context.build_context 建立。"""

    info: StockInfo
    bars: pd.DataFrame              # 索引為日期，欄位 open/high/low/close/volume；最後一列為今日（盤中為暫定 K 棒）
    is_tentative: bool
    pivots: list[Pivot]             # 截至今日已確認的轉折點（盤中含暫定 K 棒）
    trend: Trend                    # 已套用 4.2 破位修正
    _ma_cache: dict[int, pd.Series] = field(default_factory=dict, init=False, repr=False)

    @property
    def today(self) -> pd.Series:
        """今日 K 棒（盤中為暫定 K 棒），欄位 open/high/low/close/volume。"""
        return self.bars.iloc[-1]

    @property
    def yesterday(self) -> pd.Series | None:
        """昨日 K 棒；只有一根 K 棒時為 None。"""
        return self.bars.iloc[-2] if len(self.bars) >= 2 else None

    def ma(self, n: int) -> pd.Series:
        """MA n（含今日），計算後快取。"""
        if n not in self._ma_cache:
            self._ma_cache[n] = sma(self.bars["close"], n)
        return self._ma_cache[n]


@dataclass(frozen=True)
class Hit:
    """觸發至少一條同方向進場規則的股票。"""

    ctx: StockContext
    side: Side
    signals: tuple[Signal, ...]
    change_pct: float | None        # 漲跌幅（%）；回放以前一日收盤計算，盤中以 MIS 參考價計算
    price_substitute: PriceSubstitute | None = None     # 盤中沒有成交價時現價的替代來源（8.2）


@dataclass(frozen=True)
class ScanReport:
    """一次掃描的結果，供組成推播訊息（PROJECT_PLAN.md 第 11 節）。"""

    day: date
    is_replay: bool
    observed: int                   # 觀察清單檔數
    hits: tuple[Hit, ...]
    not_evaluated: tuple[str, ...]  # 當日沒有 K 棒（回放）或無成交、無報價（盤中）而未評估的代號
    scan_time: str | None = None    # 盤中掃描時間，例 "13:10"；回放為 None
    prev_close_mismatch: tuple[str, ...] = ()   # MIS 參考價與資料庫昨收不一致的代號（盤中，7.2）
