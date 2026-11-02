"""資料層介面（PROJECT_PLAN.md 8.3、10.3）。

job 只依賴這裡的介面；換資料庫或資料來源時，只需新增一個實作這些介面的模組。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from screener.models import (CorporateAction, DailyBar, DividendEvent, DividendResult, Listing, Quote, ScanRun,
                             SignalRecord, StockInfo)


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


class EventSource(Protocol):
    """除權息、減資與面額變更（PROJECT_PLAN.md 14.2）。日期區間皆含頭尾，指的是事件日。"""

    def fetch_dividends(self, code: str, start: date, end: date) -> list[DividendEvent]:
        ...

    def fetch_dividend_results(self, code: str, start: date, end: date) -> list[DividendResult]:
        ...

    def fetch_capital_reductions(self, code: str, start: date, end: date) -> list[CorporateAction]:
        ...

    def fetch_splits(self, start: date, end: date) -> list[CorporateAction]:
        """全市場的面額變更、分割與反分割，不分股票。"""
        ...


class ListingSource(Protocol):
    def fetch_listings(self) -> dict[str, Listing]:
        """所有上市、上櫃證券的掛牌日，key 為代號。"""
        ...


class PriceStore(Protocol):
    def latest_date(self, code: str) -> date | None:
        """該檔最後一筆日 K 的日期；沒有資料時為 None。"""
        ...

    def earliest_date(self, code: str) -> date | None:
        """該檔第一筆日 K 的日期；沒有資料時為 None。"""
        ...

    def upsert_bars(self, bars: Sequence[DailyBar]) -> int:
        """寫入日 K，同一檔同一天已存在時覆寫；回傳寫入筆數。"""
        ...

    def get_bars(self, code: str) -> list[DailyBar]:
        """該檔所有日 K，依日期由舊到新排列。"""
        ...


@dataclass(frozen=True)
class EventCoverage:
    """某一檔已查詢過事件的期間（含頭尾）。"""

    start: date
    end: date


class EventStore(Protocol):
    """事件只存原始資料，還原價在讀取時計算（PROJECT_PLAN.md 14.2）。寫入時同一筆已存在則覆寫。"""

    def upsert_dividends(self, events: Sequence[DividendEvent]) -> int:
        ...

    def get_dividends(self, code: str) -> list[DividendEvent]:
        ...

    def upsert_dividend_results(self, results: Sequence[DividendResult]) -> int:
        ...

    def get_dividend_results(self, code: str) -> list[DividendResult]:
        ...

    def upsert_actions(self, actions: Sequence[CorporateAction]) -> int:
        ...

    def get_actions(self, code: str) -> list[CorporateAction]:
        ...

    def event_coverage(self, code: str) -> EventCoverage | None:
        """已查詢過事件的期間；從未查詢時為 None。"""
        ...

    def set_event_coverage(self, code: str, coverage: EventCoverage) -> None:
        ...

    def upsert_listings(self, listings: Sequence[Listing]) -> int:
        ...

    def get_listing(self, code: str) -> Listing | None:
        ...


class ScanStore(Protocol):
    """盤中掃描的紀錄：執行紀錄、訊號與報價快照（RESEARCH_PLAN.md 4.1）。由 scan 寫入，研究只讀取。"""

    def record_scan(self, run: ScanRun, signals: Sequence[SignalRecord], quotes: Sequence[Quote]) -> int:
        """在同一個交易內寫入一次掃描的全部紀錄，回傳執行紀錄的 id。"""
        ...

    def get_scan_runs(self, start: date, end: date) -> list[ScanRun]:
        """資料日期在 start～end（含）的執行紀錄，依日期、掃描時間排列。"""
        ...

    def get_signal_records(self, run_id: int) -> list[SignalRecord]:
        ...

    def get_snapshot(self, run_id: int) -> dict[str, Quote]:
        """該次掃描的報價快照，key 為代號；不含原始資料列（另存壓縮檔）。"""
        ...
