"""盤中 job：合成暫定 K 棒、執行規則引擎並推播（PROJECT_PLAN.md 7.2）。

盤中模式以 MIS 快照合成今日暫定 K 棒，接在資料庫的歷史日 K 之後；回放模式（--replay）以資料庫中
該日（含）以前的日 K 建立 StockContext，該日 K 棒視為「今日」。結果推播到 Discord，--dry-run 時只印在終端。
"""

import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import TextIO

from screener.analysis.context import bars_to_frame, build_context
from screener.analysis.numeric import normalize
from screener.config import AppConfig, MisSettings, TrendConfig
from screener.data.base import DataSourceError, PriceStore, QuoteSource
from screener.data.mis import MisClient
from screener.engine import Engine
from screener.jobs import JobFailed, open_existing_store
from screener.models import DailyBar, Hit, PriceSubstitute, Quote, ScanReport, Side, StockContext, StockInfo
from screener.notify.base import Notifier, NotifyError
from screener.notify.discord import MESSAGE_LIMIT
from screener.notify.message import format_messages

logger = logging.getLogger(__name__)


def run(config: AppConfig, *, replay: date | None, notifier: Notifier | None, out: TextIO | None = None,
        now: datetime | None = None) -> None:
    """結果推播到 notifier；notifier 為 None 時（--dry-run）印到 out（預設 stdout）。

    超過單則上限時依族群拆成多則（第 11 節）。now 預設為目前時間，盤中模式以它判斷「今天」。
    """
    engine = Engine.from_config(config.rules)
    stocks, trend_config = config.watchlist.stocks, config.rules.trend
    with open_existing_store(config.settings.db_path) as store:
        if replay is not None:
            report = replay_scan(stocks, store=store, engine=engine, trend_config=trend_config, day=replay)
        else:
            report = intraday_scan(stocks, source=MisClient(config.settings.mis), store=store, engine=engine,
                                   trend_config=trend_config, settings=config.settings.mis, now=now or datetime.now())
    if report is None:                          # 非交易日：直接結束，不推播
        return

    messages = format_messages(report, config.watchlist.industries, MESSAGE_LIMIT)
    if notifier is None:
        _print_messages(messages, out or sys.stdout)
        return
    for i, message in enumerate(messages, start=1):
        try:
            notifier.send(message)
        except NotifyError as exc:
            raise JobFailed(f"推播失敗（第 {i}/{len(messages)} 則）：{exc}") from exc
    logger.info("已推播 Discord：%d 則", len(messages))


def _print_messages(messages: Sequence[str], out: TextIO) -> None:
    """多則時以分隔線標示每一則的範圍，方便確認拆分結果。"""
    if len(messages) > 1:
        messages = [f"──── 第 {i}/{len(messages)} 則 ────\n{message}" for i, message in enumerate(messages, start=1)]
    out.write("\n\n".join(messages) + "\n")


# --- 盤中 -------------------------------------------------------------------

