"""交易回測的因子挑選與訊號分組（RESEARCH_PLAN.md 4.7、R40、R42）。"""

import io
import math
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.test_trade_backtest_job import FLAT, HISTORY, VALIDATION_START
from backtest.test_trades import ZIGZAG, candidates
from conftest import REPO_CONFIG_DIR
from research.test_market_backtest import CONFIG, research, watchlist_config, write_market
from screener.analysis.adjustment import adjust_bars, event_factors
from screener.backtest.signals import StockHistory
from screener.config import ConfigError, load_backtest_config
from screener.jobs import backtest as backtest_job
from screener.jobs import trade_backtest as trade_job
from screener.models import Market, StockInfo
from screener.research.factor_eval import quantile_labels
from screener.research.market.store import SqliteMarketStore
from screener.research.trade_factors import (NO_VALUE, NOT_GROUPED, FactorTables, attach_factors, check_factors,
                                             signal_groups, signal_returns)

DAYS = [date(2026, 2, 2), date(2026, 2, 3)]
CODES = [f"{1000 + i}" for i in range(7)]


def tables(values: list[list[float]], eligible: list[list[bool]], *, min_stocks: int = 5) -> FactorTables:
    frame = pd.DataFrame(values, index=pd.Index(DAYS), columns=CODES)
    mask = pd.DataFrame(eligible, index=pd.Index(DAYS), columns=CODES)
    return FactorTables({"rev_yoy": frame}, mask, quantiles=5, min_stocks=min_stocks)


# --- 分組 -------------------------------------------------------------------

def test_stocks_in_the_pool_are_grouped_like_m9() -> None:
    values = [[0.3, -0.1, 0.5, 0.5, 0.2, 9.9, np.nan], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]]
    eligible = [[True, True, True, True, True, False, True], [True] * 7]
    item = tables(values, eligible)

    masked = np.where(np.array(eligible), np.array(values, dtype=float), np.nan)
    expected = quantile_labels(masked, 5, 5)
    for day, row in enumerate(expected):
        for column, code in enumerate(CODES):
            if eligible[day][column] and not np.isnan(values[day][column]):
                assert item.group("rev_yoy", code, DAYS[day]) == row[column], (day, code)
    assert [item.group("rev_yoy", c, DAYS[0]) for c in ("1001", "1004", "1000", "1002", "1003")] == [1, 2, 3, 5, 5]


def test_stock_outside_the_pool_is_only_located() -> None:
    # 1005 不在股票池：值 9.9 落在最高組，不改變其他股票的分組；1006 沒有值
    item = tables([[0.3, -0.1, 0.5, 0.5, 0.2, 9.9, np.nan], [0.0] * 7],
                  [[True, True, True, True, True, False, True], [True] * 7])

    assert item.group("rev_yoy", "1005", DAYS[0]) == 5
    assert item.group("rev_yoy", "1006", DAYS[0]) == NO_VALUE
    assert item.group("rev_yoy", "9999", DAYS[0]) == NO_VALUE                     # 不在面板
    assert item.group("rev_yoy", "1000", date(2026, 2, 4)) == NOT_GROUPED        # 面板沒有這一天


def test_days_with_too_few_values_are_not_grouped() -> None:
    item = tables([[0.1, 0.2, 0.3, 0.4, np.nan, np.nan, np.nan], [0.0] * 7], [[True] * 7, [True] * 7])

    assert item.group("rev_yoy", "1000", DAYS[0]) == NOT_GROUPED
    assert item.value("rev_yoy", "1000", DAYS[0]) == pytest.approx(0.1)
    assert math.isnan(item.value("rev_yoy", "1004", DAYS[0]))


def test_categorical_or_unknown_factor_cannot_be_a_pick() -> None:
    research_config = research(Path("."), date(2026, 1, 5), 2)

    check_factors(research_config, ["rev_yoy", "rev_accel", "dy"])
    with pytest.raises(ConfigError, match="trend 是類別因子"):
        check_factors(research_config, ["trend"])
    with pytest.raises(ConfigError, match="rev_growth 不是 research.yaml 的因子 id"):
        check_factors(research_config, ["rev_growth"])


# --- 報酬 -------------------------------------------------------------------

