"""全市場資料的回補與更新：python -m screener market-update（RESEARCH_PLAN.md 4.3）。

1. FinMind：交易日曆、下市清單、面額變更（各一次請求，每次都重抓）。
2. 每日行情：每個交易日上市、上櫃各一次請求。週一至五與交易日曆上的週六（補班交易日）都會查詢，
   以交易所的回應判斷是否為非交易日，並與交易日曆互相檢查。一天兩個市場抓完才在同一個交易內寫入，
   中斷後重新執行會從沒有抓取紀錄的那天繼續。
3. 事件：除權息與報酬指數月表每月一期、減資每年一期；期間還沒結束時抓到的不算完成，下次再抓。

當天的行情可能還沒公布，查詢會被誤判為非交易日，所以只抓到昨天。原始回應另存壓縮檔，解析有誤時
不必重新抓取。證交所、櫃買中心各自計算請求間隔（research.yaml 的 market.exchange）。
"""

import gzip
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from screener.config import AppConfig, ConfigError, ResearchConfig, Secrets
from screener.data.base import DataSourceError
from screener.jobs import JobFailed
from screener.models import CorporateAction, Market
from screener.research.market import tpex, twse
from screener.research.market.common import is_common_stock
from screener.research.market.finmind import MarketFinMindClient
from screener.research.market.models import OK, DailyQuotes, Delisting, FetchRecord
from screener.research.market.store import SqliteMarketStore

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 20                 # 每抓幾天在 log 記錄一次進度
_LABELS = {Market.TSE: "上市", Market.OTC: "上櫃"}


def daily_source(market: Market) -> str:
    return f"{market.value}_daily"


DIVIDENDS = {Market.TSE: "tse_dividends", Market.OTC: "otc_dividends"}
REDUCTIONS = {Market.TSE: "tse_reductions", Market.OTC: "otc_reductions"}
RETURN_INDEX_MONTHLY = "tse_return_index"
FINMIND_SOURCE = "finmind"          # corporate_actions 的來源：面額變更


class ExchangeSource(Protocol):
    def daily(self, day: date) -> Any: ...
    def dividend_results(self, start: date, end: date) -> Any: ...
    def capital_reductions(self, start: date, end: date) -> Any: ...


class TwseSource(ExchangeSource, Protocol):
    def return_index(self, month: date) -> Any: ...


class CalendarSource(Protocol):
    def fetch_trading_days(self, start: date, end: date) -> list[date]: ...
    def fetch_delistings(self, end: date) -> list[Delisting]: ...
    def fetch_splits(self, start: date, end: date) -> list[CorporateAction]: ...


@dataclass(frozen=True)
class Sources:
    twse: TwseSource
    tpex: ExchangeSource
    finmind: CalendarSource


