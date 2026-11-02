"""訊號成績單：python -m screener signal-report（RESEARCH_PLAN.md 4.1）。

研究指令：讀取 scan 記錄的訊號與報價快照，推播到研究頻道（D43），並輸出明細 CSV。
排程每週六 10:00 執行，產生上一週的成績單（R3）。
"""

import logging
import sys
from datetime import date
from typing import TextIO

from screener.config import AppConfig, ResearchConfig, rules_fingerprint
from screener.engine import Engine
from screener.jobs import JobFailed, open_existing_store
from screener.notify.base import Notifier, NotifyError
from screener.notify.discord import MESSAGE_LIMIT
from screener.notify.message import format_for_terminal
from screener.research.signal_report import SignalReport, build_report, read_m6_validation, report_period
from screener.research.signal_report_output import format_messages, write_csv

logger = logging.getLogger(__name__)


def run(config: AppConfig, research: ResearchConfig, *, end: date | None, notifier: Notifier | None,
        out: TextIO | None = None, today: date | None = None) -> SignalReport:
    """期間為 end 所在那一週的週一到 end（未指定時為上一週）。結果推播到 notifier；notifier 為 None 時
    （--dry-run）印到 out（預設 stdout）。兩者都會寫出明細 CSV。"""
    settings = research.signal_report
    period = report_period(end, today or date.today())
    engine = Engine.from_config(config.rules)
    with open_existing_store(config.settings.db_path) as store:
        report = build_report(store, config.watchlist.stocks, engine=engine, trend_config=config.rules.trend,
                              fingerprint=rules_fingerprint(config.rules), period=period, horizons=settings.horizons)

    csv_path = settings.output_dir / f"{period.end}.csv"
    write_csv(report, csv_path)
    messages = format_messages(report, m6=read_m6_validation(settings.backtest_summary, settings.horizons),
                               top_n=settings.top_n, csv_path=csv_path, limit=MESSAGE_LIMIT)
    logger.info("明細寫入 %s", csv_path)
    if notifier is None:
        (out or sys.stdout).write(format_for_terminal(messages))
        return report
    for i, message in enumerate(messages, start=1):
        try:
            notifier.send(message)
        except NotifyError as exc:
            raise JobFailed(f"推播失敗（第 {i}/{len(messages)} 則）：{exc}") from exc
    logger.info("已推播研究頻道：%d 則", len(messages))
    return report