def flat_history() -> StockHistory:
    info = StockInfo(code="2222", name="股2222", market=Market.TSE, group="PCB", subcategory="銅箔基板")
    return StockHistory(info, FLAT, adjust_bars(FLAT, [], []), event_factors(FLAT, [], []))


def test_signal_returns_use_own_bars_and_same_day_baseline() -> None:
    # 1111：02-06 收 124 進場；第 3 根 K 棒 02-11 收 130；照出場規則 02-10 收 118 賣出。2222 一路 100
    (candidate,) = candidates(HISTORY)
    returns = signal_returns([candidate], [HISTORY, flat_history()], 3, 0.3798)

    row = returns.iloc[0]
    assert row["ret"] == pytest.approx((130 / 124 - 1) * 100)
    assert row["excess"] == pytest.approx((130 / 124 - 1) * 100 / 2)            # 基準為兩檔的平均
    assert row["trade"] == pytest.approx((118 / 124 - 1) * 100 - 0.3798)


def test_signal_groups_list_counts_and_top_minus_bottom() -> None:
    item = tables([[0.1, 0.2, 0.3, 0.4, 0.5, np.nan, np.nan], [0.0] * 7], [[True] * 7, [True] * 7])
    cands = [replace(c, code=code) for c, code in zip(candidates(HISTORY) * 3, ("1000", "1004", "1005"))]
    cands = [replace(c, entry_day=DAYS[0]) for c in cands]
    returns = pd.DataFrame({"code": ["1000", "1004", "1005"], "entry_day": DAYS[:1] * 3, "ret": [1.0, 3.0, 9.0],
                            "excess": [0.5, 2.0, 8.0], "trade": [-1.0, 2.0, 5.0]})

    groups = signal_groups(cands, returns, item, ["rev_yoy"]).set_index("group")

    assert groups.loc[1, "count"] == 1 and groups.loc[5, "count"] == 1 and groups.loc[NO_VALUE, "count"] == 1
    assert groups.loc[3, "count"] == 0
    assert NOT_GROUPED not in groups.index
    diff = groups.loc["top_minus_bottom"]
    assert (diff["ret_mean"], diff["excess_mean"], diff["trade_mean"]) == pytest.approx((2.0, 1.5, 3.0))


# --- 指令 -------------------------------------------------------------------

def add_fundamentals(path: Path) -> None:
    """1111：2025-12 營收年增 +50%、殖利率 5%；2222：+10%、沒有配息。2025-12 的營收 2026-01-12 起可用（R26）。"""
    with SqliteMarketStore(path) as store:
        for code, revenue in (("1111", 150), ("2222", 110)):
            for month, value in (("2025-09-01", 100), ("2025-12-01", revenue)):
                store._conn.execute(
                    "INSERT INTO monthly_revenue (code, month, market, name, industry, revenue, previous, last_year,"
                    " cumulative, cumulative_last_year) VALUES (?, ?, 'tse', ?, '電子', ?, 100, 100, 100, 100)",
                    (code, month, f"股{code}", value))
        store._conn.execute("INSERT INTO valuations (code, date, market, pe, pb, dividend_yield) VALUES"
                            " ('1111', ?, 'tse', 10, 1, 5.0), ('2222', ?, 'tse', 10, 1, NULL)",
                            (ZIGZAG.index[0].isoformat(), ZIGZAG.index[0].isoformat()))
        store._conn.commit()


def factor_env(tmp_path: Path):
    bars = {"1111": HISTORY.bars, "2222": FLAT}
    write_market(tmp_path / "market.db", bars, {"1111": 2_000_000, "2222": 1_000_000})
    add_fundamentals(tmp_path / "market.db")
    _, bt, research_config = factor_env_settings(tmp_path)
    return watchlist_config(tmp_path, bars), bt, research_config


def run_trade(config, bt, research_config, universe: str, **kwargs):
    out = io.StringIO()
    options = {"validate": False, "max_positions": None, "pick": None, **kwargs}
    result = trade_job.run(config, bt, rules=CONFIG.rules, rules_label="rules.yaml", universe=universe,
                           research=research_config, out=out, workers=1, **options)
    return result, out.getvalue()


