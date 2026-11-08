"""交易回測：python -m screener trade-backtest（RESEARCH_PLAN.md 4.2）。

照進場（5.5）與出場（5.4）規則模擬一個帳戶，比較檔數上限與挑選方式。預設只跑觀察段的全部組合；
在觀察段選定一組後，加 --validate 只檢查那一組在驗證段的結果（D32）。各段都從初始資金開始。

--universe market 改用全市場的動態股票池（RESEARCH_PLAN.md 4.3）：觀察段從 market.signal_start 起，基準為當月
股票池每天等權，輸出到 market.output_dir 的 trade/；另以相同的組合重跑觀察清單，在兩者都有資料的期間並列。
"""

import logging
import os
import random
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from typing import TextIO

from screener.backtest.signals import StockHistory
from screener.backtest.stats import OBSERVE, VALIDATE
from screener.backtest.trade_report import TradeBacktestResult, format_summary, pick_label, write_csv, write_html
from screener.backtest.trades import (RANDOM, AccountSettings, Candidate, benchmark_series, find_candidates,
                                      simulate_account)
from screener.config import AppConfig, BacktestConfig, ConfigError, ResearchConfig, RulesConfig, TradeConfig
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs.backtest import (MARKET, MARKET_BASELINE, WATCHLIST, load_histories, load_market, market_label,
                                    market_warnings)
from screener.research.market.compare import MARKET as MARKET_LABEL
from screener.research.market.compare import WATCHLIST as WATCHLIST_LABEL
from screener.research.market.compare import format_trade_comparison, trade_comparison

logger = logging.getLogger(__name__)


def run(config: AppConfig, backtest: BacktestConfig, *, rules: RulesConfig, rules_label: str, validate: bool,
        max_positions: int | None, pick: str | None, universe: str = WATCHLIST, research: ResearchConfig | None = None,
        out: TextIO | None = None, workers: int | None = None) -> TradeBacktestResult:
    trade = backtest.trade
    caps, picks = select_runs(trade, validate=validate, max_positions=max_positions, pick=pick)
    engine = Engine.from_config(rules)          # 先檢查規則，錯誤時在讀資料前就結束
    if not engine.entry_rules:
        raise ConfigError(f"{rules_label} 沒有啟用的進場規則（entry_rules），無法做交易回測")
    if not engine.exit_rules:
        raise ConfigError(f"{rules_label} 沒有啟用的出場規則（exit_rules），無法做交易回測")
    if universe == MARKET:
        if research is None:
            raise ValueError("全市場需要 research.yaml 的設定")
        loaded = load_market(research.market)
        histories, warnings = loaded.histories, market_warnings(loaded)
        observe_start, output_root = research.market.signal_start, research.market.output_dir / "trade"
        universe_label, baseline_label = market_label(research.market, loaded), MARKET_BASELINE
    else:
        histories, warnings = load_histories(config)
        observe_start, output_root = backtest.signal_start, trade.output_dir
        universe_label, baseline_label = f"觀察清單 {len(histories)} 檔", "全清單"

    segment, start, end, calendar = _segment(histories, backtest, observe_start, validate=validate)
    logger.info("交易回測（%s）%s ~ %s：%d 檔，規則檔 %s", segment, start, end, len(histories), rules_label)
    candidates = find_all_candidates(histories, rules, start=start, end=end, pick_ma=trade.pick_ma, workers=workers)
    logger.info("進場訊號 %d 個", len(candidates))
    runs = tuple(simulate_account(candidates, calendar, end, _settings(backtest, cap, how))
                 for cap in caps for how in picks)
    randoms = {cap: tuple(simulate_account(candidates, calendar, end, _settings(backtest, cap, RANDOM),
                                           rng=random.Random(trade.random_seed + i))
                          for i in range(trade.random_runs))
               for cap in caps}
    result = TradeBacktestResult(segment=segment, start=calendar[0], end=end, candidates=tuple(candidates), runs=runs,
                                 random_runs=randoms,
                                 benchmark=benchmark_series(histories, calendar, trade.initial_capital))

    output_dir = output_root / ("validate" if validate else "observe")
    write_csv(result, trade, output_dir)
    write_html(result, trade, output_dir / "report.html", baseline_label=baseline_label)
    text = format_summary(result, trade, backtest.cost, universe_label=universe_label, rules_label=rules_label,
                          output_dir=output_dir, warnings=warnings, baseline_label=baseline_label)
    if universe == MARKET:
        text += "\n\n" + _compare_watchlist(config, backtest, rules, candidates, calendar, histories, caps, picks,
                                             start=max(research.market.compare_start, start), end=end,
                                             output_dir=output_dir, workers=workers)
    (out or sys.stdout).write(text + "\n")
    return result


