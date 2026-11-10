"""營收年增的因子快照：計算（RESEARCH_PLAN.md 4.7、PROJECT_PLAN.md D57）。

全部上市櫃普通股（M8 的代號規則、沒有下市紀錄）中，資料日依 R26 可用的最新一個月有營收、去年當月營收為正數的
公司，計算營收年增（與 M9 的 rev_yoy 相同）並由高到低排名（同值同名次）。還沒有最新月份的公司（晚公告）不列入（R42）。
不需要全市場的面板：factors.fundamental.month_table 只保留曾入選股票池的股票。
"""

from collections.abc import Collection, Sequence
from datetime import date, timedelta

import pandas as pd

from screener.config import MarketConfig
from screener.jobs import JobFailed
from screener.models import FactorSnapshot
from screener.research.factors.fundamental import revenue_available_on
from screener.research.market.common import is_common_stock


def latest_month(months: Collection[str], days: Sequence[date], market: MarketConfig) -> date | None:
    """資料的最後一個交易日已經可以用的最新月份（R26）；一個都沒有時為 None。"""
    index = pd.Index(sorted(days))
    usable = [month for month in (date.fromisoformat(m) for m in months)
              if revenue_available_on(month, index, market) is not None]
    return max(usable, default=None)


def build_snapshot(revenue: pd.DataFrame, *, delisted: Collection[str], days: Sequence[date], market: MarketConfig,
                   snapshot_date: date) -> FactorSnapshot:
    """revenue：market.db 的 monthly_revenue（`SqliteMarketStore.revenue_frame`）；days：market.db 的交易日。"""
    if revenue.empty:
        raise JobFailed("market.db 沒有月營收，請先執行 market-update")
    month = latest_month(revenue["month"].unique(), days, market)
    if month is None:
        raise JobFailed("market.db 還沒有任何一個月的營收到了可用日（R26）")
    rows = revenue[revenue["month"] == month.isoformat()]
    rows = rows[rows["code"].map(is_common_stock) & ~rows["code"].isin(set(delisted)) & (rows["last_year"] > 0)]
    if rows.empty:
        raise JobFailed(f"{month:%Y-%m} 沒有任何普通股的營收年增")
    yoy = rows["revenue"].to_numpy() / rows["last_year"].to_numpy() - 1
    ranks = pd.Series(yoy).rank(ascending=False, method="min").astype(int).to_numpy()
    values = {code: (float(value), int(rank)) for code, value, rank in zip(rows["code"], yoy, ranks, strict=True)}
    return FactorSnapshot(month=month, snapshot_date=snapshot_date, count=len(values), values=values)


def require_fresh(covered: date, today: date) -> None:
    """market.db 查詢過的最後一天（含休市日）要到 today 之前最近的週五：避免開機補跑時比 market-update 先跑，
    用舊資料產生日期是新的快照（R42）。"""
    friday = today - timedelta(days=(today.weekday() - 4) % 7 or 7)
    if covered < friday:
        raise JobFailed(f"market.db 只更新到 {covered}，需要到 {friday}；請先執行 market-update，再重新執行 factor-snapshot")
