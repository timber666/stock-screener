"""portfolio-backtest、portfolio-track 的流程（RESEARCH_PLAN.md 4.6）：以手工的 market.db 驗證，不連網。"""

import io
import sqlite3
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from screener.config import (ConfigError, PaperPortfolio, TOP_FACTOR, TOP_VALUE, load_backtest_config, load_config,
                             load_research_config)
from screener.jobs import JobFailed, portfolio_backtest, portfolio_track
from screener.jobs.portfolio_track import last_friday, pending_rebalances, planned_changes
from screener.models import Market
from screener.notify.base import NotifyError
from screener.research.market.models import DailyQuotes, MarketQuote
from screener.research.market.store import SqliteMarketStore
from screener.research.portfolio import Rebalance
from screener.research.portfolio_output import PICK_LABELS
from screener.research.portfolio_store import PaperStore, SavedRebalance

CODES = [f"{1000 + i}" for i in range(10)]


class FakeNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.messages: list[str] = []
        self.error = error

    def send(self, message: str) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append(message)


def _quote(code: str, day: date, price: float, market: Market = Market.TSE) -> MarketQuote:
    return MarketQuote(code=code, date=day, market=market, name=f"股{code}", open=price, high=price, low=price, close=price,
                       volume=1_000_000, value=int(price * 1_000_000), trades=100, change=0.0, flag=None,
                       next_limit_up=None, next_limit_down=None)


def build_market_db(path: Path, end: date, *, covered: date | None = None) -> list[date]:
    """從 2026-05 起每個平日都有行情的 market.db：10 檔股票，價格每天依代號微幅變動。"""
    days = [d.date() for d in pd.bdate_range(date(2026, 5, 1), end)]
    with SqliteMarketStore(path) as store:
        for position, day in enumerate(days):
            quotes = [_quote(code, day, 100.0 + i + position * (0.1 + 0.01 * i)) for i, code in enumerate(CODES)]
            store.record_day(day, [("tse_daily", DailyQuotes(day, Market.TSE, tuple(quotes), 1000.0 + position)),
                                   ("otc_daily", DailyQuotes(day, Market.OTC, ()))], day)
        for code in CODES:
            store._conn.execute("INSERT OR REPLACE INTO monthly_revenue (code, month, market, name, industry, revenue,"
                                " previous, last_year, cumulative, cumulative_last_year) VALUES (?, ?, 'tse', ?, '電子', ?, 100,"
                                " 100, 100, 100)", (code, "2026-09-01", f"股{code}", 100 + int(code[-1]) * 10))
            store._conn.execute("INSERT OR REPLACE INTO monthly_revenue (code, month, market, name, industry, revenue,"
                                " previous, last_year, cumulative, cumulative_last_year) VALUES (?, ?, 'tse', ?, '電子', ?, 100,"
                                " 100, 100, 100)", (code, "2026-10-01", f"股{code}", 100 + int(code[-1]) * 10))
        store._conn.commit()
        store.refresh_securities()
        if covered is not None:
            store._conn.execute("DELETE FROM fetch_log WHERE start > ?", (covered.isoformat(),))
            store._conn.commit()
    return days


@pytest.fixture
def env(config_dir: Path):
    config = load_config(config_dir)
    research = load_research_config(config_dir / "research.yaml")
    backtest = load_backtest_config(config_dir / "backtest.yaml")
    market = replace(research.market, signal_start=date(2026, 9, 1),
                     universe=replace(research.market.universe, size=10, lookback_days=20))
    factors = replace(research.factors, min_stocks=5, quantiles=2)
    portfolio = replace(research.portfolio, factors=("rev_yoy",), top_n=(2,), random_runs=2, track=PaperPortfolio("rev_yoy", 2, TOP_FACTOR))
    research = replace(research, market=market, factors=factors, portfolio=portfolio)
    backtest = replace(backtest, validation_start=date(2026, 11, 1))
    return config, research, backtest


