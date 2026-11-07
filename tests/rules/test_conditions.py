"""名詞定義（PROJECT_PLAN.md 第 3 節）。"""

import pytest

from conftest import make_bars, make_context, set_bar
from screener.rules.conditions import (
    body_pct,
    crosses_above_ma,
    crosses_below_ma,
    is_above_ma,
    is_above_yesterday_high,
    is_below_ma,
    is_black,
    is_long_black,
    is_long_red,
    is_ma_rising,
    is_red,
    is_strictly_ascending,
    is_strictly_descending,
    is_volume_spike,
    ma_values,
    prior_high,
    prior_low,
    volume_ratio,
)


def two_days(prev_close: float, open_: float, close: float, prev_volume: int = 1000, volume: int = 1000):
    bars = make_bars([prev_close, close])
    return make_context(set_bar(set_bar(bars, -2, volume=prev_volume), -1, open=open_, volume=volume))


# --- 過昨高、MA 上揚（5.5）------------------------------------------------------

@pytest.mark.parametrize(("close", "expected"), [(10.6, True), (10.5, False), (10.4, False)], ids=["above", "equal", "below"])
def test_above_yesterday_high_uses_high_not_close(close: float, expected: bool) -> None:
    bars = set_bar(make_bars([10.0, close]), -2, high=10.5)

    assert is_above_yesterday_high(make_context(bars)) is expected


def test_above_yesterday_high_needs_yesterday() -> None:
    assert is_above_yesterday_high(make_context(make_bars([10.0]))) is False


@pytest.mark.parametrize(
    ("closes", "expected"),
    [([10.0] * 5 + [11.0], True), ([10.0] * 6, False), ([10.0] * 5 + [9.0], False), ([10.0] * 5, False), ([10.0], False)],
    ids=["rising", "flat", "falling", "no-yesterday-ma", "one-bar"],
)
def test_ma_rising_compares_today_with_yesterday(closes: list[float], expected: bool) -> None:
    assert is_ma_rising(make_context(make_bars(closes)), 5) is expected


# --- 紅 K、長紅 --------------------------------------------------------------

@pytest.mark.parametrize(("open_", "close", "expected"), [(10.0, 10.1, True), (10.0, 10.0, False), (10.1, 10.0, False)])
def test_red_candle_requires_close_above_open(open_: float, close: float, expected: bool) -> None:
    assert is_red(two_days(10.0, open_, close)) is expected


@pytest.mark.parametrize(("open_", "close", "expected"), [(10.1, 10.0, True), (10.0, 10.0, False), (10.0, 10.1, False)])
def test_black_candle_requires_close_below_open(open_: float, close: float, expected: bool) -> None:
    assert is_black(two_days(10.0, open_, close)) is expected


@pytest.mark.parametrize(
    ("prev_close", "open_", "close", "expected"),
    [
        (100.0, 100.0, 103.0, True),        # 實體剛好 3%
        (100.0, 100.0, 102.99, False),
        (10.0, 10.05, 10.35, True),         # 剛好 3%；直接相除會得到 2.99999…%
        (100.0, 104.0, 107.0, True),        # 跳空開高，實體仍以昨收為分母
        (100.0, 103.0, 100.0, False),       # 黑 K
    ],
)
def test_long_red_body_is_measured_against_previous_close(
    prev_close: float, open_: float, close: float, expected: bool
) -> None:
    assert is_long_red(two_days(prev_close, open_, close), min_body_pct=3.0) is expected


@pytest.mark.parametrize(
    ("prev_close", "open_", "close", "expected"),
    [
        (100.0, 100.0, 97.0, True),         # 實體剛好 3%
        (100.0, 100.0, 97.01, False),
        (10.0, 10.35, 10.05, True),         # 剛好 3%；直接相減會得到 2.99999…%
        (100.0, 96.0, 93.0, True),          # 跳空開低，實體仍以昨收為分母
        (100.0, 97.0, 100.0, False),        # 紅 K
    ],
)
def test_long_black_body_is_measured_against_previous_close(
    prev_close: float, open_: float, close: float, expected: bool
) -> None:
    assert is_long_black(two_days(prev_close, open_, close), min_body_pct=3.0) is expected


def test_body_pct() -> None:
    assert body_pct(two_days(100.0, 100.0, 103.5)) == 3.5
    assert body_pct(make_context(make_bars([100.0]))) is None


# --- 爆量 -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("prev_volume", "volume", "ratio", "expected"),
    [
        (1000, 1500, 1.5, True),            # 剛好 1.5 倍
        (1000, 1499, 1.5, False),
        (100, 110, 1.1, True),              # 100 × 1.1 直接相乘會得到 110.00000000000001
        (0, 800, 1.5, False),               # 昨日 0 張：算不出量比，不成立（D39）
    ],
)
def test_volume_spike(prev_volume: int, volume: int, ratio: float, expected: bool) -> None:
    ctx = two_days(10.0, 10.0, 10.0, prev_volume=prev_volume, volume=volume)

    assert is_volume_spike(ctx, ratio) is expected


def test_volume_ratio() -> None:
    assert volume_ratio(two_days(10.0, 10.0, 10.0, prev_volume=1000, volume=1800)) == 1.8


