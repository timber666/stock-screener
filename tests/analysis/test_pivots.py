"""轉折點（PROJECT_PLAN.md 4.1）。直接給定收盤價與均線，讓每個情境的觸碰點一目了然。"""

import math

import pandas as pd
import pytest

from screener.analysis.pivots import find_pivots

NAN = math.nan


def find(closes: list[float], ma: list[float]) -> list[tuple[int, str, float]]:
    """以位置表示轉折點：(第幾根, 種類, 收盤價)。"""
    index = pd.Index(pd.bdate_range("2026-01-05", periods=len(closes)).date, name="date")
    dates = list(index)
    pivots = find_pivots(pd.Series(closes, index=index, dtype=float), pd.Series(ma, index=index, dtype=float))
    return [(dates.index(p.date), p.kind, p.close) for p in pivots]


def test_peak_is_highest_close_from_interval_start_to_touch() -> None:
    #           0   1   2   3   4
    closes = [10, 12, 15, 14, 11]
    ma = [9, 10, 11, 12, 12]                 # 第 4 根收盤 ≤ 均線 → 確認頭

    assert find(closes, ma) == [(2, "peak", 15)]


def test_next_range_starts_day_after_pivot_and_includes_touch_day() -> None:
    #           0   1   2   3  4  5  6   7
    closes = [10, 12, 15, 14, 7, 9, 8, 10]
    ma = [9, 10, 11, 12, 12, 11, 10, 9.5]    # 第 4 根確認頭；第 7 根站上 → 確認底

    # 底的區間是頭（第 2 根）的隔天到第 7 根，因此確認頭的第 4 根也在區間內
    assert find(closes, ma) == [(2, "peak", 15), (4, "trough", 7)]


def test_touching_ma_exactly_confirms_pivot() -> None:
    #           0   1   2  3   4
    closes = [10, 12, 11, 9, 10]
    ma = [9, 11, 11, 10, 10]                 # 第 2 根收盤 = 均線 → 頭；第 4 根收盤 = 均線 → 底

    assert find(closes, ma) == [(1, "peak", 12), (3, "trough", 9)]


def test_ties_take_the_later_bar() -> None:
    #           0   1   2   3   4  5   6
    closes = [10, 12, 12, 11, 8, 8, 10]
    ma = [9, 10, 11, 11.5, 10, 9, 9]

    assert find(closes, ma) == [(2, "peak", 12), (5, "trough", 8)]


def test_first_bar_only_sets_state_and_can_be_the_pivot() -> None:
    # 第 0 根在均線下方 → 初始狀態「下方」，不是觸碰；第 2 根站上才確認底，區間從第 0 根開始
    assert find([8, 9, 11], [9, 10, 10]) == [(0, "trough", 8)]


def test_first_bar_equal_to_ma_starts_below() -> None:
    # 收盤 = 均線不算「上方」，所以第 1 根跌到 9 不會確認頭
    assert find([10, 9, 11], [10, 10, 10]) == [(1, "trough", 9)]


def test_bars_before_ma_is_available_belong_to_first_range() -> None:
    # 前兩根沒有均線（歷史不足）；第 2 根才決定初始狀態，但第一個區間仍從計算區間起點開始
    assert find([20, 11, 12, 13, 18], [NAN, NAN, 14, 14, 15]) == [(1, "trough", 11)]


def test_last_segment_without_touch_is_not_confirmed() -> None:
    assert find([10, 12, 15, 9, 8], [9, 10, 11, 11, 10]) == [(2, "peak", 15)]


def test_no_ma_gives_no_pivots() -> None:
    assert find([10, 9, 8], [NAN, NAN, NAN]) == []


def test_mismatched_index_is_rejected() -> None:
    closes = pd.Series([1.0, 2.0], index=[1, 2])
    ma = pd.Series([1.0, 2.0], index=[2, 3])

    with pytest.raises(ValueError, match="索引不一致"):
        find_pivots(closes, ma)
