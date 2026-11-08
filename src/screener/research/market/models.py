"""全市場資料的型別（RESEARCH_PLAN.md 4.3）。除權息與公司行動沿用共用的 DividendResult、CorporateAction。"""

from dataclasses import dataclass
from datetime import date

from screener.models import Market

SHARES_PER_LOT = 1000               # 1 張 = 1000 股


@dataclass(frozen=True)
class MarketQuote:
    """一檔股票一天的每日收盤行情。沒有成交的日子仍在行情表中，價格為 None。"""

    code: str
    date: date
    market: Market
    name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int                     # 成交股數（股）；上櫃照櫃買中心的口徑（R10）
    value: int                      # 成交金額（元）
    trades: int                     # 成交筆數
    change: float | None            # 漲跌價差（帶正負號）；沒有成交或有標示（flag）時為 None
    flag: str | None = None         # 漲跌欄位的標示：上市為 X（不比價），上櫃為「除息」「除權」等文字
    next_limit_up: float | None = None      # 上櫃：次日漲停價（上市的行情沒有）
    next_limit_down: float | None = None    # 上櫃：次日跌停價

    @property
    def traded(self) -> bool:
        return self.close is not None

    @property
    def reference(self) -> float | None:
        """當天的參考價 = 收盤 − 漲跌價差；有標示（除權息等）的日子算不出來。"""
        return None if self.close is None or self.change is None else self.close - self.change


@dataclass(frozen=True)
class DailyQuotes:
    """一個市場一天的行情：普通股的行情，與上市才有的發行量加權股價報酬指數。"""

    day: date
    market: Market
    quotes: tuple[MarketQuote, ...]
    return_index: float | None = None


@dataclass(frozen=True)
class Delisting:
    """FinMind TaiwanStockDelisting 的一筆下市紀錄。"""

    code: str
    date: date
    name: str


@dataclass(frozen=True)
class MonthlyRevenue:
    """公開資訊觀測站的月營收（千元），與當月所屬的產業別（RESEARCH_PLAN.md 4.4、R20）。"""

    code: str
    month: date                     # 營收月份的 1 日
    market: Market
    name: str
    industry: str
    revenue: int | None
    previous: int | None            # 上月營收
    last_year: int | None           # 去年當月營收
    cumulative: int | None          # 當月累計營收
    cumulative_last_year: int | None


@dataclass(frozen=True)
class Valuation:
    """取樣日的本益比、股價淨值比與殖利率（RESEARCH_PLAN.md 4.5、R25）。虧損的本益比沒有值。"""

    code: str
    date: date
    market: Market
    pe: float | None
    pb: float | None
    dividend_yield: float | None    # %


@dataclass(frozen=True)
class SharesOutstanding:
    """取樣日的發行股數（股）。"""

    code: str
    date: date
    market: Market
    shares: int


CLOSED = "closed"                   # 抓取紀錄的狀態：非交易日
OK = "ok"


@dataclass(frozen=True)
class FetchRecord:
    """抓取紀錄：每日行情每個交易日、每個市場一筆；事件以月或年為一期。供中斷後續跑。"""

    source: str                     # 例：tse_daily、otc_dividends
    start: date
    end: date
    status: str                     # OK 或 CLOSED（非交易日）
    rows: int
    fetched_on: date

    @property
    def complete(self) -> bool:
        """抓取時期間已經結束：之後不會再有新資料，不需要重抓。"""
        return self.fetched_on > self.end


@dataclass(frozen=True)
class Security:
    """market.db 的證券清單：由每日行情彙整，名稱與市場取最後一個交易日。"""

    code: str
    name: str
    market: Market
    first_date: date                # 第一次出現在行情的日子
    last_date: date                 # 最後一次出現在行情的日子
    delisted_on: date | None        # FinMind 的下市日
