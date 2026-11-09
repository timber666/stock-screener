"""組合回測：python -m screener portfolio-backtest（RESEARCH_PLAN.md 4.6）。

每月依因子換股，模擬 30 萬元的帳戶。預設只跑觀察段的全部組合；在觀察段選定一組後，加 --validate 只檢查那一組在
驗證段的結果（D32）。各段都從初始資金開始。讀取 market.db，不連網、不推播。
"""

import logging
import random
import sys
from dataclasses import dataclass
from datetime import date
from typing import TextIO

import numpy as np
import pandas as pd

from screener.backtest.signals import StockHistory
from screener.backtest.stats import OBSERVE, VALIDATE
from screener.backtest.trades import AccountSettings, benchmark_series
from screener.config import AppConfig, BacktestConfig, ConfigError, MarketConfig, PortfolioConfig, ResearchConfig
from screener.jobs import JobFailed
from screener.research.factors import Factor, FactorContext, build_factors
from screener.research.factors.panel import Panel, build_panel
from screener.research.market.history import load_market_histories, value_table
from screener.research.market.store import SqliteMarketStore
from screener.research.portfolio import (EXECUTIONS, OPEN, PriceTable, RebalanceRanking, account_metrics, build_rankings,
                                         index_series, random_plan, rebalance_schedule, simulate_account)
from screener.research.portfolio_output import (CombinationRun, PortfolioBacktestResult, RandomRuns, format_summary,
                                                write_csv, write_html)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioData:
    panel: Panel
    context: FactorContext
    return_index: dict[date, float]
    delisted: frozenset[str]        # 有下市紀錄的股票
    covered_through: date           # market.db 查詢過的最後一天（含休市日）
    names: dict[str, str]


def load_data(config: AppConfig, market: MarketConfig) -> tuple[PortfolioData, list[StockHistory]]:
    """(資料, 歷史資料)。歷史資料只用來算基準②，算完就可以釋放（全市場約 1 GB）。"""
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")
    logger.info("讀取全市場資料")
    with SqliteMarketStore(market.db_path) as store:
        loaded = load_market_histories(store, market.universe)
        values = value_table(store)
        revenue, valuations, shares = store.revenue_frame(), store.valuation_frame(), store.shares_frame()
        return_index = store.return_index()
        delisted = frozenset(s.code for s in store.securities() if s.delisted_on is not None)
        covered = store.covered_through()
    if covered is None:
        raise JobFailed("market.db 沒有抓取紀錄，請先執行 market-update")
    panel = build_panel(loaded.histories, values)
    context = FactorContext(panel=panel, trend=config.rules.trend, market=market, revenue=revenue, valuations=valuations,
                            shares=shares)
    names = {history.info.code: history.info.name for history in loaded.histories}
    logger.info("股票 %d 檔、交易日 %d 天", len(panel.codes), len(panel.days))
    return PortfolioData(panel, context, return_index, delisted, covered, names), loaded.histories


def settings_for(portfolio: PortfolioConfig, backtest: BacktestConfig, top_n: int, pick: str) -> AccountSettings:
    return AccountSettings(initial_capital=portfolio.initial_capital, max_positions=top_n, pick=pick, cost=backtest.cost,
                           min_commission=backtest.trade.min_commission)


def factor_by_id(research: ResearchConfig) -> dict[str, Factor]:
    return {factor.id: factor for factor in build_factors(research.factors.definitions)}


