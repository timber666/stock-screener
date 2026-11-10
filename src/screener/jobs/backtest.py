"""訊號回測：python -m screener backtest（PROJECT_PLAN.md 14.1）。

逐日回放規則、計算訊號後的報酬並與同日觀察清單的平均比較，輸出終端摘要、CSV 與圖表報表。
各股票的訊號互不相關，以多個行程平行計算；歷史資料在主行程讀出後才分給各行程，各行程不碰資料庫。

--universe market 改用全市場的動態股票池（RESEARCH_PLAN.md 4.3）：資料來自 market.db，基準為同日當月股票池的
平均，輸出到 research.yaml 的 market.output_dir；另以相同規則重跑觀察清單，在兩者都有資料的期間並列。
"""

import logging
import os
import sys
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import TextIO

import pandas as pd

from screener.backtest.report import format_summary, write_csv, write_html
from screener.backtest.signals import (StockHistory, add_flags, add_returns, dedupe, find_signals,
                                       forward_returns, load_history)
from screener.backtest.stats import OBSERVE, VALIDATE, summarize
from screener.config import (AppConfig, BacktestConfig, ConfigError, MarketConfig, ResearchConfig, RulesConfig,
                             TrendConfig)
from screener.data.base import EventCoverage
from screener.engine import Engine
from screener.jobs import JobFailed, open_existing_store
from screener.models import Side
from screener.research.market.compare import format_signal_comparison, signal_comparison
from screener.research.market.history import MarketHistories, load_market_histories
from screener.research.market.store import SqliteMarketStore

logger = logging.getLogger(__name__)

WATCHLIST, MARKET = "watchlist", "market"
UNIVERSES = (WATCHLIST, MARKET)
MARKET_BASELINE = "當月股票池"


@dataclass(frozen=True)
class BacktestResult:
    signals: pd.DataFrame           # 未還原價判斷的訊號（主要結果）
    adjusted_signals: pd.DataFrame  # 還原價判斷的訊號（D34 的對照組）
    summary: pd.DataFrame           # 主要結果所有分組的統計
    adjusted_summary: pd.DataFrame
    start: date
    end: date
    rule_names: dict[str, str]      # 規則 id → 名稱，依規則檔順序
    rule_sides: dict[str, str]


def run(config: AppConfig, backtest: BacktestConfig, *, rules: RulesConfig, rules_label: str,
        rule_ids: Sequence[str] | None, start: date | None, end: date | None, horizon: int | None,
        universe: str = WATCHLIST, research: ResearchConfig | None = None, out: TextIO | None = None,
        workers: int | None = None) -> BacktestResult:
    horizon = horizon or backtest.summary_horizon
    if horizon not in backtest.horizons:
        raise ConfigError(f"--horizon {horizon}：應為 backtest.yaml 的 horizons 其中之一 {list(backtest.horizons)}")
    engine = build_engine(rules, rule_ids)       # 先檢查規則，錯誤時在讀資料前就結束
    if universe == MARKET:
        if research is None:
            raise ValueError("全市場需要 research.yaml 的設定")
        return _run_market(config, backtest, research.market, rules=rules, rules_label=rules_label,
                           rule_ids=rule_ids, start=start, end=end, horizon=horizon, out=out, workers=workers)
    histories, warnings = load_histories(config)

    start = start or backtest.signal_start
    end = end or max(h.bars.index[-1] for h in histories)
    logger.info("訊號回測 %s ~ %s：%d 檔，規則檔 %s，%d 條觸發規則", start, end, len(histories), rules_label,
                len(engine.trigger_rules))
    result = backtest_histories(histories, rules=rules, rule_ids=rule_ids, backtest=backtest, start=start,
                                end=end, workers=workers)

    output_dir = backtest.output_dir
    write_csv(result, backtest, output_dir)
    write_html(result, backtest, horizon, output_dir / "report.html")
    (out or sys.stdout).write(format_summary(result, backtest, horizon, f"觀察清單 {len(histories)} 檔", rules_label,
                                             output_dir, warnings=warnings) + "\n")
    return result