def test_observe_writes_all_outputs(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 10, 30))
    backtest = replace(backtest, validation_start=date(2026, 10, 20))
    out = io.StringIO()
    result = portfolio_backtest.run(config, research, backtest, validate=False, factor=None, top_n=None, pick=None, out=out)
    assert result.segment == "觀察段"
    assert len(result.runs) == 2 * 2                # 2 種挑法 × 2 種成交價
    directory = research.portfolio.output_dir / "observe"
    for name in ("summary.csv", "trades.csv", "equity.csv", "report.html"):
        assert (directory / name).is_file()
    summary = pd.read_csv(directory / "summary.csv")
    assert set(summary["挑法"]) == set(PICK_LABELS.values())
    assert "組合回測" in out.getvalue()
    for run in result.runs:
        equity = run.result.equity
        assert equity.iloc[-1] - 300000 == pytest.approx(sum(t.pnl for t in run.result.trades))


def test_validate_runs_only_the_selected_combination(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 20))
    out = io.StringIO()
    result = portfolio_backtest.run(config, research, backtest, validate=True, factor="rev_yoy", top_n=2, pick=TOP_VALUE, out=out)
    assert result.segment == "驗證段"
    assert {(r.factor, r.top_n, r.pick) for r in result.runs} == {("rev_yoy", 2, TOP_VALUE)}
    assert (research.portfolio.output_dir / "validate" / "summary.csv").is_file()


@pytest.mark.parametrize("kwargs, message", [
    ({"validate": False, "factor": "rev_yoy", "top_n": None, "pick": None}, "只能和 --validate"),
    ({"validate": True, "factor": "rev_yoy", "top_n": None, "pick": None}, "同時指定"),
    ({"validate": True, "factor": "ep", "top_n": 2, "pick": TOP_FACTOR}, "--factor ep"),
    ({"validate": True, "factor": "rev_yoy", "top_n": 3, "pick": TOP_FACTOR}, "--top-n 3"),
    ({"validate": True, "factor": "rev_yoy", "top_n": 2, "pick": "other"}, "--pick other"),
])
def test_validate_requires_a_compared_combination(env, kwargs, message) -> None:
    config, research, backtest = env
    with pytest.raises(ConfigError, match=message):
        portfolio_backtest.run(config, research, backtest, **kwargs)


# --- 模擬組合 ---------------------------------------------------------------

def test_last_friday() -> None:
    assert last_friday(date(2026, 11, 14)) == date(2026, 11, 13)    # 週六
    assert last_friday(date(2026, 11, 13)) == date(2026, 11, 6)     # 週五當天不算
    assert last_friday(date(2026, 11, 16)) == date(2026, 11, 13)    # 週一


REB_OCT = Rebalance(date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 19))
REB_NOV = Rebalance(date(2026, 11, 11), date(2026, 11, 13), None)


def saved_row(signal: date) -> SavedRebalance:
    return SavedRebalance(signal, signal, "rev_yoy", 2, TOP_FACTOR, ("1001", "1002"))


def test_first_run_only_accepts_the_latest_week() -> None:
    assert pending_rebalances([REB_OCT, REB_NOV], [], date(2026, 11, 14)) == [REB_NOV]
    assert pending_rebalances([REB_OCT, REB_NOV], [], date(2026, 11, 21)) == []     # 上一週才換過，等下一個換股週


def test_saved_list_is_not_repeated() -> None:
    assert pending_rebalances([REB_OCT, REB_NOV], [saved_row(REB_NOV.signal)], date(2026, 11, 14)) == []


def test_missed_rebalances_are_caught_up() -> None:
    reb_sep = Rebalance(date(2026, 9, 11), date(2026, 9, 11), date(2026, 9, 14))
    assert pending_rebalances([reb_sep, REB_OCT, REB_NOV], [saved_row(reb_sep.signal)], date(2026, 11, 14)) == [REB_OCT, REB_NOV]


def test_planned_changes() -> None:
    assert planned_changes(("A", "B", "C"), ["B", "D"], 2) == (("B",), ("A",), ("D",))
    assert planned_changes((), ["B"], 2) == (("B",), (), ())


def test_track_not_rebalance_week_ends_quietly(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 6))
    notifier = FakeNotifier()
    assert portfolio_track.run(config, research, backtest, notifier=notifier, today=date(2026, 11, 7)) is None
    assert notifier.messages == []