def intraday_scan(
    stocks: Sequence[StockInfo],
    *,
    source: QuoteSource,
    store: PriceStore,
    engine: Engine,
    trend_config: TrendConfig,
    settings: MisSettings,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanReport | None:
    """以 MIS 快照合成今日暫定 K 棒，接在資料庫中今天以前的日 K 之後評估每一檔。

    MIS 的資料日期都不是今天時判定為非交易日，回傳 None。今日無成交或無報價的股票不評估（8.2）。
    """
    today = now.date()
    quotes = _fetch_quotes(source, stocks)
    if not quotes:
        raise JobFailed("MIS 沒有回傳任何觀察清單股票的報價：請確認 watchlist.yaml 的 market，或 MIS 格式是否變動")
    if all(quote.date != today for quote in quotes.values()):
        logger.info("MIS 資料日期為 %s，不是今天 %s：非交易日，不掃描",
                    max(quote.date for quote in quotes.values()), today)
        return None
    _retry_missing_prices(stocks, quotes, source=source, settings=settings, today=today, sleep=sleep)

    hits: list[Hit] = []
    not_evaluated: list[str] = []
    mismatched: list[str] = []
    substituted: list[str] = []
    for stock in stocks:
        quote = quotes.get(stock.code)
        if quote is None:
            logger.warning("MIS 查無 %s %s 的報價，請確認 watchlist.yaml 的 market", stock.code, stock.name)
            not_evaluated.append(stock.code)
            continue
        if quote.date != today:
            logger.warning("%s %s 的 MIS 資料日期為 %s，不是今天，未評估", stock.code, stock.name, quote.date)
            not_evaluated.append(stock.code)
            continue

        history = [bar for bar in store.get_bars(stock.code) if bar.date < today]
        if not _prev_close_matches(quote, history):
            mismatched.append(stock.code)
            logger.warning("%s %s 昨收不一致：MIS 參考價 %s，資料庫 %s", stock.code, stock.name, quote.prev_close,
                           f"{history[-1].date} 收盤 {history[-1].close}" if history else "沒有日 K")

        bar, substitute = _tentative_bar(quote, today)
        if bar is None:
            not_evaluated.append(stock.code)
            continue
        if substitute is not None:
            substituted.append(stock.code)
            logger.info("%s %s 沒有成交價，改用%s %s", stock.code, stock.name, substitute.label, bar.close)

        ctx = build_context(stock, bars_to_frame([*history, bar]), trend_config, is_tentative=True)
        hits += _evaluate(ctx, engine, change_pct=_change_from_reference(bar.close, quote.prev_close),
                          price_substitute=substitute)

    if len(not_evaluated) == len(stocks):
        raise JobFailed(f"觀察清單沒有任何股票 {today} 有成交：尚未開盤，或 MIS 資料異常")
    logger.info("盤中掃描 %s：觀察 %d 檔，評估 %d 檔（其中 %d 檔為替代價），昨收不一致 %d 檔，觸發多方 %d 檔、空方 %d 檔",
                now.strftime("%Y-%m-%d %H:%M"), len(stocks), len(stocks) - len(not_evaluated), len(substituted),
                len(mismatched), *_side_counts(hits))
    return ScanReport(day=today, is_replay=False, observed=len(stocks), hits=tuple(hits),
                      not_evaluated=tuple(not_evaluated), scan_time=now.strftime("%H:%M"),
                      prev_close_mismatch=tuple(mismatched))


def _fetch_quotes(source: QuoteSource, stocks: Sequence[StockInfo]) -> dict[str, Quote]:
    try:
        return source.fetch_quotes(stocks)
    except DataSourceError as exc:
        raise JobFailed(f"盤中報價抓取失敗：{exc}") from exc


def _retry_missing_prices(
    stocks: Sequence[StockInfo],
    quotes: dict[str, Quote],
    *,
    source: QuoteSource,
    settings: MisSettings,
    today: date,
    sleep: Callable[[float], None],
) -> None:
    """今日有成交、但成交價為 "-"（該時點沒有成交）的股票，等待後重新查詢並更新 quotes（8.2）。

    重新查詢失敗時（重試後仍失敗）不中止掃描，沿用第一次的報價，之後改用替代價（D40）。
    """
    for attempt in range(1, settings.missing_price_retry + 1):
        pending = [stock for stock in stocks if (quote := quotes.get(stock.code)) is not None
                   and quote.date == today and quote.open is not None and quote.last is None]
        if not pending:
            return
        logger.info("%d 檔目前沒有成交價，%g 秒後重新查詢（%d/%d）：%s", len(pending), settings.missing_price_wait_sec,
                    attempt, settings.missing_price_retry, "、".join(stock.code for stock in pending))
        sleep(settings.missing_price_wait_sec)
        try:
            quotes.update(source.fetch_quotes(pending))
        except DataSourceError as exc:
            logger.warning("重新查詢成交價失敗，沿用第一次的報價（沒有成交價的改用替代價）：%s", exc)
            return


def _prev_close_matches(quote: Quote, history: Sequence[DailyBar]) -> bool:
    """MIS 參考價與資料庫最後一筆收盤是否一致（7.2）。MIS 沒有參考價時無從比較，視為一致。"""
    if quote.prev_close is None:
        return True
    return bool(history) and normalize(history[-1].close) == normalize(quote.prev_close)


def _tentative_bar(quote: Quote, today: date) -> tuple[DailyBar | None, PriceSubstitute | None]:
    """以 MIS 快照合成今日暫定 K 棒（第 6 節），並回傳現價的替代來源（不是替代值時為 None）。

    沒有成交價時依序改用最佳買價、最佳賣價的第一檔（跌停鎖死時沒有買單，賣價即跌停價，D28）。
    今日尚無成交（開盤價為 "-"），或成交價與買賣價都沒有時，暫定 K 棒為 None（8.2）。
    """
    if quote.open is None or quote.high is None or quote.low is None or quote.volume is None:
        return None, None
    candidates = [(quote.last, None), (quote.best_bid, PriceSubstitute.BEST_BID),
                  (quote.best_ask, PriceSubstitute.BEST_ASK)]
    for price, substitute in candidates:
        if price is not None:
            bar = DailyBar(code=quote.code, date=today, open=quote.open, high=quote.high, low=quote.low,
                           close=price, volume=quote.volume)
            return bar, substitute
    return None, None


def _change_from_reference(price: float, reference: float | None) -> float | None:
    """漲跌幅以 MIS 參考價計算，與看盤軟體一致（第 11 節）。"""
    return None if reference is None else (price - reference) / reference * 100


# --- 回放 -------------------------------------------------------------------

def replay_scan(
    stocks: Sequence[StockInfo],
    *,
    store: PriceStore,
    engine: Engine,
    trend_config: TrendConfig,
    day: date,
) -> ScanReport:
    """以 day 的日 K 當作「今日」評估每一檔；當天沒有日 K 的股票不評估。"""
    hits: list[Hit] = []
    not_evaluated: list[str] = []
    for stock in stocks:
        bars = [bar for bar in store.get_bars(stock.code) if bar.date <= day]
        if not bars or bars[-1].date != day:
            not_evaluated.append(stock.code)
            continue
        ctx = build_context(stock, bars_to_frame(bars), trend_config, is_tentative=False)
        hits += _evaluate(ctx, engine, change_pct=_change_from_previous_close(ctx))

    if len(not_evaluated) == len(stocks):
        raise JobFailed(f"資料庫沒有任何股票 {day} 的日 K：非交易日，或尚未執行 update")
    logger.info("回放 %s：觀察 %d 檔，評估 %d 檔，觸發多方 %d 檔、空方 %d 檔",
                day, len(stocks), len(stocks) - len(not_evaluated), *_side_counts(hits))
    return ScanReport(day=day, is_replay=True, observed=len(stocks), hits=tuple(hits),
                      not_evaluated=tuple(not_evaluated))


def _change_from_previous_close(ctx: StockContext) -> float | None:
    """回放沒有 MIS 參考價，漲跌幅改用前一日收盤計算（第 11 節）。"""
    if ctx.yesterday is None:
        return None
    return float((ctx.today["close"] - ctx.yesterday["close"]) / ctx.yesterday["close"] * 100)


# --- 共用 -------------------------------------------------------------------

def _evaluate(ctx: StockContext, engine: Engine, *, change_pct: float | None,
              price_substitute: PriceSubstitute | None = None) -> list[Hit]:
    """執行規則引擎，依方向整理成 Hit（每個有觸發的方向一筆）。"""
    signals = engine.evaluate(ctx)
    for signal in signals:
        logger.info("%s %s %s｜%s %s：%s", ctx.info.code, ctx.info.name, ctx.trend.label,
                    signal.side.label, signal.rule_name, signal.detail)
    return [
        Hit(ctx=ctx, side=side, signals=side_signals, change_pct=change_pct, price_substitute=price_substitute)
        for side in Side
        if (side_signals := tuple(signal for signal in signals if signal.side is side))
    ]


def _side_counts(hits: Sequence[Hit]) -> tuple[int, int]:
    return sum(hit.side is Side.LONG for hit in hits), sum(hit.side is Side.SHORT for hit in hits)
