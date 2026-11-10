"""backtest、trade-backtest --universe market：全市場股票池的回測，與觀察清單並列（RESEARCH_PLAN.md 4.3）。"""

import io
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backtest.test_backtest_job import BACKTEST, CONFIG, DAY, FLAT, RISES, daily_bars, stock
from backtest.test_trade_backtest_job import HISTORY, VALIDATION_START
from backtest.test_trade_backtest_job import FLAT as TRADE_FLAT
from backtest.test_trades import ZIGZAG
from conftest import REPO_CONFIG_DIR
from screener.config import UniverseConfig, load_backtest_config, load_research_config
from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed
from screener.jobs import backtest as backtest_job
from screener.jobs import trade_backtest as trade_job
from screener.models import Listing, Market
from screener.research.market.models import DailyQuotes, MarketQuote
from screener.research.market.store import SqliteMarketStore

RESEARCH = load_research_config(REPO_CONFIG_DIR / "research.yaml")


def write_market(path: Path, bars_by_code: dict[str, pd.DataFrame], values: dict[str, int]) -> None:
    """以日 K 建立 market.db：漲跌 = 收盤 − 前一日收盤（參考價即前一日收盤，沒有事件）。"""
    days = sorted({day for bars in bars_by_code.values() for day in bars.index})
    with SqliteMarketStore(path) as store:
        for day in days:
            quotes = []
            for code, bars in bars_by_code.items():
                i = bars.index.get_loc(day)
                r = bars.iloc[i]
                prev = bars["close"].iloc[i - 1] if i else r["close"]
                quotes.append(MarketQuote(code, day, Market.TSE, f"股{code}", r["open"], r["high"], r["low"], r["close"],
                                          int(r["volume"]) * 1000, values[code], 1, change=r["close"] - prev))
            store.record_day(day, [("tse_daily", DailyQuotes(day, Market.TSE, tuple(quotes))),
                                   ("otc_daily", DailyQuotes(day, Market.OTC, ()))], date(2027, 1, 1))
        store.refresh_securities()


def watchlist_config(tmp_path: Path, bars_by_code: dict[str, pd.DataFrame]):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        for code, bars in bars_by_code.items():
            store.upsert_bars(daily_bars(code, bars))
            store.set_event_coverage(code, EventCoverage(bars.index[0], bars.index[-1]))
            store.upsert_listings([Listing(code, Market.TSE, date(2000, 1, 4))])
    watchlist = replace(CONFIG.watchlist, stocks=tuple(stock(code) for code in bars_by_code))
    return replace(CONFIG, watchlist=watchlist, settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))


def research(tmp_path: Path, start: date, size: int):
    market = replace(RESEARCH.market, db_path=tmp_path / "market.db", output_dir=tmp_path / "market",
                     signal_start=start, compare_start=start, universe=UniverseConfig(size=size, lookback_days=3))
    return replace(RESEARCH, market=market)


# --- 訊號回測 -------------------------------------------------------------------

def run_backtest(tmp_path: Path, values: dict[str, int], size: int):
    bars = {"1111": RISES, "2222": FLAT}
    write_market(tmp_path / "market.db", bars, values)
    out = io.StringIO()
    result = backtest_job.run(watchlist_config(tmp_path, bars), BACKTEST, rules=CONFIG.rules, rules_label="rules.yaml",
                              rule_ids=None, start=None, end=None, horizon=None, universe=backtest_job.MARKET,
                              research=research(tmp_path, RISES.index[0], size), out=out, workers=1)
    return result, out.getvalue()


