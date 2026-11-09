"""單因子評估：報酬、Rank IC、分位數、換手與產業中性（RESEARCH_PLAN.md 4.4）。數字都是手算的。"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from screener.research.factor_eval import (CLOSE, NEXT_OPEN, daily_ic, forward_returns, group_returns, neutralize,
                                           non_overlapping_t_stat, quantile_labels, restrict, top_turnover)
from screener.research.factors.panel import Panel

NAN = float("nan")


def make_panel(closes: dict[str, list[float]], opens: dict[str, list[float]] | None = None) -> Panel:
    n = len(next(iter(closes.values())))
    days = pd.Index([date(2026, 1, 1) + timedelta(days=i) for i in range(n)], name="date")
    close = pd.DataFrame(closes, index=days, dtype=float)
    open_ = pd.DataFrame(opens, index=days, dtype=float) if opens else close.copy()
    flags = pd.DataFrame(False, index=days, columns=close.columns)
    return Panel(open=open_, high=close, low=close, close=close, raw_close=close, raw_open=close, reference=close, volume=close, value=close,
                 eligible=close.notna(), limit_up=flags, limit_down=flags)


def test_forward_returns_use_last_close_for_untraded_exit_and_delisting() -> None:
    panel = make_panel({"A": [10, 11, NAN, 12, 13], "B": [10, 20, NAN, NAN, NAN]})

    returns = forward_returns(panel, 2, CLOSE)

    # A：第 2 天（索引 2）沒有成交，t=0 持有 2 日的出場價用 11（t+2 以前最後一個收盤）；B 下市，以最後一個收盤結算
    assert returns[0, 0] == pytest.approx(10.0)
    assert returns[1, 0] == pytest.approx((12 / 11 - 1) * 100)
    assert np.isnan(returns[2, 0])              # 當天沒有成交，不能進場
    assert returns[0, 1] == pytest.approx(100.0)
    assert returns[1, 1] == pytest.approx(0.0)
    assert np.isnan(returns[3:]).all()          # 之後不足 2 個交易日


def test_next_open_entry_is_the_next_day_open() -> None:
    panel = make_panel({"A": [10, 11, 12, 13]}, opens={"A": [9, 10.5, NAN, 12.5]})

    returns = forward_returns(panel, 2, NEXT_OPEN)

    assert returns[0, 0] == pytest.approx((12 / 10.5 - 1) * 100)       # 隔日開盤 10.5 → t+2 收盤 12
    assert np.isnan(returns[1, 0])                                     # 隔日沒有成交，不能進場
    assert returns[2, 0] != returns[2, 0]                              # 之後不足 2 個交易日


def test_unknown_entry_is_rejected() -> None:
    with pytest.raises(ValueError):
        forward_returns(make_panel({"A": [1.0, 2.0]}), 1, "open")


def test_restrict_keeps_only_eligible_stocks_and_drops_infinite_values() -> None:
    panel = make_panel({"A": [1.0, 2.0], "B": [3.0, NAN]})
    factor = pd.DataFrame({"A": [np.inf, 0.5], "B": [1.0, 2.0]}, index=panel.days)

    values = restrict(factor, panel)

    assert np.isnan(values[0, 0])               # 無限大
    assert np.isnan(values[1, 1])               # B 當天不可列入
    assert values[1, 0] == 0.5


def test_daily_ic_perfect_and_reversed() -> None:
    factor = np.array([[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]])
    returns = np.array([[10.0, 20.0, 30.0, 40.0], [-1.0, -2.0, -3.0, -4.0]])

    ics = daily_ic(factor, returns, min_stocks=4)

    assert ics[0] == pytest.approx(1.0)
    assert ics[1] == pytest.approx(-1.0)


def test_daily_ic_hand_calculated_with_ties_and_missing() -> None:
    # 有效的 4 檔。因子排名 1, 2.5, 2.5, 4；報酬排名 1, 3, 2, 4。
    factor = np.array([[1.0, 2.0, 2.0, 5.0, NAN]])
    returns = np.array([[-1.0, 3.0, 1.0, 9.0, 100.0]])

    x, y = np.array([1, 2.5, 2.5, 4.0]), np.array([1, 3, 2, 4.0])
    expected = np.corrcoef(x, y)[0, 1]

    assert daily_ic(factor, returns, min_stocks=4)[0] == pytest.approx(expected)
    assert np.isnan(daily_ic(factor, returns, min_stocks=5)[0])         # 股票數不足


def test_quantile_labels_top_group_has_largest_factor_values() -> None:
    factor = np.array([[float(i) for i in range(1, 11)]])

    labels = quantile_labels(factor, 5, min_stocks=10)

    assert list(labels[0]) == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert (quantile_labels(factor, 5, min_stocks=11) == 0).all()       # 股票數不足時不分組


def test_quantile_labels_skip_missing_values() -> None:
    labels = quantile_labels(np.array([[3.0, NAN, 1.0, 2.0]]), 3, min_stocks=3)

    assert list(labels[0]) == [3, 0, 1, 2]


def test_group_returns_equal_weight_and_universe() -> None:
    labels = np.array([[1, 1, 2, 0]], dtype=np.int8)
    returns = np.array([[1.0, 3.0, 8.0, 100.0]])

    result = group_returns(labels, returns, [1, 2])

    assert list(result.groups[0]) == [2.0, 8.0]
    assert result.universe[0] == pytest.approx(4.0)     # 只算有分組的股票


def test_top_turnover_counts_replaced_stocks_on_rebalance_days() -> None:
    # 每 2 列重新分組：第 0 列 {A, B}，第 2 列 {B, C}（換 1 檔，50%），第 4 列 {C, D}（換 1 檔）
    labels = np.array([[2, 2, 1, 1], [1, 2, 2, 1], [1, 2, 2, 1], [1, 1, 2, 2], [1, 1, 2, 2]], dtype=np.int8)

    turnover = top_turnover(labels, 2, 2)

    assert np.isnan(turnover[0]) and np.isnan(turnover[1]) and np.isnan(turnover[3])
    assert turnover[2] == pytest.approx(0.5)
    assert turnover[4] == pytest.approx(0.5)


def test_top_turnover_keeps_last_list_when_no_groups() -> None:
    # 第 1 次重新分組當天沒有分組，下一次仍與第 0 列比較
    labels = np.array([[2, 2], [0, 0], [2, 1]], dtype=np.int8)

    turnover = top_turnover(labels, 2, 1)

    assert np.isnan(turnover[1])
    assert turnover[2] == pytest.approx(0.0)


def test_neutralize_subtracts_industry_mean_and_merges_small_groups() -> None:
    # 產業 0 有 3 檔、產業 1 有 3 檔，產業 2 只有 1 檔併到「其他」；未分類（−1）也併到「其他」
    factor = np.array([[1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 7.0, 9.0]])
    industries = np.array([[0, 0, 0, 1, 1, 1, 2, -1]])

    result = neutralize(factor, industries, min_group=3)

    assert list(result[0][:6]) == [-1.0, 0.0, 1.0, -10.0, 0.0, 10.0]
    assert result[0, 6] == pytest.approx(-1.0) and result[0, 7] == pytest.approx(1.0)       # (7 + 9) / 2 = 8


def test_neutralize_leaves_missing_values() -> None:
    result = neutralize(np.array([[1.0, NAN, 3.0]]), np.array([[0, 0, 0]]), min_group=1)

    assert result[0, 0] == pytest.approx(-1.0) and np.isnan(result[0, 1]) and result[0, 2] == pytest.approx(1.0)


def test_t_stat_samples_every_horizon_days() -> None:
    ic = np.array([0.1, 9.0, 0.2, 9.0, 0.3, 9.0])       # 起點 0 取 0.1、0.2、0.3；起點 1 全部相同，算不出 t 值，不列入

    t, samples = non_overlapping_t_stat(ic, 2)

    assert samples == 3
    assert t == pytest.approx(0.2 / (0.1 / np.sqrt(3)))


def test_t_stat_averages_over_starting_days() -> None:
    ic = np.array([0.1, 0.4, 0.2, 0.5, 0.3, 0.9])

    t, _ = non_overlapping_t_stat(ic, 2)

    first = 0.2 / (0.1 / np.sqrt(3))
    odd = np.array([0.4, 0.5, 0.9])
    second = odd.mean() / (odd.std(ddof=1) / np.sqrt(3))
    assert t == pytest.approx((first + second) / 2)


def test_t_stat_with_too_few_samples_is_nan() -> None:
    assert np.isnan(non_overlapping_t_stat(np.array([0.1, 0.2]), 2)[0])