def _run_market(config: AppConfig, backtest: BacktestConfig, market: MarketConfig, *, rules: RulesConfig,
                rules_label: str, rule_ids: Sequence[str] | None, start: date | None, end: date | None, horizon: int,
                out: TextIO | None, workers: int | None) -> BacktestResult:
    loaded = load_market(market)
    start = start or market.signal_start
    end = end or loaded.last_day
    logger.info("訊號回測（全市場）%s ~ %s：曾入選股票池 %d 檔，規則檔 %s", start, end, len(loaded.histories), rules_label)
    result = backtest_histories(loaded.histories, rules=rules, rule_ids=rule_ids, backtest=backtest, start=start,
                                end=end, workers=workers)

    output_dir = market.output_dir
    write_csv(result, backtest, output_dir)
    write_html(result, backtest, horizon, output_dir / "report.html", baseline_label=MARKET_BASELINE)
    text = format_summary(result, backtest, horizon, market_label(market, loaded), rules_label, output_dir,
                          warnings=market_warnings(loaded), baseline_label=MARKET_BASELINE)
    comparison = _compare_watchlist(config, result, loaded, backtest, market, rules=rules, rule_ids=rule_ids,
                                    start=start, end=end, horizon=horizon, workers=workers)
    (out or sys.stdout).write(f"{text}\n\n{comparison}\n")
    return result


def _compare_watchlist(config: AppConfig, result: BacktestResult, loaded: MarketHistories, backtest: BacktestConfig,
                       market: MarketConfig, *, rules: RulesConfig, rule_ids: Sequence[str] | None, start: date,
                       end: date, horizon: int, workers: int | None) -> str:
    """以相同規則重跑觀察清單，在兩者都有資料的期間並列；寫出 comparison.csv，回傳終端的文字。"""
    watchlist, _ = load_histories(config)
    compare_start = max(market.compare_start, start)
    compare_end = min(end, max(h.bars.index[-1] for h in watchlist))
    path = market.output_dir / "comparison.csv"
    path.unlink(missing_ok=True)
    if compare_start > compare_end:
        return f"■ 與觀察清單並列：{compare_start} 之後沒有兩者都有資料的期間，略過"
    logger.info("對照：觀察清單 %d 檔，%s ~ %s", len(watchlist), compare_start, compare_end)
    watch = backtest_histories(watchlist, rules=rules, rule_ids=rule_ids, backtest=backtest, start=compare_start,
                               end=compare_end, workers=workers)
    comparison = signal_comparison(result, watch, loaded.histories, watchlist, backtest, start=compare_start,
                                   end=compare_end, horizon=horizon)
    comparison.round(4).to_csv(path, index=False, encoding="utf-8-sig")
    text = format_signal_comparison(comparison, start=compare_start, end=compare_end, horizon=horizon)
    return f"{text}\n  明細：{path}"


def load_market(market: MarketConfig, extra_codes: Collection[str] = ()) -> MarketHistories:
    """讀出全市場曾經入選股票池的股票（訊號回測與交易回測共用）；extra_codes 見 load_market_histories。"""
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")
    with SqliteMarketStore(market.db_path) as store:
        try:
            loaded = load_market_histories(store, market.universe, extra_codes)
        except ValueError as exc:
            raise JobFailed(f"{market.db_path}：{exc}，請先執行 market-update") from exc
    if not loaded.histories:
        raise JobFailed(f"股票池是空的：market.db 的交易日不足 {market.universe.lookback_days} 天")
    return loaded


def market_label(market: MarketConfig, loaded: MarketHistories) -> str:
    return (f"全市場股票池（每月第一個交易日依前 {market.universe.lookback_days} 日平均成交金額取前 "
            f"{market.universe.size} 檔，{loaded.universe.first_day} 起；曾入選 {len(loaded.histories):,} 檔）")


