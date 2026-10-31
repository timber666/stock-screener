"""資料層介面（PROJECT_PLAN.md 8.3、10.3）。

job 只依賴這裡的介面；換資料庫或資料來源時，只需新增一個實作這些介面的模組。
"""

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from screener.models import DailyBar, Quote, StockInfo


class DataSourceError(Exception):
    """向外部資料來源取得資料失敗（重試後仍失敗、被拒絕或回傳格式不符）。"""


class DailyPriceSource(Protocol):
    def fetch_daily_prices(self, code: str, start: date, end: date) -> list[DailyBar]:
        """取得 start 到 end（含）的日 K；沒有成交的日子不會有資料。"""
        ...


class QuoteSource(Protocol):
    def fetch_quotes(self, stocks: Sequence[StockInfo]) -> dict[str, Quote]:
        """取得即時報價，key 為代號；查無報價的股票不在結果中。"""
        ...


class PriceStore(Protocol):
    def latest_date(self, code: str) -> date | None:
        """該檔最後一筆日 K 的日期；沒有資料時為 None。"""
        ...

    def upsert_bars(self, bars: Sequence[DailyBar]) -> int:
        """寫入日 K，同一檔同一天已存在時覆寫；回傳寫入筆數。"""
        ...

    def get_bars(self, code: str) -> list[DailyBar]:
        """該檔所有日 K，依日期由舊到新排列。"""
        ...