class RawArchive:
    """原始回應存成 <root>/<來源>/<key>.json.gz。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, source: str, key: str, body: Any) -> None:
        path = self._root / source / f"{key}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)


@dataclass
class MarketUpdateSummary:
    trading_days: int = 0           # 這次抓到行情的交易日
    closed_days: int = 0            # 這次判定為非交易日的日子
    skipped_days: int = 0           # 已有抓取紀錄而略過的日子
    requests: int = 0               # 證交所、櫃買中心的請求數
    off_calendar: list[date] = field(default_factory=list)   # 有行情卻不在 FinMind 交易日曆上的日子
    event_periods: int = 0
    dividends: int = 0
    reductions: int = 0
    splits: int = 0
    delistings: int = 0
    securities: int = 0


def run(config: AppConfig, research: ResearchConfig, secrets: Secrets, *, start: date | None = None,
        end: date | None = None) -> MarketUpdateSummary:
    market = research.market
    today = date.today()
    start = start or market.start
    end = end or today - timedelta(days=1)
    if end >= today:
        raise ConfigError(f"--end {end}：只能抓到昨天，當天的行情可能還沒公布，會被誤判為非交易日")
    if start > end:
        raise ConfigError(f"起始日 {start} 晚於結束日 {end}")
    if not secrets.finmind_token:
        logger.warning("未設定 FINMIND_TOKEN，以未登入身分查詢 FinMind（請求上限較低）")
    sources = Sources(twse=twse.TwseClient(market.exchange), tpex=tpex.TpexClient(market.exchange),
                      finmind=MarketFinMindClient(config.settings.finmind, secrets.finmind_token))
    with SqliteMarketStore(market.db_path) as store:
        return update_market(store, sources, archive=RawArchive(market.raw_dir), start=start, end=end, today=today)


def update_market(store: SqliteMarketStore, sources: Sources, *, archive: RawArchive, start: date, end: date,
                  today: date, clock: Callable[[], float] = time.monotonic) -> MarketUpdateSummary:
    summary = MarketUpdateSummary()
    calendar = _update_finmind(store, sources.finmind, start, end, today, summary)
    _update_quotes(store, sources, archive, calendar, start=start, end=end, today=today, summary=summary,
                   clock=clock)
    _update_events(store, sources, archive, start=start, end=end, today=today, summary=summary)
    summary.securities = store.refresh_securities()
    _log_summary(summary)
    return summary


# --- 1. FinMind ---------------------------------------------------------------

def _update_finmind(store: SqliteMarketStore, source: CalendarSource, start: date, end: date, today: date,
                    summary: MarketUpdateSummary) -> list[date]:
    try:
        calendar = source.fetch_trading_days(start, end)
        delistings = source.fetch_delistings(today)
        splits = [a for a in source.fetch_splits(start, today) if is_common_stock(a.code)]
    except DataSourceError as exc:
        raise JobFailed(f"FinMind 抓取失敗：{exc}") from exc
    store.record_calendar(calendar)
    store.replace_delistings(delistings)
    store.record_actions(None, FINMIND_SOURCE, splits)
    summary.delistings, summary.splits = len(delistings), len(splits)
    logger.info("FinMind：交易日曆 %d 天（%s～%s）、下市 %d 檔、面額變更 %d 筆", len(calendar),
                calendar[0] if calendar else "-", calendar[-1] if calendar else "-", len(delistings), len(splits))
    return calendar


# --- 2. 每日行情 ---------------------------------------------------------------

def _update_quotes(store: SqliteMarketStore, sources: Sources, archive: RawArchive, calendar: list[date], *,
                   start: date, end: date, today: date, summary: MarketUpdateSummary,
                   clock: Callable[[], float]) -> None:
    records = {m: store.fetch_records(daily_source(m)) for m in Market}
    on_calendar = set(calendar)
    days = [d for d in _days(start, end) if d.weekday() < 5 or d in on_calendar]
    todo = [d for d in days if any(d not in records[m] for m in Market)]
    summary.skipped_days = len(days) - len(todo)
    logger.info("每日行情：%s～%s 要查詢的日子 %d 天，已有紀錄 %d 天，這次查詢 %d 天", start, end, len(days),
                summary.skipped_days, len(todo))
    clients = {Market.TSE: (sources.twse, twse.parse_daily), Market.OTC: (sources.tpex, tpex.parse_daily)}
    started = clock()
    for n, day in enumerate(todo, start=1):
        fetched: dict[Market, DailyQuotes | None] = {}
        opened: dict[Market, bool] = {}
        try:
            for market, (client, parse) in clients.items():
                if (record := records[market].get(day)) is not None:
                    opened[market] = record.status == OK
                    continue
                body = client.daily(day)
                summary.requests += 1
                archive.save(daily_source(market), f"{day:%Y}/{day:%Y%m%d}", body)
                fetched[market] = parse(body, day)
                opened[market] = fetched[market] is not None
            _check_day(day, opened, on_calendar, calendar[-1] if calendar else None, summary)
        except DataSourceError as exc:
            raise JobFailed(f"每日行情 {day} 抓取失敗：{exc}。已完成 {n - 1}/{len(todo)} 天，"
                            f"重新執行會從中斷處繼續") from exc
        store.record_day(day, [(daily_source(m), daily) for m, daily in fetched.items()], today)
        trading = all(opened.values())
        if trading:
            summary.trading_days += 1
        else:
            summary.closed_days += 1
        counts = "、".join(f"{_LABELS[m]} {len(d.quotes)} 檔" for m, d in fetched.items() if d is not None)
        logger.debug("%s：%s", day, counts if trading else "非交易日")
        if n % PROGRESS_EVERY == 0 or n == len(todo):
            remaining = (clock() - started) / n * (len(todo) - n)
            logger.info("每日行情 [%d/%d] %s：%s；預估剩餘 %d 分鐘", n, len(todo), day, counts or "非交易日",
                        round(remaining / 60))


def _check_day(day: date, opened: dict[Market, bool], on_calendar: set[date], calendar_end: date | None,
               summary: MarketUpdateSummary) -> None:
    """兩個市場要一致；兩邊都沒有行情的交易日（日曆上有）視為錯誤：可能是還沒公布或回應異常。"""
    if len(set(opened.values())) > 1:
        status = "、".join(f"{_LABELS[m]}{'有' if v else '沒有'}行情" for m, v in opened.items())
        raise DataSourceError(f"兩個市場不一致（{status}）")
    trading = all(opened.values())
    if not trading and day in on_calendar:
        raise DataSourceError("FinMind 交易日曆有這一天，但證交所、櫃買中心都沒有行情（可能還沒公布）")
    if trading and calendar_end is not None and day <= calendar_end and day not in on_calendar:
        summary.off_calendar.append(day)
        logger.warning("%s 有行情，但不在 FinMind 的交易日曆上", day)


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


# --- 3. 事件 -------------------------------------------------------------------

def _update_events(store: SqliteMarketStore, sources: Sources, archive: RawArchive, *, start: date, end: date,
                   today: date, summary: MarketUpdateSummary) -> None:
    """除權息、報酬指數月表每月一期；減資每年一期。每期查詢完整的月份或年份，與 start、end 無關。"""
    exchanges = {Market.TSE: (sources.twse, twse.parse_dividend_results, twse.parse_capital_reductions),
                 Market.OTC: (sources.tpex, tpex.parse_dividend_results, tpex.parse_capital_reductions)}
    records = {source: store.fetch_records(source)
               for source in (*DIVIDENDS.values(), *REDUCTIONS.values(), RETURN_INDEX_MONTHLY)}

    def due(source: str, period_start: date) -> bool:
        record = records[source].get(period_start)
        return record is None or not record.complete

    tasks: list[tuple[str, date, date]] = []
    for month_start, month_end in _months(start, end):
        tasks += [(DIVIDENDS[Market.TSE], month_start, month_end), (DIVIDENDS[Market.OTC], month_start, month_end),
                  (RETURN_INDEX_MONTHLY, month_start, month_end)]
    for year in range(start.year, end.year + 1):
        tasks += [(REDUCTIONS[m], date(year, 1, 1), date(year, 12, 31)) for m in Market]
    tasks = [task for task in tasks if due(task[0], task[1])]
    logger.info("事件：要查詢 %d 期（除權息、報酬指數月表每月，減資每年）", len(tasks))

    for source, period_start, period_end in tasks:
        where = f"{source} {period_start}～{period_end}"
        try:
            if source == RETURN_INDEX_MONTHLY:
                body = sources.twse.return_index(period_start)
            else:
                market = Market.TSE if source.startswith("tse") else Market.OTC
                client, parse_dividends, parse_reductions = exchanges[market]
                fetch = client.dividend_results if source in DIVIDENDS.values() else client.capital_reductions
                body = fetch(period_start, period_end)
            summary.requests += 1
            archive.save(source, f"{period_start:%Y%m%d}", body)
            if source == RETURN_INDEX_MONTHLY:
                values = twse.parse_return_index(body, where)
                store.record_return_index_monthly(_record(source, period_start, period_end, len(values), today),
                                                  values)
            elif source in DIVIDENDS.values():
                results = parse_dividends(body, where)
                store.record_dividend_results(_record(source, period_start, period_end, len(results), today), market,
                                              results)
                summary.dividends += len(results)
            else:
                actions = parse_reductions(body, where)
                store.record_actions(_record(source, period_start, period_end, len(actions), today), market.value,
                                     actions)
                summary.reductions += len(actions)
        except DataSourceError as exc:
            raise JobFailed(f"事件 {where} 抓取失敗：{exc}。重新執行會從中斷處繼續") from exc
        summary.event_periods += 1
        logger.debug("事件 %s：完成", where)


def _record(source: str, start: date, end: date, rows: int, today: date) -> FetchRecord:
    return FetchRecord(source, start, end, OK, rows, today)


def _months(start: date, end: date) -> Iterator[tuple[date, date]]:
    month = date(start.year, start.month, 1)
    while month <= end:
        following = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        yield month, following - timedelta(days=1)
        month = following


def _log_summary(summary: MarketUpdateSummary) -> None:
    logger.info("全市場資料更新完成：交易日 %d 天、非交易日 %d 天（已有紀錄略過 %d 天）；事件 %d 期，除權息 %d 筆、"
                "減資 %d 筆；面額變更 %d 筆、下市 %d 檔；證券 %d 檔；證交所與櫃買中心請求 %d 次",
                summary.trading_days, summary.closed_days, summary.skipped_days, summary.event_periods,
                summary.dividends, summary.reductions, summary.splits, summary.delistings, summary.securities,
                summary.requests)
    if summary.off_calendar:
        logger.warning("有行情卻不在 FinMind 交易日曆上的日子 %d 天：%s", len(summary.off_calendar),
                       "、".join(map(str, summary.off_calendar)))