def market_warnings(loaded: MarketHistories) -> list[str]:
    if not loaded.derived_events:
        return []
    return [f"還原權息：{loaded.derived_events:,} 天沒有官方資料，以行情推出的參考價補上（明細見 market-check）"]


def load_histories(config: AppConfig) -> tuple[list[StockHistory], list[str]]:
    """讀出觀察清單的日 K、除權息與公司行動、上市櫃日（訊號回測與交易回測共用）。

    回傳 (歷史資料, 要印在終端摘要的提醒)。有日 K 卻沒查過事件的股票無法還原權息，以失敗結束。
    """
    with open_existing_store(config.settings.db_path) as store:
        first_bars = {s.code: store.earliest_date(s.code) for s in config.watchlist.stocks}
        stocks = [s for s in config.watchlist.stocks if first_bars[s.code] is not None]
        if no_bars := [s.code for s in config.watchlist.stocks if first_bars[s.code] is None]:
            logger.warning("%d 檔資料庫沒有日 K，不列入回測：%s", len(no_bars), "、".join(no_bars))
        coverages = {s.code: store.event_coverage(s.code) for s in stocks}
        missing = [s.code for s in stocks
                   if (c := coverages[s.code]) is None or first_bars[s.code] < c.start]
        if missing:
            raise JobFailed(f"{len(missing)} 檔尚未查詢除權息與公司行動，無法還原權息，"
                            f"請先執行 events：{'、'.join(missing[:20])}{' 等' if len(missing) > 20 else ''}")
        # events 每次執行都會先存入證交所清單的上市櫃日；查過事件卻沒有上市櫃日，代表不在清單中（例：已下市）
        if unlisted := [s.code for s in stocks if store.get_listing(s.code) is None]:
            logger.warning("%d 檔不在證交所的上市、上櫃清單中（可能已下市或代號錯誤），無法排除興櫃期間，照常回測：%s",
                           len(unlisted), "、".join(unlisted))
        histories = [load_history(store, store, s) for s in stocks]
    if not histories:
        raise JobFailed("資料庫沒有任何日 K，請先執行 update")
    return histories, stale_events_warnings(histories, coverages)


def stale_events_warnings(histories: Sequence[StockHistory], coverages: Mapping[str, EventCoverage]) -> list[str]:
    """事件資料只查到上次執行 events 的那天，之後的日 K 若有除權息、減資不會還原，報酬與漲跌停、跳空標記
    可能有誤。只提醒、照常回測（D41）；回傳要印在終端摘要的提醒，代號寫入 log。"""
    stale = [h for h in histories if coverages[h.info.code].end < h.bars.index[-1]]
    if not stale:
        return []
    message = (f"{len(stale)} 檔的除權息與公司行動只查到 {min(coverages[h.info.code].end for h in stale)}，"
               f"日 K 已到 {max(h.bars.index[-1] for h in stale)}：之後的除權息、減資沒有還原，報酬與漲跌停、"
               f"跳空標記可能有誤，請先執行 events 再回測")
    logger.warning("%s：%s", message, "、".join(h.info.code for h in stale))
    return [message]


def build_engine(rules: RulesConfig, rule_ids: Sequence[str] | None) -> Engine:
    """依規則檔建立引擎；指定 rule_ids 時只保留這些觸發規則（D35）。"""
    engine = Engine.from_config(rules)
    if rule_ids is None:
        return engine
    enabled = {rule.id for rule in engine.trigger_rules}
    unknown = [rule_id for rule_id in rule_ids if rule_id not in enabled]
    if unknown:
        raise ConfigError(f"--rules：規則檔中沒有已啟用的觸發規則 {', '.join(unknown)}；"
                          f"可用的有 {', '.join(sorted(enabled))}")
    return Engine(filters=engine.filters, trigger_rules=[r for r in engine.trigger_rules if r.id in rule_ids])