def test_track_first_list_pushes_and_saves(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    notifier = FakeNotifier()
    report = portfolio_track.run(config, research, backtest, notifier=notifier, today=date(2026, 11, 14))
    assert report is not None
    assert report.latest.signal == date(2026, 11, 13)
    assert report.previous is None
    text = "\n".join(notifier.messages)
    assert "第一份名單" in text
    assert "下一個交易日開盤成交" in text
    assert "不下單" in text
    assert "1009 股1009" in text                    # 營收年增最高的兩檔
    with PaperStore(research.portfolio.paper_db_path) as paper:
        [saved] = paper.rebalances()
    assert saved.signal == date(2026, 11, 13)
    assert saved.ranking[:2] == ("1009", "1008")
    assert (research.portfolio.output_dir / "track" / "2026-11-13.csv").is_file()


def test_track_second_run_settles_and_reports_last_month(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 10, 16))
    first = FakeNotifier()
    portfolio_track.run(config, research, backtest, notifier=first, today=date(2026, 10, 17))
    assert "第一份名單" in "\n".join(first.messages)

    build_market_db(research.market.db_path, date(2026, 11, 13))
    second = FakeNotifier()
    report = portfolio_track.run(config, research, backtest, notifier=second, today=date(2026, 11, 14))
    assert report.previous.signal == date(2026, 10, 16)
    text = "\n".join(second.messages)
    assert "上月（10/16 收盤 → 11/13 收盤）" in text
    assert "累積（2026-10-16 起）" in text
    assert report.result.logs[0].trade == date(2026, 10, 19)    # 實際在 10/19 開盤結算第一次的名單
    assert len(report.result.open_positions) == 2

    # 重跑不會重複產生、不會再推播
    third = FakeNotifier()
    assert portfolio_track.run(config, research, backtest, notifier=third, today=date(2026, 11, 14)) is None
    assert third.messages == []


def test_track_push_failure_does_not_save(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    with pytest.raises(JobFailed, match="推播失敗"):
        portfolio_track.run(config, research, backtest, notifier=FakeNotifier(NotifyError("x")), today=date(2026, 11, 14))
    with PaperStore(research.portfolio.paper_db_path) as paper:
        assert paper.rebalances() == []


def test_track_dry_run_writes_nothing(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    out = io.StringIO()
    report = portfolio_track.run(config, research, backtest, notifier=None, out=out, today=date(2026, 11, 14))
    assert report is not None
    assert "【模擬組合】" in out.getvalue()
    assert not research.portfolio.paper_db_path.exists()
    assert not (research.portfolio.output_dir / "track").exists()


def test_track_stale_market_db_fails_on_rebalance_week(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 13), covered=date(2026, 11, 10))
    with pytest.raises(JobFailed, match="market.db 只更新到"):
        portfolio_track.run(config, research, backtest, notifier=FakeNotifier(), today=date(2026, 11, 14))


def test_track_requires_a_selected_combination(env) -> None:
    config, research, backtest = env
    research = replace(research, portfolio=replace(research.portfolio, track=None))
    with pytest.raises(ConfigError, match="track 尚未選定"):
        portfolio_track.run(config, research, backtest, notifier=None)


def test_track_refuses_a_different_combination(env) -> None:
    config, research, backtest = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    research.portfolio.paper_db_path.parent.mkdir(parents=True, exist_ok=True)
    with PaperStore(research.portfolio.paper_db_path) as paper:
        paper.save([SavedRebalance(date(2026, 10, 16), date(2026, 10, 12), "bp", 2, TOP_FACTOR, ("1001",))],
                   datetime(2026, 10, 17))
    with pytest.raises(JobFailed, match="要換組合"):
        portfolio_track.run(config, research, backtest, notifier=FakeNotifier(), today=date(2026, 11, 14))


def test_store_empty_ranking_round_trip(tmp_path: Path) -> None:
    with PaperStore(tmp_path / "p.db") as paper:
        paper.save([SavedRebalance(date(2026, 10, 16), date(2026, 10, 12), "bp", 2, TOP_FACTOR, ())],
                   datetime(2026, 10, 17))
        [row] = paper.rebalances()
    assert row.ranking == ()
    assert sqlite3.connect(tmp_path / "p.db").execute("SELECT COUNT(*) FROM rankings").fetchone() == (0,)


def test_covered_through_is_the_earlier_market(tmp_path: Path) -> None:
    with SqliteMarketStore(tmp_path / "m.db") as store:
        assert store.covered_through() is None
        store.record_day(date(2026, 11, 13), [("tse_daily", None)], date(2026, 11, 14))
        assert store.covered_through() is None
        store.record_day(date(2026, 11, 12), [("otc_daily", None)], date(2026, 11, 14))
        assert store.covered_through() == date(2026, 11, 12)
