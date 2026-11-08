"""單因子研究：python -m screener factor-report（RESEARCH_PLAN.md 4.4、4.5）。

讀取 market.db（唯讀用途，研究不寫入 screener.db，PROJECT_PLAN.md 10.4 規則 3），不連網、不推播。
"""

import logging
import sys
from collections.abc import Sequence
from typing import TextIO

from screener.config import AppConfig, BacktestConfig, ConfigError, ResearchConfig
from screener.jobs import JobFailed
from screener.research.factor_output import format_summary, write_csv, write_html
from screener.research.factor_study import StudyResult, run_study
from screener.research.factors import FactorContext, build_factors
from screener.research.factors.fundamental import industry_codes
from screener.research.factors.panel import build_panel
from screener.research.market.history import load_market_histories, value_table
from screener.research.market.store import SqliteMarketStore

logger = logging.getLogger(__name__)


def run(config: AppConfig, research: ResearchConfig, backtest: BacktestConfig, *, factor_ids: Sequence[str] | None = None,
        out: TextIO | None = None) -> StudyResult:
    market = research.market
    factors_config = research.factors
    factors = build_factors(factors_config.definitions)
    if factor_ids is not None:
        unknown = sorted(set(factor_ids) - {factor.id for factor in factors})
        if unknown:
            raise ConfigError(f"--factors：research.yaml 沒有這些因子：{'、'.join(unknown)}")
        factors = [factor for factor in factors if factor.id in factor_ids]
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")

    logger.info("讀取全市場資料")
    with SqliteMarketStore(market.db_path) as store:
        loaded = load_market_histories(store, market.universe)
        values = value_table(store)
        revenue, valuations, shares = store.revenue_frame(), store.valuation_frame(), store.shares_frame()
    panel = build_panel(loaded.histories, values)
    del loaded, values
    context = FactorContext(panel=panel, trend=config.rules.trend, market=market, revenue=revenue,
                            valuations=valuations, shares=shares)
    if revenue.empty:
        raise JobFailed("market.db 沒有月營收（產業別的來源），請先執行 market-update")
    industries, names = industry_codes(revenue, panel, market)
    logger.info("股票 %d 檔、交易日 %d 天、產業別 %d 類、因子 %d 個", len(panel.codes), len(panel.days), len(names), len(factors))

    result = run_study(context, factors, factors_config, industries, start=market.signal_start,
                       validation_start=backtest.validation_start, round_trip_pct=backtest.cost.round_trip_pct)
    output_dir = factors_config.output_dir
    write_csv(result, factors_config, output_dir)
    write_html(result, factors_config, backtest.validation_start, output_dir / "report.html")
    (out or sys.stdout).write(format_summary(result, factors_config, backtest.validation_start, output_dir) + "\n")
    logger.info("單因子研究完成：輸出在 %s", output_dir)
    return result