def test_market_backtest_uses_the_monthly_pool_and_lists_the_watchlist(tmp_path: Path) -> None:
    result, text = run_backtest(tmp_path, {"1111": 2_000_000, "2222": 1_000_000}, size=2)

    # 1 月之前不足 3 個交易日沒有股票池；2 月（DAY = 02-02）兩檔都入選，基準為兩檔的平均
    (signal,) = result.signals.to_dict("records")
    assert (signal["code"], signal["rule_name"], signal["date"]) == ("1111", "三線多排", DAY)
    assert (signal["ret_1"], signal["base_1"], signal["excess_1"]) == pytest.approx((10.0, 5.0, 5.0))
    assert signal["group"] == "上市"
    assert "全市場股票池（每月第一個交易日依前 3 日平均成交金額取前 2 檔，2026-02-02 起；曾入選 2 檔）" in text
    assert "超額 = 減去同日當月股票池的平均報酬" in text
    assert "■ 與觀察清單並列（2026-01-05 ~ 2026-02-04" in text

    out = tmp_path / "market"
    assert {p.name for p in out.iterdir()} >= {"signals.csv", "summary.csv", "report.html", "comparison.csv"}
    comparison = pd.read_csv(out / "comparison.csv", encoding="utf-8-sig")
    rule = comparison[(comparison["item"] == "三線多排") & (comparison["segment"] == "觀察段")].iloc[0]
    assert (rule["market_count"], rule["watchlist_count"]) == (1, 1)
    assert rule["market_excess"] == pytest.approx(rule["watchlist_excess"])
    bench = comparison[comparison["item"].str.startswith("基準")].iloc[0]
    assert bench["market_mean"] == pytest.approx(bench["watchlist_mean"])     # 兩檔都在股票池，基準相同


def test_stock_outside_the_pool_gives_no_signal(tmp_path: Path) -> None:
    result, text = run_backtest(tmp_path, {"1111": 1_000_000, "2222": 2_000_000}, size=1)

    assert result.signals.empty                                 # 2 月只有 2222 入選
    assert "曾入選 1 檔" in text
    comparison = pd.read_csv(tmp_path / "market" / "comparison.csv", encoding="utf-8-sig")
    rule = comparison[(comparison["item"] == "三線多排") & (comparison["segment"] == "觀察段")].iloc[0]
    assert (rule["market_count"], rule["watchlist_count"]) == (0, 1)


def test_market_backtest_requires_market_db(tmp_path: Path) -> None:
    with pytest.raises(JobFailed, match="請先執行 market-update"):
        backtest_job.run(CONFIG, BACKTEST, rules=CONFIG.rules, rules_label="rules.yaml", rule_ids=None, start=None,
                         end=None, horizon=None, universe=backtest_job.MARKET,
                         research=research(tmp_path, RISES.index[0], 2), workers=1)


# --- 交易回測 -------------------------------------------------------------------

def test_market_trade_backtest_lists_the_watchlist(tmp_path: Path) -> None:
    bars = {"1111": HISTORY.bars, "2222": TRADE_FLAT}
    write_market(tmp_path / "market.db", bars, {"1111": 2_000_000, "2222": 1_000_000})
    bt = load_backtest_config(REPO_CONFIG_DIR / "backtest.yaml")
    bt = replace(bt, signal_start=ZIGZAG.index[0], validation_start=VALIDATION_START,
                 trade=replace(bt.trade, max_positions=(1, 2), random_runs=5, output_dir=tmp_path / "trade",
                               factor_picks=()))
    out = io.StringIO()

    result = trade_job.run(watchlist_config(tmp_path, bars), bt, rules=CONFIG.rules, rules_label="rules.yaml",
                           validate=False, max_positions=None, pick=None, universe=backtest_job.MARKET,
                           research=research(tmp_path, ZIGZAG.index[0], 2), out=out, workers=1)

    assert [(c.code, c.entry_day) for c in result.candidates] == [("1111", date(2026, 2, 6))]
    text = out.getvalue()
    assert "全市場股票池" in text and "基準（當月股票池每天等權，不扣成本）" in text
    assert "■ 與觀察清單並列（2026-01-05 ~ 2026-02-06" in text
    folder = tmp_path / "market" / "trade" / "observe"
    assert (folder / "summary.csv").is_file() and (folder / "report.html").is_file()
    comparison = pd.read_csv(folder / "comparison.csv", encoding="utf-8-sig")
    assert len(comparison) == 2 * 2 * 2                         # 上限 1、2 × 兩種挑選 × 兩個範圍
    for _, group in comparison.groupby(["max_positions", "pick"]):
        market, watch = group.iloc[0], group.iloc[1]
        assert (market["universe"], watch["universe"]) == ("全市場", "觀察清單")
        assert market["total_return"] == pytest.approx(watch["total_return"])   # 兩檔都在股票池，交易相同