def backtest_histories(histories: Sequence[StockHistory], *, rules: RulesConfig, rule_ids: Sequence[str] | None,
                       backtest: BacktestConfig, start: date, end: date,
                       workers: int | None = None) -> BacktestResult:
    tasks = [_Task(h, rules, tuple(rule_ids) if rule_ids else None, start, end, backtest.horizons,
                   rules.trend.lookback_days, backtest.gap_flag_pct) for h in histories]
    workers = workers or min(os.cpu_count() or 1, len(tasks))
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(_process_stock, tasks, chunksize=4))
    else:
        outputs = [_process_stock(task) for task in tasks]

    baseline = {n: pd.concat([fwd[n] for _, _, fwd in outputs], axis=1).mean(axis=1) for n in backtest.horizons}
    engine = build_engine(rules, rule_ids)
    names = {rule.id: rule.name for rule in engine.trigger_rules}
    sides = {rule.id: rule.side.value for rule in engine.trigger_rules}
    signals = _finish([row for raw, _, _ in outputs for row in raw], baseline, backtest)
    adjusted = _finish([row for _, adj, _ in outputs for row in adj], baseline, backtest)
    cost = backtest.cost.round_trip_pct
    logger.info("訊號 %d 個（去重後 %d），還原價對照 %d 個", len(signals), int(signals["kept"].sum()) if len(signals) else 0,
                len(adjusted))
    return BacktestResult(
        signals=signals, adjusted_signals=adjusted,
        summary=summarize(signals, backtest.horizons, cost, list(names)),
        adjusted_summary=summarize(adjusted, backtest.horizons, cost, list(names)),
        start=start, end=end, rule_names=names, rule_sides=sides,
    )


@dataclass(frozen=True)
class _Task:
    history: StockHistory
    rules: RulesConfig
    rule_ids: tuple[str, ...] | None
    start: date
    end: date
    horizons: tuple[int, ...]
    window: int
    gap_flag_pct: float


def _process_stock(task: _Task) -> tuple[list[dict], list[dict], pd.DataFrame]:
    """一檔股票的（未還原價訊號, 還原價訊號, 每日持有 N 日報酬）。在子行程中執行。"""
    engine = build_engine(task.rules, task.rule_ids)
    trend: TrendConfig = task.rules.trend
    outputs = []
    for adjusted_rules in (False, True):
        rows = find_signals(task.history, engine, trend, task.start, task.end, adjusted_rules=adjusted_rules)
        add_flags(rows, task.history, window=task.window, gap_flag_pct=task.gap_flag_pct)
        add_returns(rows, task.history, task.horizons)
        outputs.append(rows)
    return outputs[0], outputs[1], forward_returns(task.history, task.horizons)


def _finish(rows: list[dict], baseline: dict[int, pd.Series], backtest: BacktestConfig) -> pd.DataFrame:
    """加上基準報酬、超額報酬、觀察段／驗證段與去重標記，依日期與代號排序。"""
    columns = ["date", "position", "code", "name", "group", "rule_id", "rule_name", "side", "trend",
               "entry_price", "gap_event", "limit_locked"]
    signals = pd.DataFrame(rows, columns=columns + [f"{k}_{n}" for n in backtest.horizons
                                                    for k in ("ret", "mfe", "mae")])
    sign = signals["side"].map({Side.LONG.value: 1, Side.SHORT.value: -1})
    for n in backtest.horizons:
        signals[f"base_{n}"] = sign * signals["date"].map(baseline[n])
        signals[f"excess_{n}"] = signals[f"ret_{n}"] - signals[f"base_{n}"]
    signals["segment"] = [OBSERVE if day < backtest.validation_start else VALIDATE for day in signals["date"]]
    signals["kept"] = dedupe(signals, backtest.dedupe_days)
    # 沒有訊號時欄位是 object 型別，布林欄位要明確轉型，否則 signals[mask] 會被當成選欄位
    signals = signals.astype({"gap_event": bool, "limit_locked": bool, "kept": bool})
    return signals.sort_values(["date", "code", "rule_id"]).reset_index(drop=True)