def run(config: AppConfig, research: ResearchConfig, backtest: BacktestConfig, *, validate: bool, factor: str | None,
        top_n: int | None, pick: str | None, out: TextIO | None = None) -> PortfolioBacktestResult:
    portfolio = research.portfolio
    factor_ids, top_ns, picks = select_runs(portfolio, validate=validate, factor=factor, top_n=top_n, pick=pick)
    factors = factor_by_id(research)
    data, histories = load_data(config, research.market)
    days = list(data.panel.days)
    segment, start, end = _segment(days, backtest, research.market, validate=validate)
    schedule = rebalance_schedule(days, start=start, end=end, available_day=research.market.revenue_available_day,
                                  covered_through=data.covered_through)
    if not schedule:
        raise JobFailed(f"{segment}（{start} ~ {end}）沒有任何換股日")
    calendar = [day for day in days if schedule[0].signal <= day <= end]
    logger.info("組合回測（%s）%s ~ %s：換股 %d 次、%d 個因子", segment, calendar[0], end, len(schedule), len(factor_ids))
    universe = benchmark_series(histories, calendar, portfolio.initial_capital)
    del histories
    index = index_series(data.return_index, calendar, portfolio.initial_capital)
    prices = PriceTable.from_panel(data.panel)
    liquidity = factors[portfolio.liquidity_factor].compute(data.context)

    runs: list[CombinationRun] = []
    randoms: dict[tuple[str, int], RandomRuns] = {}
    for factor_id in factor_ids:
        logger.info("因子 %s", factor_id)
        rankings = build_rankings(data.panel, factors[factor_id].compute(data.context), liquidity, schedule,
                                  quantiles=research.factors.quantiles, min_stocks=research.factors.min_stocks)
        if skipped := sum(not item.by_factor for item in rankings):
            logger.warning("%s：%d 次換股因子值不足 %d 檔，當月不換股", factor_id, skipped, research.factors.min_stocks)
        for n in top_ns:
            for how in picks:
                settings = settings_for(portfolio, backtest, n, how)
                plan = [(item.rebalance, item.ranking(how)) for item in rankings]
                for execution in EXECUTIONS:
                    result = simulate_account(prices, plan, settings, execution, start=calendar[0], end=end,
                                              delisted=data.delisted)
                    runs.append(CombinationRun(factor_id, n, how, result))
            randoms[(factor_id, n)] = _random_runs(prices, rankings, portfolio, backtest, factor_id, n, calendar[0], end,
                                                   data.delisted, index, universe)

    result = PortfolioBacktestResult(segment=segment, start=calendar[0], end=end, rebalances=len(schedule), runs=tuple(runs),
                                     randoms=randoms, index=index, universe=universe)
    output_dir = portfolio.output_dir / ("validate" if validate else "observe")
    write_csv(result, data.names, output_dir)
    write_html(result, portfolio, output_dir / "report.html")
    (out or sys.stdout).write(format_summary(result, portfolio, backtest.cost, backtest.trade.min_commission, output_dir) + "\n")
    logger.info("組合回測完成：輸出在 %s", output_dir)
    return result


def _random_runs(prices: PriceTable, rankings: list[RebalanceRanking], portfolio: PortfolioConfig, backtest: BacktestConfig,
                 factor: str, top_n: int, start: date, end: date, delisted: frozenset[str], index: pd.Series,
                 universe: pd.Series) -> RandomRuns:
    """基準③：最高組隨機 N 檔，隔日開盤成交，遞補、順延與成本規則與組合相同（R35）。"""
    settings = settings_for(portfolio, backtest, top_n, "random")
    equities, metrics = [], []
    for i in range(portfolio.random_runs):
        plan = random_plan(rankings, random.Random(portfolio.random_seed + i))
        run = simulate_account(prices, plan, settings, OPEN, start=start, end=end, delisted=delisted)
        equities.append(run.cash + run.holdings)
        metrics.append(account_metrics(run, index, universe))
    size = len(prices.days[prices.days.index(start):prices.days.index(end) + 1])
    return RandomRuns(factor, top_n, np.array(equities) if equities else np.empty((0, size)), pd.DataFrame(metrics))


def _segment(days: list[date], backtest: BacktestConfig, market: MarketConfig, *, validate: bool) -> tuple[str, date, date]:
    """(段, 訊號日的起點, 最後一個交易日)。"""
    if validate:
        return VALIDATE, backtest.validation_start, days[-1]
    return OBSERVE, market.signal_start, max(day for day in days if day < backtest.validation_start)


def select_runs(portfolio: PortfolioConfig, *, validate: bool, factor: str | None, top_n: int | None,
                pick: str | None) -> tuple[tuple[str, ...], tuple[int, ...], tuple[str, ...]]:
    """觀察段跑設定檔的全部組合；驗證段只跑指定的一組，而且必須是觀察段比較過的組合（D32）。"""
    if not validate:
        if factor is not None or top_n is not None or pick is not None:
            raise ConfigError("--factor、--top-n、--pick 只能和 --validate 一起使用：觀察段會跑 research.yaml 的全部組合")
        return portfolio.factors, portfolio.top_n, portfolio.picks
    if factor is None or top_n is None or pick is None:
        raise ConfigError("--validate 需要同時指定 --factor、--top-n 與 --pick：驗證段只檢查在觀察段選定的一組（D32）")
    if factor not in portfolio.factors:
        raise ConfigError(f"--factor {factor}：應為 research.yaml 的 portfolio.factors 其中之一 {list(portfolio.factors)}")
    if top_n not in portfolio.top_n:
        raise ConfigError(f"--top-n {top_n}：應為 research.yaml 的 portfolio.top_n 其中之一 {list(portfolio.top_n)}")
    if pick not in portfolio.picks:
        raise ConfigError(f"--pick {pick}：應為 research.yaml 的 portfolio.picks 其中之一 {list(portfolio.picks)}")
    return (factor,), (top_n,), (pick,)

