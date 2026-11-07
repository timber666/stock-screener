"""盤中 job：合成暫定 K 棒、執行規則引擎並推播（PROJECT_PLAN.md 7.2）。

盤中模式以 MIS 快照合成今日暫定 K 棒，接在資料庫的歷史日 K 之後；回放模式（--replay）以資料庫中
該日（含）以前的日 K 建立 StockContext，該日 K 棒視為「今日」。結果推播到 Discord，--dry-run 時只印在終端。

盤中模式推播成功後，寫入執行紀錄、訊號與報價快照，MIS 原始資料另存壓縮檔（D45、D46）；
--dry-run 與回放不寫入。
"""

import gzip
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from screener.analysis.context import bars_to_frame, build_context
from screener.analysis.market import limit_prices
from screener.analysis.numeric import normalize
from screener.config import AppConfig, MisSettings, TrendConfig, rules_fingerprint
from screener.data.base import DataSourceError, PriceStore, QuoteSource
from screener.data.mis import MisClient
from screener.engine import Engine
from screener.jobs import JobFailed, open_existing_store
from screener.models import (DailyBar, Hit, PriceSubstitute, Quote, ScanReport, ScanRun, Side, SignalRecord,
                             StockContext, StockInfo)
from screener.notify.base import Notifier, NotifyError
from screener.notify.discord import MESSAGE_LIMIT
from screener.notify.message import format_for_terminal, format_messages

logger = logging.getLogger(__name__)


def run(config: AppConfig, *, replay: date | None, notifier: Notifier | None, out: TextIO | None = None,
        now: datetime | None = None) -> None:
    """結果推播到 notifier；notifier 為 None 時（--dry-run）印到 out（預設 stdout）。

    超過單則上限時依族群拆成多則（第 11 節）。now 預設為目前時間，盤中模式以它判斷「今天」。
    盤中模式推播成功後才寫入訊號紀錄（D45）；寫入失敗時已推播的訊息不受影響，job 以失敗結束。
    """
    engine = Engine.from_config(config.rules)
    stocks, trend_config = config.watchlist.stocks, config.rules.trend
    now = now or datetime.now()
    with open_existing_store(config.settings.db_path) as store:
        if replay is not None:
            report = replay_scan(stocks, store=store, engine=engine, trend_config=trend_config, day=replay)
        else:
            report = intraday_scan(stocks, source=MisClient(config.settings.mis), store=store, engine=engine,
                                   trend_config=trend_config, settings=config.settings.mis, now=now)
    if report is None:                          # 非交易日：直接結束，不推播
        return

    messages = format_messages(report, config.watchlist.industries, MESSAGE_LIMIT)
    if notifier is None:
        (out or sys.stdout).write(format_for_terminal(messages))
        return
    for i, message in enumerate(messages, start=1):
        try:
            notifier.send(message)
        except NotifyError as exc:
            raise JobFailed(f"推播失敗（第 {i}/{len(messages)} 則）：{exc}") from exc
    logger.info("已推播 Discord：%d 則", len(messages))
    if not report.is_replay:
        record_scan(config, report, scanned_at=now)


def record_scan(config: AppConfig, report: ScanReport, *, scanned_at: datetime) -> int:
    """寫入執行紀錄、訊號與報價快照（D45），原始資料另存壓縮檔（D46），回傳執行紀錄的 id。

    先寫壓縮檔，再在同一個交易內寫入資料庫，任何一步失敗時資料庫都不會留下不完整的紀錄。
    """
    try:
        raw_file = write_raw_snapshot(config.settings.snapshot_dir, report, scanned_at=scanned_at)
        run = ScanRun(day=report.day, scanned_at=scanned_at, rules_fingerprint=rules_fingerprint(config.rules),
                      observed=report.observed, evaluated=report.observed - len(report.not_evaluated),
                      not_evaluated=len(report.not_evaluated), substituted=len(report.substituted),
                      prev_close_mismatch=len(report.prev_close_mismatch), raw_file=raw_file.as_posix())
        signals = signal_records(report)
        with open_existing_store(config.settings.db_path) as store:
            run_id = store.record_scan(run, signals, report.quotes)
    except Exception as exc:
        logger.exception("訊號紀錄寫入失敗")
        raise JobFailed(f"已推播 Discord，但訊號紀錄寫入失敗（{type(exc).__name__}: {exc}）") from exc
    logger.info("已寫入訊號紀錄 #%d：訊號 %d 個、報價快照 %d 檔，原始資料 %s", run_id, len(signals),
                len(report.quotes), raw_file)
    return run_id