def test_single_bar_has_no_yesterday_comparisons() -> None:
    ctx = make_context(make_bars([10.0]))

    assert not is_long_red(ctx, 3.0)
    assert not is_volume_spike(ctx, 1.5)
    assert not crosses_above_ma(ctx, 1)
    assert volume_ratio(ctx) is None


# --- 突破、站上均線 ---------------------------------------------------------

# MA5：第 5 根（昨日）9.92，第 6 根（今日）10.0
CROSS = [10.0, 10.0, 10.0, 10.0, 10.0, 9.6, 10.4]


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        (CROSS, True),                                      # 昨收 9.6 ≤ 9.92，現價 10.4 > 10.0
        ([10.0, 10.0, 10.0, 10.0, 10.0, 10.2, 10.4], False),  # 昨收已在 MA5 之上
        ([10.0, 10.0, 10.0, 10.0, 10.0, 9.6, 9.9], False),    # 現價 9.9 = 今日 MA5 9.9，不算突破
        ([10.0, 10.0, 10.0, 9.6, 10.4], False),               # 昨日只有 4 根，沒有 MA5
    ],
)
def test_crosses_above_ma(closes: list[float], expected: bool) -> None:
    assert crosses_above_ma(make_context(make_bars(closes)), 5) is expected


def test_crossing_when_yesterday_close_equals_ma() -> None:
    # 昨收 = 昨日 MA5 也算「昨收 ≤ 昨日 MA5」
    assert crosses_above_ma(make_context(make_bars([10.0] * 6 + [10.5])), 5)


# MA5：第 5 根（昨日）10.08，第 6 根（今日）10.0
BREAK = [10.0, 10.0, 10.0, 10.0, 10.0, 10.4, 9.6]


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        (BREAK, True),                                      # 昨收 10.4 ≥ 10.08，現價 9.6 < 10.0
        ([10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6], False),    # 昨收已在 MA5 之下
        ([10.0, 10.0, 10.0, 10.0, 10.0, 10.4, 10.1], False),  # 現價 10.1 = 今日 MA5 10.1，不算跌破
        ([10.0] * 6 + [9.5], True),                         # 昨收 = 昨日 MA5 也算「昨收 ≥ 昨日 MA5」
        ([10.0, 10.0, 10.0, 10.4, 9.6], False),               # 昨日只有 4 根，沒有 MA5
    ],
)
def test_crosses_below_ma(closes: list[float], expected: bool) -> None:
    assert crosses_below_ma(make_context(make_bars(closes)), 5) is expected


def test_above_ma_is_strict_and_needs_enough_bars() -> None:
    assert is_above_ma(make_context(make_bars([10.0, 10.0, 11.0])), 3)
    assert not is_above_ma(make_context(make_bars([10.0, 10.0, 10.0])), 3)
    assert not is_above_ma(make_context(make_bars([10.0, 11.0])), 3)     # 沒有 MA3（D23）


def test_below_ma_is_strict_and_needs_enough_bars() -> None:
    assert is_below_ma(make_context(make_bars([10.0, 10.0, 9.0])), 3)
    assert not is_below_ma(make_context(make_bars([10.0, 10.0, 10.0])), 3)  # 等於月線，空方也不通過
    assert not is_below_ma(make_context(make_bars([10.0, 9.0])), 3)


# --- 前高 -------------------------------------------------------------------

def test_prior_high_excludes_today_and_uses_last_n_bars() -> None:
    bars = set_bar(set_bar(make_bars([10.0] * 7), 0, high=99.0), 3, high=12.0)
    bars = set_bar(bars, -1, high=50.0)                      # 今日不計入

    assert prior_high(make_context(bars), 5) == 12.0         # 第 0 根在最近 5 根之外


def test_prior_low_excludes_today_and_uses_last_n_bars() -> None:
    bars = set_bar(set_bar(make_bars([10.0] * 7), 0, low=1.0), 3, low=8.0)
    bars = set_bar(bars, -1, low=2.0)                        # 今日不計入

    assert prior_low(make_context(bars), 5) == 8.0           # 第 0 根在最近 5 根之外
    assert prior_low(make_context(bars), 7) is None          # 今日之前只有 6 根（D23）


def test_prior_high_needs_enough_bars_before_today() -> None:
    ctx = make_context(make_bars([10.0] * 5))                # 今日之前只有 4 根

    assert prior_high(ctx, 4) == 10.0
    assert prior_high(ctx, 5) is None                        # D23


# --- 均線排列 ---------------------------------------------------------------

def test_ma_values_today_and_yesterday() -> None:
    ctx = make_context(make_bars([1.0, 2.0, 3.0, 4.0]))

    assert ma_values(ctx, [1, 2]) == [4.0, 3.5]
    assert ma_values(ctx, [1, 2], days_ago=1) == [3.0, 2.5]
    assert ma_values(ctx, [1, 4], days_ago=1) is None        # 昨日沒有 MA4


@pytest.mark.parametrize(("values", "expected"), [([3, 2, 1], True), ([3, 2, 2], False), ([1, 2, 3], False)])
def test_strictly_descending(values: list[float], expected: bool) -> None:
    assert is_strictly_descending(values) is expected


@pytest.mark.parametrize(("values", "expected"), [([1, 2, 3], True), ([1, 2, 2], False), ([3, 2, 1], False)])
def test_strictly_ascending(values: list[float], expected: bool) -> None:
    assert is_strictly_ascending(values) is expected
