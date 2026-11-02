"""抓取除權息、減資、面額變更與上市櫃日並寫入資料庫：python -m screener events（PROJECT_PLAN.md 14.2）。

上市日／上櫃日取自證交所 ISIN 清單（D38），每次執行都重新取得，並順便檢查 watchlist.yaml 的 market。

每檔查詢的期間從資料庫第一筆日 K 開始，只需要涵蓋有價格的日子。已查過的股票只從上次查詢的
迄日往前重疊 refetch_days 天（research.yaml 的 events）再查，補上較晚公告的資料；當天已查過的股票略過，
因此超過請求上限中止後重新執行，不會重複查詢已完成的股票。寫入時同一筆已存在則覆寫。
抓完後以官方的除權息參考價交叉驗證股利資料，差異大的事件寫入 log。還原時以官方的前後價格為優先（D37），
官方有、股利資料沒有的事件（多為現金增資除權）仍會正確還原，只列入統計。
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from screener.analysis.adjustment import reference_price, trading_day_index
from screener.config import AppConfig, ResearchConfig, Secrets
from screener.data.base import DataSourceError, EventCoverage, EventSource, EventStore, ListingSource, PriceStore
from screener.data.db import SqlitePriceStore
from screener.data.finmind import FinMindClient
from screener.data.isin import IsinClient
from screener.jobs import JobFailed
from screener.models import StockInfo

logger = logging.getLogger(__name__)


class Store(PriceStore, EventStore, Protocol):
    """events 需要同時讀日 K 與寫入事件。"""


@dataclass
class EventSummary:
    dividends: int = 0
    results: int = 0
    actions: int = 0
    queried: int = 0                                        # 有查詢的檔數
    up_to_date: int = 0                                     # 今天已查過、略過的檔數
    no_bars: list[str] = field(default_factory=list)        # 資料庫沒有日 K，不需查詢的代號
    unlisted: list[str] = field(default_factory=list)       # ISIN 清單查無的代號
    wrong_market: list[str] = field(default_factory=list)   # 與 watchlist.yaml 的 market 不同的代號


def run(config: AppConfig, research: ResearchConfig, secrets: Secrets) -> None:
    if not secrets.finmind_token:
        logger.warning("未設定 FINMIND_TOKEN，以未登入身分查詢（請求上限較低）")
    events = research.events
    source = FinMindClient(config.settings.finmind, secrets.finmind_token,
                           dividend_lookahead_days=events.dividend_lookahead_days)
    with SqlitePriceStore(config.settings.db_path) as store:
        update_events(config.watchlist.stocks, source=source, listings=IsinClient(research.isin),
                      store=store, today=date.today(), refetch_days=events.refetch_days)
        check_reference_prices(config.watchlist.stocks, store=store, tolerance_pct=events.reference_tolerance_pct)


def update_events(stocks: Sequence[StockInfo], *, source: EventSource, listings: ListingSource, store: Store,
                  today: date, refetch_days: int) -> EventSummary:
    """先取得上市櫃日（證交所）與全市場的面額變更（FinMind 一次請求），再逐檔查股利、除權息結果與減資
    （每檔三次請求）。已查過的股票從上次查詢的迄日往前重疊 refetch_days 天再查。

    每檔查完立即寫入並記錄查詢期間，中途失敗（例：超過每小時請求上限）時重新執行會從中斷處繼續。
    """
    summary = EventSummary()
    _update_listings(stocks, listings, store, summary)
    earliest = {stock.code: store.earliest_date(stock.code) for stock in stocks}
    summary.no_bars = [code for code, day in earliest.items() if day is None]
    starts = [day for day in earliest.values() if day is not None]
    if not starts:
        raise JobFailed("資料庫沒有任何日 K，請先執行 update")

    codes = set(earliest)
    try:
        splits = [a for a in source.fetch_splits(min(starts), today) if a.code in codes]
    except DataSourceError as exc:
        raise JobFailed(f"面額變更抓取失敗：{exc}") from exc
    summary.actions += store.upsert_actions(splits)

    total = len(stocks)
    for i, stock in enumerate(stocks, start=1):
        first_bar = earliest[stock.code]
        if first_bar is None:
            continue
        coverage = store.event_coverage(stock.code)
        if coverage is not None and coverage.start <= first_bar and coverage.end >= today:
            summary.up_to_date += 1
            continue
        if coverage is not None and coverage.start <= first_bar:
            start = max(coverage.end - timedelta(days=refetch_days), first_bar)
        else:
            start = first_bar
        progress = f"[{i}/{total}] {stock.code} {stock.name}"
        try:
            dividends = source.fetch_dividends(stock.code, start, today)
            results = source.fetch_dividend_results(stock.code, start, today)
            reductions = source.fetch_capital_reductions(stock.code, start, today)
        except DataSourceError as exc:
            _log_summary(summary)
            raise JobFailed(f"{progress} 抓取失敗：{exc}。剩餘 {total - i + 1} 檔未查詢，重新執行會從中斷處繼續") from exc

        summary.dividends += store.upsert_dividends(dividends)
        summary.results += store.upsert_dividend_results(results)
        summary.actions += store.upsert_actions(reductions)
        store.set_event_coverage(stock.code, EventCoverage(min(start, coverage.start) if coverage else start, today))
        summary.queried += 1
        logger.info("%s：%s ~ %s，除權息 %d 筆、官方結果 %d 筆、減資 %d 筆",
                    progress, start, today, len(dividends), len(results), len(reductions))

    _log_summary(summary)
    return summary


def _update_listings(stocks: Sequence[StockInfo], source: ListingSource, store: Store, summary: EventSummary) -> None:
    try:
        listings = source.fetch_listings()
    except DataSourceError as exc:
        raise JobFailed(f"上市櫃日抓取失敗：{exc}") from exc
    found = [listings[s.code] for s in stocks if s.code in listings]
    store.upsert_listings(found)
    summary.unlisted = [s.code for s in stocks if s.code not in listings]
    summary.wrong_market = [s.code for s in stocks if s.code in listings and listings[s.code].market is not s.market]
    logger.info("上市櫃日：觀察清單 %d 檔中查到 %d 檔", len(stocks), len(found))
    if summary.unlisted:
        logger.warning("%d 檔不在證交所的上市、上櫃清單中（可能已下市或代號錯誤），回測無法排除興櫃期間：%s",
                       len(summary.unlisted), "、".join(summary.unlisted))
    for stock in stocks:
        if stock.code in summary.wrong_market:
            logger.warning("%s %s 在 watchlist.yaml 的 market 為 %s，證交所清單為 %s", stock.code, stock.name,
                           stock.market.value, listings[stock.code].market.value)


def _log_summary(summary: EventSummary) -> None:
    logger.info("事件更新結果：查詢 %d 檔（今天已查過而略過 %d 檔），寫入除權息 %d 筆、官方結果 %d 筆、減資與面額變更 %d 筆",
                summary.queried, summary.up_to_date, summary.dividends, summary.results, summary.actions)
    if summary.no_bars:
        logger.warning("%d 檔資料庫沒有日 K，未查詢事件：%s", len(summary.no_bars), "、".join(summary.no_bars))


@dataclass(frozen=True)
class ReferenceMismatch:
    code: str
    day: date
    computed: float | None          # 以資料庫前一日收盤與股利計算的參考價；沒有對應的股利資料時為 None
    official: float


def check_reference_prices(stocks: Sequence[StockInfo], *, store: Store,
                           tolerance_pct: float) -> list[ReferenceMismatch]:
    """以官方除權息參考價驗證股利資料：差異超過 tolerance_pct（%），或官方有結果而股利資料沒有的事件。"""
    tolerance = tolerance_pct / 100
    mismatches: list[ReferenceMismatch] = []
    compared = 0
    max_error = 0.0
    for stock in stocks:
        bars = store.get_bars(stock.code)
        days = [bar.date for bar in bars]
        position = {day: i for i, day in enumerate(days)}
        dividends: dict[int, tuple[float, float]] = {}      # 順延到交易日後的位置 → (現金, 股票)
        for event in store.get_dividends(stock.code):
            if (i := trading_day_index(days, event.date)) is not None:
                cash, stock_dividend = dividends.get(i, (0.0, 0.0))
                dividends[i] = (cash + event.cash_dividend, stock_dividend + event.stock_dividend)
        for result in store.get_dividend_results(stock.code):
            i = position.get(result.date)
            if i is None or i == 0:
                continue                        # 官方結果的日期沒有日 K，或沒有前一日收盤可比對
            if i not in dividends:
                mismatches.append(ReferenceMismatch(stock.code, result.date, None, result.after_price))
                continue
            computed = reference_price(bars[i - 1].close, *dividends[i])
            error = abs(computed / result.after_price - 1)
            compared += 1
            max_error = max(max_error, error)
            if error > tolerance:
                mismatches.append(ReferenceMismatch(stock.code, result.date, computed, result.after_price))

    differ = [m for m in mismatches if m.computed is not None]
    only_official = [m for m in mismatches if m.computed is None]
    logger.info("官方參考價比對：%d 筆，最大誤差 %.2f%%；差異超過 %.1f%% 的 %d 筆，官方有而股利資料沒有的 %d 筆"
                "（多為現金增資除權）。還原時以官方前後價格為準", compared, max_error * 100,
                tolerance_pct, len(differ), len(only_official))
    for m in differ:
        logger.info("  %s %s 參考價差異 %.2f%%：以股利金額計算 %.4f，官方 %s", m.code, m.day,
                    (m.computed / m.official - 1) * 100, m.computed, m.official)
    for m in only_official:
        logger.info("  %s %s 只有官方結果（參考價 %s）", m.code, m.day, m.official)
    return mismatches
