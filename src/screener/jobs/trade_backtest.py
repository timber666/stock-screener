"""交易回測：python -m screener trade-backtest（RESEARCH_PLAN.md 4.2）。

照進場（5.5）與出場（5.4）規則模擬一個帳戶，比較檔數上限與挑選方式。預設只跑觀察段的全部組合；
在觀察段選定一組後，加 --validate 只檢查那一組在驗證段的結果（D32）。各段都從初始資金開始。
"""

import logging
import os
import random
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from typing import TextIO

from screener.backtest.signals import StockHistory
from screener.backtest.stats import OBSERVE, VALIDATE
from screener.backtest.trade_report import TradeBacktestResult, format_summary, write_csv, write_html
from screener.backtest.trades import (RANDOM, AccountSettings, Candidate, benchmark_series, find_candidates,
                                      simulate_account)
from screener.config import AppConfig, BacktestConfig, ConfigError, RulesConfig, TradeConfig
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs.backtest import load_histories

logger = logging.getLogger(__name__)


def run(config: AppConfig, backtest: BacktestConfig, *, rules: RulesConfig, rules_label: str, validate: bool,
        max_positions: int | None, pick: str | None, out: TextIO | None = None,
        workers: int | None = None) -> TradeBacktestResult:
    trade = backtest.trade
    caps, picks = select_runs(trade, validate=validate, max_positions=max_positions, pick=pick)
    engine = Engine.from_config(rules)          # 先檢查規則，錯誤時在讀資料前就結束
    if not engine.entry_rules:
        raise ConfigError(f"{rules_label} 沒有啟用的進場規則（entry_rules），無法做交易回測")
    if not engine.exit_rules:
        raise ConfigError(f"{rules_label} 沒有啟用的出場規則（exit_rules），無法做交易回測")
    histories, warnings = load_histories(config)

    days = sorted({day for h in histories for day in h.bars.index})
    if validate:
        segment, start, end = VALIDATE, backtest.validation_start, days[-1]
    else:
        segment, start = OBSERVE, backtest.signal_start
        end = max((day for day in days if day < backtest.validation_start), default=start)
    calendar = [day for day in days if day >= start]
    if not calendar or end < calendar[0]:
        raise JobFailed(f"{segment}（{start} 起）沒有任何交易日的日 K")
    logger.info("交易回測（%s）%s ~ %s：%d 檔，規則檔 %s", segment, start, end, len(histories), rules_label)
    candidates = find_all_candidates(histories, rules, start=start, end=end, pick_ma=trade.pick_ma, workers=workers)
    logger.info("進場訊號 %d 個", len(candidates))

    def settings(cap: int, how: str) -> AccountSettings:
        return AccountSettings(initial_capital=trade.initial_capital, max_positions=cap, pick=how, cost=backtest.cost,
                               min_commission=trade.min_commission)

    runs = tuple(simulate_account(candidates, calendar, end, settings(cap, how)) for cap in caps for how in picks)
    randoms = {cap: tuple(simulate_account(candidates, calendar, end, settings(cap, RANDOM),
                                           rng=random.Random(trade.random_seed + i))
                          for i in range(trade.random_runs))
               for cap in caps}
    result = TradeBacktestResult(segment=segment, start=calendar[0], end=end, candidates=tuple(candidates), runs=runs,
                                 random_runs=randoms,
                                 benchmark=benchmark_series(histories, calendar, trade.initial_capital))

    output_dir = trade.output_dir / ("validate" if validate else "observe")
    write_csv(result, trade, output_dir)
    write_html(result, trade, output_dir / "report.html")
    (out or sys.stdout).write(format_summary(result, trade, backtest.cost, observed=len(histories),
                                             rules_label=rules_label, output_dir=output_dir,
                                             warnings=warnings) + "\n")
    return result


def select_runs(trade: TradeConfig, *, validate: bool, max_positions: int | None,
                pick: str | None) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """觀察段跑設定檔的全部組合；驗證段只跑指定的一組，而且必須是觀察段比較過的組合（D32）。"""
    if not validate:
        if max_positions is not None or pick is not None:
            raise ConfigError("--max-positions、--pick 只能和 --validate 一起使用：觀察段會跑 backtest.yaml 的全部組合")
        return trade.max_positions, trade.picks
    if max_positions is None or pick is None:
        raise ConfigError("--validate 需要同時指定 --max-positions 與 --pick：驗證段只檢查在觀察段選定的一組（D32）")
    if max_positions not in trade.max_positions:
        raise ConfigError(f"--max-positions {max_positions}：應為 backtest.yaml 的 max_positions 其中之一 "
                          f"{list(trade.max_positions)}")
    if pick not in trade.picks:
        raise ConfigError(f"--pick {pick}：應為 backtest.yaml 的 picks 其中之一 {list(trade.picks)}")
    return (max_positions,), (pick,)


def find_all_candidates(histories: Sequence[StockHistory], rules: RulesConfig, *, start: date, end: date,
                        pick_ma: int, workers: int | None = None) -> list[Candidate]:
    """各股票的進場訊號互不相關，以多個行程平行計算；結果依日期與代號排序。"""
    tasks = [(h, rules, start, end, pick_ma) for h in histories]
    workers = workers or min(os.cpu_count() or 1, len(tasks))
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(_candidates_of, tasks, chunksize=4))
    else:
        outputs = [_candidates_of(task) for task in tasks]
    return sorted((c for output in outputs for c in output), key=lambda c: (c.entry_day, c.code))


def _candidates_of(task: tuple[StockHistory, RulesConfig, date, date, int]) -> list[Candidate]:
    """一檔股票的進場訊號。在子行程中執行。"""
    history, rules, start, end, pick_ma = task
    return find_candidates(history, Engine.from_config(rules), rules.trend, start, end, pick_ma)
