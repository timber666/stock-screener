"""模擬組合：python -m screener portfolio-track（RESEARCH_PLAN.md 4.6、R36）。

排程每週六 09:30 執行（09:00 的 market-update 之後）。不是換股週，或這個訊號日的名單已經產生，就直接結束，不推播。
換股週：以 market.db 的實際開盤與收盤結算上一次的名單，產生新名單，推播 1 則到研究頻道（D43）。不下單。
"""

import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from screener.backtest.trades import benchmark_series
from screener.config import AppConfig, BacktestConfig, ConfigError, MarketConfig, PaperPortfolio, ResearchConfig
from screener.jobs import JobFailed
from screener.jobs.portfolio_backtest import factor_by_id, load_data, settings_for
from screener.notify.base import Notifier, NotifyError
from screener.notify.discord import MESSAGE_LIMIT
from screener.notify.message import format_for_terminal
from screener.research.market.store import SqliteMarketStore
from screener.research.portfolio import (OPEN, PriceTable, Rebalance, build_rankings, index_series, new_list,
                                         rebalance_schedule, simulate_account)
from screener.research.portfolio_output import TrackReport, format_track_messages, write_track_csv
from screener.research.portfolio_store import PaperStore, SavedRebalance

logger = logging.getLogger(__name__)


def run(config: AppConfig, research: ResearchConfig, backtest: BacktestConfig, *, notifier: Notifier | None,
        out: TextIO | None = None, today: date | None = None) -> TrackReport | None:
    """沒有新名單時回傳 None。結果推播到 notifier；notifier 為 None 時（--dry-run）印到 out，不寫入任何檔案。"""
    portfolio, market = research.portfolio, research.market
    track = portfolio.track
    if track is None:
        raise ConfigError("research.yaml > portfolio > track 尚未選定：看完 portfolio-backtest 後填入要追蹤的一組")
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")
    today = today or date.today()
    with SqliteMarketStore(market.db_path) as store:
        days, covered = store.trading_days(), store.covered_through()
    if not days or covered is None:
        raise JobFailed("market.db 沒有任何行情，請先執行 market-update")
    _require_fresh(covered, today, market)
    schedule = rebalance_schedule(days, start=market.signal_start, end=days[-1],
                                  available_day=market.revenue_available_day, covered_through=covered)
    saved = _saved_rebalances(portfolio.paper_db_path)
    _check_same_track(saved, track)
    new = pending_rebalances(schedule, saved, today)
    if not new:
        logger.info("模擬組合：不是換股週，或這個訊號日的名單已經產生，結束")
        return None

    factors = factor_by_id(research)
    data, histories = load_data(config, market)
    rankings = build_rankings(data.panel, factors[track.factor].compute(data.context),
                              factors[portfolio.liquidity_factor].compute(data.context), new,
                              quantiles=research.factors.quantiles, min_stocks=research.factors.min_stocks)
    by_signal = {rebalance.signal: rebalance for rebalance in schedule}
    plan: list[tuple[Rebalance, tuple[str, ...]]] = [(by_signal[item.signal], item.ranking) for item in saved]
    plan += [(item.rebalance, item.ranking(track.pick)) for item in rankings]
    latest, ranking = plan[-1]
    first = plan[0][0].signal
    calendar = [day for day in days if first <= day <= latest.signal]
    universe = benchmark_series(histories, calendar, portfolio.initial_capital)
    del histories
    index = index_series(data.return_index, calendar, portfolio.initial_capital)
    result = simulate_account(PriceTable.from_panel(data.panel), plan, settings_for(portfolio, backtest, track.top_n, track.pick),
                              OPEN, start=first, end=latest.signal, delisted=data.delisted, close_out=False)
    held = [position.code for position in result.open_positions]
    kept, bought, sold = planned_changes(ranking, held, track.top_n)
    report = TrackReport(latest=latest, previous=plan[-2][0] if len(plan) > 1 else None, result=result, index=index,
                         universe=universe, kept=kept, bought=bought, sold=sold, skipped=not ranking)
    logger.info("模擬組合 %s：權益 %.0f 元，賣 %d、買 %d、留任 %d", latest.signal, float(result.equity.iloc[-1]), len(sold),
                len(bought), len(kept))

    if notifier is None:
        messages = format_track_messages(report, track, data.names, None, MESSAGE_LIMIT)
        (out or sys.stdout).write(format_for_terminal(messages))
        return report
    csv_dir = portfolio.output_dir / "track"
    write_track_csv(report, data.names, ranking, csv_dir)
    messages = format_track_messages(report, track, data.names, csv_dir, MESSAGE_LIMIT)
    for i, message in enumerate(messages, start=1):
        try:
            notifier.send(message)
        except NotifyError as exc:
            raise JobFailed(f"推播失敗（第 {i}/{len(messages)} 則）：{exc}") from exc
    logger.info("已推播研究頻道：%d 則", len(messages))
    with PaperStore(portfolio.paper_db_path) as paper:          # 推播成功才寫入，失敗時下次重新產生
        paper.save([SavedRebalance(item.rebalance.signal, item.rebalance.available, track.factor, track.top_n, track.pick,
                                   item.ranking(track.pick)) for item in rankings], datetime.now())
    return report