def _segment(histories: Sequence[StockHistory], backtest: BacktestConfig, observe_start: date, *,
             validate: bool) -> tuple[str, date, date, list[date]]:
    """(段, 第一天, 最後一個可以買進的交易日, 交易日曆)；段尾之後的交易日留在日曆上，持有中的部位照規則賣完。"""
    days = sorted({day for h in histories for day in h.bars.index})
    if validate:
        segment, start, end = VALIDATE, backtest.validation_start, days[-1]
    else:
        segment, start = OBSERVE, observe_start
        end = max((day for day in days if day < backtest.validation_start), default=start)
    calendar = [day for day in days if day >= start]
    if not calendar or end < calendar[0]:
        raise JobFailed(f"{segment}（{start} 起）沒有任何交易日的日 K")
    return segment, start, end, calendar


def _settings(backtest: BacktestConfig, cap: int, how: str) -> AccountSettings:
    trade = backtest.trade
    return AccountSettings(initial_capital=trade.initial_capital, max_positions=cap, pick=how, cost=backtest.cost,
                           min_commission=trade.min_commission)


def _compare_watchlist(config: AppConfig, backtest: BacktestConfig, rules: RulesConfig,
                       candidates: Sequence[Candidate], calendar: Sequence[date], histories: Sequence[StockHistory],
                       caps: Sequence[int], picks: Sequence[str], *, start: date, end: date, output_dir: Path,
                       workers: int | None) -> str:
    """相同的檔數上限與挑選方式，在兩者都有資料的期間各自從初始資金開始（隨機挑選只在主要結果列出）。"""
    watchlist, _ = load_histories(config)
    end = min(end, max(h.bars.index[-1] for h in watchlist))
    path = output_dir / "comparison.csv"
    path.unlink(missing_ok=True)
    if start > end:
        return f"■ 與觀察清單並列：{start} 之後沒有兩者都有資料的期間，略過"
    market_calendar = [d for d in calendar if d >= start]
    market_candidates = [c for c in candidates if start <= c.entry_day <= end]
    watch_calendar = sorted({day for h in watchlist for day in h.bars.index if day >= start})
    logger.info("對照：觀察清單 %d 檔，%s ~ %s", len(watchlist), start, end)
    watch_candidates = find_all_candidates(watchlist, rules, start=start, end=end, pick_ma=backtest.trade.pick_ma,
                                           workers=workers)
    market_bench = benchmark_series(histories, market_calendar, backtest.trade.initial_capital)
    watch_bench = benchmark_series(watchlist, watch_calendar, backtest.trade.initial_capital)
    runs = []
    for cap in caps:
        for how in picks:
            settings = _settings(backtest, cap, how)
            runs.append((MARKET_LABEL, cap, how, simulate_account(market_candidates, market_calendar, end, settings),
                         market_bench))
            runs.append((WATCHLIST_LABEL, cap, how, simulate_account(watch_candidates, watch_calendar, end, settings),
                         watch_bench))
    table = trade_comparison(runs)
    table.round(4).to_csv(path, index=False, encoding="utf-8-sig")
    labels = {how: pick_label(how, backtest.trade) for how in picks}
    return f"{format_trade_comparison(table, labels, start=market_calendar[0], end=end)}\n  明細：{path}"


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
