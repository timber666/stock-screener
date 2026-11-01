"""盤後 job：向 FinMind 補抓日 K 並寫入 SQLite（PROJECT_PLAN.md 7.1）。"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from screener.config import AppConfig, Secrets
from screener.data.base import DailyPriceSource, DataSourceError, PriceStore
from screener.data.db import SqlitePriceStore
from screener.data.finmind import FinMindClient
from screener.jobs import JobFailed
from screener.models import StockInfo

logger = logging.getLogger(__name__)


@dataclass
class UpdateSummary:
    rows: int = 0                                           # 寫入筆數
    updated: int = 0                                        # 有寫入資料的檔數
    up_to_date: int = 0                                     # 已是最新、不需查詢的檔數
    backfilled: list[str] = field(default_factory=list)     # 首次回補的代號
    extended: list[str] = field(default_factory=list)       # 往前回補（--since）的代號
    empty_backfill: list[str] = field(default_factory=list) # 回補期間查無資料的代號


def run(config: AppConfig, secrets: Secrets, *, since: date | None = None) -> None:
    """since：把資料庫往前回補到這一天（PROJECT_PLAN.md 14.1）；None 時只補抓最後一筆之後的資料。"""
    if not secrets.finmind_token:
        logger.warning("未設定 FINMIND_TOKEN，以未登入身分查詢（請求上限較低）")
    source = FinMindClient(config.settings.finmind, secrets.finmind_token)
    with SqlitePriceStore(config.settings.db_path) as store:
        update_prices(
            config.watchlist.stocks,
            source=source,
            store=store,
            today=date.today(),
            backfill_days=config.settings.backfill_days,
            since=since,
        )


def update_prices(
    stocks: Sequence[StockInfo],
    *,
    source: DailyPriceSource,
    store: PriceStore,
    today: date,
    backfill_days: int,
    since: date | None = None,
) -> UpdateSummary:
    """逐檔從資料庫最後一筆的隔天補抓到今天；資料庫沒有資料的股票回補 backfill_days 天。

    指定 since 時，第一筆晚於 since 的股票改從 since 抓到今天（往前回補），一次請求涵蓋整段，
    已有的日子以相同資料覆寫；沒有資料的股票也從 since 開始回補。上市晚於 since 的股票第一筆
    永遠晚於 since，每次指定 since 都會再查一次。

    每檔抓完立即寫入，因此中途失敗時已完成的部分會保留，重新執行會從中斷處繼續。
    """
    summary = UpdateSummary()
    total = len(stocks)
    logger.info("開始更新日 K：%d 檔，抓取到 %s", total, today)
    for i, stock in enumerate(stocks, start=1):
        progress = f"[{i}/{total}] {stock.code} {stock.name}"
        last = store.latest_date(stock.code)
        is_backfill = last is None
        earliest = store.earliest_date(stock.code) if since is not None and not is_backfill else None
        is_extension = earliest is not None and since is not None and since < earliest
        if is_backfill:
            start = since if since is not None else today - timedelta(days=backfill_days)
        elif is_extension:
            start = since
        else:
            start = last + timedelta(days=1)
        if start > today:
            summary.up_to_date += 1
            logger.debug("%s：已是最新（%s）", progress, last)
            continue

        try:
            bars = source.fetch_daily_prices(stock.code, start, today)
        except DataSourceError as exc:
            _log_summary(summary)
            raise JobFailed(
                f"{progress} 抓取失敗：{exc}。剩餘 {total - i + 1} 檔未更新，重新執行會從中斷處繼續"
            ) from exc

        written = store.upsert_bars(bars)
        summary.rows += written
        if written:
            summary.updated += 1
        if is_backfill:
            summary.backfilled.append(stock.code)
            if not bars:
                summary.empty_backfill.append(stock.code)
        if is_extension:
            summary.extended.append(stock.code)
        kind = "回補" if is_backfill else "往前回補" if is_extension else "補抓"
        logger.info("%s：%s %s ~ %s，寫入 %d 筆", progress, kind, start, today, written)

    _log_summary(summary)
    return summary


def _log_summary(summary: UpdateSummary) -> None:
    logger.info(
        "日 K 更新結果：%d 檔寫入 %d 筆（其中首次回補 %d 檔、往前回補 %d 檔），%d 檔已是最新",
        summary.updated, summary.rows, len(summary.backfilled), len(summary.extended), summary.up_to_date,
    )
    if summary.empty_backfill:
        logger.warning(
            "%d 檔回補期間查無任何資料，請確認代號是否正確：%s",
            len(summary.empty_backfill), "、".join(summary.empty_backfill),
        )