def _saved_rebalances(path: Path) -> list[SavedRebalance]:
    if not path.is_file():
        return []
    with PaperStore(path) as paper:
        return paper.rebalances()


def _check_same_track(saved: Sequence[SavedRebalance], track: PaperPortfolio) -> None:
    """一個狀態檔只追蹤一組：換組合時要換 paper_db_path，舊的名單才不會混進新的績效。"""
    for item in saved:
        if (item.factor, item.top_n, item.pick) != (track.factor, track.top_n, track.pick):
            raise JobFailed(f"paper_portfolio.db 追蹤的是 {item.factor} {item.top_n} 檔 {item.pick}，"
                            f"與 research.yaml 的 {track.factor} {track.top_n} 檔 {track.pick} 不同；"
                            "要換組合請改用另一個 paper_db_path")


def last_friday(today: date) -> date:
    """today 之前最近的週五（不含 today）。"""
    return today - timedelta(days=(today.weekday() - 4) % 7 or 7)


def _require_fresh(covered: date, today: date, market: MarketConfig) -> None:
    """換股週但 market.db 還沒更新到本週最後一天時失敗並通知；不是換股週就不管（market-update 失敗時自己會通知）。"""
    friday = last_friday(today)
    if covered >= friday:
        return
    available = date(friday.year, friday.month, market.revenue_available_day) + timedelta(days=1)
    while available.weekday() >= 5:
        available += timedelta(days=1)
    if friday - timedelta(days=4) <= available <= friday:
        raise JobFailed(f"market.db 只更新到 {covered}，換股週需要到 {friday}；請先執行 market-update")


def pending_rebalances(schedule: Sequence[Rebalance], saved: Sequence[SavedRebalance], today: date) -> list[Rebalance]:
    """要產生名單的換股。已有紀錄時補上之後所有的換股（錯過的週六不會少換一次）；第一次執行只接受最近一週的換股日。"""
    if saved:
        return [rebalance for rebalance in schedule if rebalance.signal > saved[-1].signal]
    if not schedule:
        return []
    monday = last_friday(today) - timedelta(days=4)
    return [schedule[-1]] if schedule[-1].signal >= monday else []


def planned_changes(ranking: Sequence[str], held: Sequence[str], top_n: int) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(留任, 買進, 賣出)。買不買得到要到成交日才知道，這裡假設買得到；實際的遞補在下一次結算時算。排名是空的時當月不換股。"""
    if not ranking:
        return tuple(held), (), ()
    chosen, _ = new_list(ranking, held, top_n, lambda code: True)
    wanted = set(chosen)
    current = set(held)
    return (tuple(code for code in chosen if code in current), tuple(code for code in chosen if code not in current),
            tuple(code for code in held if code not in wanted))