@pytest.mark.parametrize("universe", [backtest_job.WATCHLIST, backtest_job.MARKET])
def test_factor_picks_run_and_signal_groups_are_written(tmp_path: Path, universe: str) -> None:
    config, bt, research_config = factor_env(tmp_path)

    result, text = run_trade(config, bt, research_config, universe)

    assert [r.settings.pick for r in result.runs] == ["nearest_ma", "volume_ratio", "rev_yoy", "rev_accel", "dy"]
    (candidate,) = result.candidates
    assert candidate.factors["rev_yoy"] == pytest.approx(0.5)
    assert candidate.factors["rev_accel"] == pytest.approx(0.5)                # +50% − 0%（3 個月前）
    assert candidate.factors["dy"] > 0
    assert "rev_yoy 最高" in text and "這次比較 1 種上限 × 5 種挑選方式 = 5 組" in text
    assert "■ 訊號分組（觀察段的全部進場訊號" in text
    folder = tmp_path / ("trade" if universe == backtest_job.WATCHLIST else "market/trade") / "observe"
    groups = pd.read_csv(folder / "signal_groups.csv", encoding="utf-8-sig")
    top = groups[(groups["因子"] == "rev_yoy") & (groups["組"] == "第 2 組")].iloc[0]
    assert top["次數"] == 1                                                    # 1111 在兩檔中因子值最大


def test_watchlist_stock_outside_the_pool_gets_values_but_is_not_counted(tmp_path: Path) -> None:
    # 3333 只在觀察清單：成交金額最小，從沒入選股票池（2 檔）；加進面板只為了算因子值
    bars = {"1111": HISTORY.bars, "2222": FLAT, "3333": FLAT}
    write_market(tmp_path / "market.db", bars, {"1111": 2_000_000, "2222": 1_000_000, "3333": 1})
    add_fundamentals(tmp_path / "market.db")
    _, bt, research_config = factor_env_settings(tmp_path)

    result, text = run_trade(watchlist_config(tmp_path, bars), bt, research_config, backtest_job.MARKET)

    assert "曾入選 2 檔" in text
    assert [c.code for c in result.candidates] == ["1111"]


def factor_env_settings(tmp_path: Path):
    research_config = research(tmp_path, ZIGZAG.index[0], 2)
    research_config = replace(research_config, factors=replace(research_config.factors, min_stocks=1, quantiles=2))
    bt = load_backtest_config(REPO_CONFIG_DIR / "backtest.yaml")
    bt = replace(bt, signal_start=ZIGZAG.index[0], validation_start=VALIDATION_START,
                 trade=replace(bt.trade, max_positions=(1,), random_runs=2, output_dir=tmp_path / "trade"))
    return None, bt, research_config


def test_validate_with_a_factor_pick_checks_only_that_factor(tmp_path: Path) -> None:
    config, bt, research_config = factor_env(tmp_path)

    result, text = run_trade(config, bt, research_config, backtest_job.WATCHLIST, validate=True, max_positions=1,
                             pick="rev_yoy")

    assert [r.settings.pick for r in result.runs] == ["rev_yoy"]
    groups = pd.read_csv(tmp_path / "trade" / "validate" / "signal_groups.csv", encoding="utf-8-sig")
    assert set(groups["因子"]) == {"rev_yoy"} and set(groups["段"]) == {"驗證段"}


def test_validate_with_a_fixed_pick_does_not_read_factors(tmp_path: Path) -> None:
    config, bt, research_config = factor_env(tmp_path)
    stale = tmp_path / "trade" / "validate" / "signal_groups.csv"
    stale.parent.mkdir(parents=True)
    stale.write_text("old", encoding="utf-8")

    result, text = run_trade(config, bt, None, backtest_job.WATCHLIST, validate=True, max_positions=1,
                             pick="nearest_ma")

    assert "訊號分組" not in text and not stale.exists()           # 上一次的分組不會留著


def test_factor_pick_without_research_config_fails(tmp_path: Path) -> None:
    config, bt, _ = factor_env(tmp_path)

    with pytest.raises(ValueError, match="需要 research.yaml"):
        run_trade(config, bt, None, backtest_job.WATCHLIST)


def test_attach_factors_marks_missing_values_as_nan() -> None:
    item = tables([[0.1] * 7, [0.2] * 7], [[True] * 7, [True] * 7])
    (candidate,) = candidates(HISTORY)                                         # 02-06 不在面板

    (attached,) = attach_factors([candidate], item)

    assert math.isnan(attached.factors["rev_yoy"])