def signal_records(report: ScanReport) -> list[SignalRecord]:
    """推播內容中的每個訊號（一檔股票觸發的一條規則）一筆。"""
    return [
        SignalRecord(code=hit.ctx.info.code, rule_id=signal.rule_id, rule_name=signal.rule_name, side=hit.side,
                     trend=hit.ctx.trend, price=float(hit.ctx.today["close"]), price_substitute=hit.price_substitute,
                     change_pct=hit.change_pct, detail=signal.detail)
        for hit in report.hits for signal in hit.signals
    ]


def write_raw_snapshot(directory: Path, report: ScanReport, *, scanned_at: datetime) -> Path:
    """MIS 回傳的原始資料列（每檔約 40 個欄位）存成 gzip 壓縮的 JSON，檔名為掃描時間（D46）。"""
    path = directory / f"{scanned_at:%Y-%m-%d_%H%M%S}.json.gz"
    content = {"date": report.day.isoformat(), "scanned_at": scanned_at.isoformat(timespec="seconds"),
               "records": [quote.raw for quote in report.quotes if quote.raw is not None]}
    directory.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False)
    return path


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
                          price_substitute=substitute, reference=quote.prev_close)

    if len(not_evaluated) == len(stocks):
        raise JobFailed(f"觀察清單沒有任何股票 {today} 有成交：尚未開盤，或 MIS 資料異常")
    logger.info("盤中掃描 %s：觀察 %d 檔，評估 %d 檔（其中 %d 檔為替代價），昨收不一致 %d 檔，觸發多方 %d 檔、空方 %d 檔",
                now.strftime("%Y-%m-%d %H:%M"), len(stocks), len(stocks) - len(not_evaluated), len(substituted),
                len(mismatched), *_side_counts(hits))
    return ScanReport(day=today, is_replay=False, observed=len(stocks), hits=tuple(hits),
                      not_evaluated=tuple(not_evaluated), scan_time=now.strftime("%H:%M"),
                      prev_close_mismatch=tuple(mismatched), substituted=tuple(substituted),
                      quotes=tuple(quotes.values()))


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
    """今日有成交、但拿不到成交價（MIS 的 z 與 trade.z 都是 "-"）的股票，等待後重新查詢並更新 quotes（8.2）。

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

    沒有成交價時依序改用最佳買價、最佳賣價（跌停鎖死時沒有買單，賣價通常即跌停價，D28）。
    今日尚無成交（開盤價為 "-"），或成交價與買賣價都沒有時，暫定 K 棒為 None（8.2）。
    """
    if quote.open is None or quote.high is None or quote.low is None or quote.volume is None:
        return None, None
    price, substitute = quote.current_price()
    if price is None:
        return None, None
    bar = DailyBar(code=quote.code, date=today, open=quote.open, high=quote.high, low=quote.low, close=price,
                   volume=quote.volume)
    return bar, substitute


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
              price_substitute: PriceSubstitute | None = None, reference: float | None = None) -> list[Hit]:
    """執行規則引擎，依方向整理成 Hit（每個有觸發的方向一筆），並標示是否符合進場（5.5）。

    reference 為 MIS 參考價；回放沒有時，漲跌停價改以前一日收盤計算。
    """
    signals = engine.evaluate(ctx)
    for signal in signals:
        logger.info("%s %s %s｜%s %s：%s", ctx.info.code, ctx.info.name, ctx.trend.label,
                    signal.side.label, signal.rule_name, signal.detail)
    entry_sides = {side for side in engine.entry_sides(ctx, signals) if not _at_price_limit(ctx, side, reference)}
    for side in entry_sides:
        logger.info("%s %s %s｜%s 符合進場", ctx.info.code, ctx.info.name, ctx.trend.label, side.label)
    return [
        Hit(ctx=ctx, side=side, signals=side_signals, change_pct=change_pct, price_substitute=price_substitute,
            entry=side in entry_sides)
        for side in Side
        if (side_signals := tuple(signal for signal in signals if signal.side is side))
    ]


def _at_price_limit(ctx: StockContext, side: Side, reference: float | None) -> bool:
    """現價已在該方向的漲跌停價（多方漲停、空方跌停）：視為不成交，不標示符合進場（5.5）。"""
    up, down = limit_prices(float(ctx.yesterday["close"] if reference is None else reference))
    price = normalize(ctx.today["close"])
    return bool(price >= normalize(up) if side is Side.LONG else price <= normalize(down))


def _side_counts(hits: Sequence[Hit]) -> tuple[int, int]:
    return sum(hit.side is Side.LONG for hit in hits), sum(hit.side is Side.SHORT for hit in hits)
