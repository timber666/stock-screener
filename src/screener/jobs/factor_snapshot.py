"""因子快照：python -m screener factor-snapshot（RESEARCH_PLAN.md 4.7、PROJECT_PLAN.md D57）。

排程每週六 09:45 執行（09:00 的 market-update 之後）。由 market.db 的月營收計算全部上市櫃普通股的營收年增與排名，
寫到 settings.yaml 的 factor_snapshot.path，供每日的 scan 唯讀。成功時不推播；失敗時錯誤通知推到研究頻道。
"""

import logging
import sys
from datetime import date
from typing import TextIO

from screener.config import AppConfig, ResearchConfig
from screener.data.factor_snapshot import write_snapshot
from screener.jobs import JobFailed
from screener.models import FactorSnapshot
from screener.research.factor_snapshot import build_snapshot, require_fresh
from screener.research.market.store import SqliteMarketStore

logger = logging.getLogger(__name__)


def run(config: AppConfig, research: ResearchConfig, *, dry_run: bool, out: TextIO | None = None,
        today: date | None = None) -> FactorSnapshot:
    """--dry-run 時只印摘要與觀察清單的值，不寫檔。"""
    market = research.market
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")
    today = today or date.today()
    with SqliteMarketStore(market.db_path) as store:
        days, covered = store.trading_days(), store.covered_through()
        revenue = store.revenue_frame()
        delisted = {security.code for security in store.securities() if security.delisted_on is not None}
    if not days or covered is None:
        raise JobFailed("market.db 沒有任何行情，請先執行 market-update")
    require_fresh(covered, today)
    snapshot = build_snapshot(revenue, delisted=delisted, days=days, market=market, snapshot_date=today)
    path = config.settings.factor_snapshot.path
    watched = [stock for stock in config.watchlist.stocks if stock.code in snapshot.values]
    summary = (f"因子快照 {snapshot.snapshot_date}：{snapshot.month:%Y-%m} 月營收，{snapshot.count} 檔上市櫃普通股"
               f"（資料到 {days[-1]}），觀察清單 {len(watched)}/{len(config.watchlist.stocks)} 檔有值")
    logger.info("%s", summary)
    lines = [summary]
    if dry_run:
        lines += [f"  {stock.code} {stock.name}  營收年增 {_format(snapshot, stock.code)}"
                  for stock in sorted(watched, key=lambda s: (snapshot.values[s.code][1], s.code))]
        lines.append("（--dry-run：沒有寫檔）")
    else:
        write_snapshot(path, snapshot)
        logger.info("已寫入 %s", path)
        lines.append(f"已寫入 {path}")
    (out or sys.stdout).write("\n".join(lines) + "\n")
    return snapshot


def _format(snapshot: FactorSnapshot, code: str) -> str:
    value, rank = snapshot.values[code]
    return f"{value * 100:+.2f}%（第 {rank} 名，前 {snapshot.percentile(rank)}%）"
