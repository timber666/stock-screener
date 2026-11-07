"""前置條件與觸發規則 E1–E5（PROJECT_PLAN.md 5.2、5.3）。"""

import pytest

from conftest import make_bars, make_context, set_bar
from screener.models import Side, Signal, Trend
from screener.rules.entry.bull_ma5_entry import BullMa5Entry
from screener.rules.trigger.bear_ma5_breakdown import BearMa5Breakdown
from screener.rules.trigger.bull_ma5_breakout import BullMa5Breakout
from screener.rules.trigger.ma_alignment import MaBearAlignment, MaBullAlignment
from screener.rules.trigger.range_breakdown import RangeBreakdown
from screener.rules.trigger.range_breakout_volume import RangeBreakoutVolume
from screener.rules.filters.above_ma20 import AboveMa20
from screener.rules.filters.below_ma20 import BelowMa20


def test_each_rule_belongs_to_one_side() -> None:
    assert {rule.side for rule in (AboveMa20, BullMa5Breakout, RangeBreakoutVolume, MaBullAlignment,
                                   BullMa5Entry)} == {Side.LONG}
    assert {rule.side for rule in (BelowMa20, BearMa5Breakdown, RangeBreakdown, MaBearAlignment)} == {Side.SHORT}

# --- above_ma20 -------------------------------------------------------------

ABOVE_MA = AboveMa20({"ma": 3})


@pytest.mark.parametrize(
    ("closes", "expected"),
    [([10.0, 10.0, 11.0], True), ([10.0, 10.0, 10.0], False), ([10.0, 11.0, 9.0], False), ([10.0, 11.0], False)],
    ids=["above", "equal", "below", "no-ma"],
)
def test_above_ma_filter(closes: list[float], expected: bool) -> None:
    assert ABOVE_MA.passes(make_context(make_bars(closes))) is expected


# --- below_ma20 -------------------------------------------------------------

BELOW_MA = BelowMa20({"ma": 3})


@pytest.mark.parametrize(
    ("closes", "expected"),
    [([10.0, 10.0, 9.0], True), ([10.0, 10.0, 10.0], False), ([10.0, 9.0, 11.0], False), ([10.0, 9.0], False)],
    ids=["below", "equal", "above", "no-ma"],
)
def test_below_ma_filter(closes: list[float], expected: bool) -> None:
    assert BELOW_MA.passes(make_context(make_bars(closes))) is expected


# --- E1 多頭突破五日線紅 K ----------------------------------------------------

E1 = BullMa5Breakout({"trends": ["bull"], "ma": 5})
CROSS = [10.0, 10.0, 10.0, 10.0, 10.0, 9.6, 10.4]      # 昨收 9.6 ≤ MA5 9.92；現價 10.4 > MA5 10.0


def e1_context(closes: list[float] = CROSS, today_open: float = 10.0):
    return make_context(set_bar(make_bars(closes), -1, open=today_open), trend=Trend.BULL)


def test_e1_triggers_on_red_candle_crossing_ma5() -> None:
    signal = E1.evaluate(e1_context())

    assert signal is not None
    assert (signal.rule_id, signal.rule_name) == ("bull_ma5_breakout", "多頭突破五日線紅K")
    assert signal.detail == "現價 10.4 > MA5 10（昨收 9.6 ≤ 9.92）"


@pytest.mark.parametrize("today_open", [10.4, 10.5], ids=["flat-candle", "black-candle"])
def test_e1_requires_red_candle(today_open: float) -> None:
    assert E1.evaluate(e1_context(today_open=today_open)) is None


# --- 多方進場（5.5）：E1 當天、現價過昨高、MA5 上揚 -----------------------------------

ENTRY = BullMa5Entry({"ma": 5})


def test_entry_matches_e1_above_yesterday_high_with_rising_ma5() -> None:
    ctx = e1_context()                                  # 現價 10.4 > 昨高 9.6；MA5 10.0 > 昨日 9.92

    assert ENTRY.matches(ctx, [E1.evaluate(ctx)]) is True


def test_entry_requires_e1_today() -> None:
    ctx = e1_context()
    other = Signal("ma_bull_alignment", "三線多排", "detail", Side.LONG)

    assert ENTRY.matches(ctx, [other]) is False
    assert ENTRY.matches(ctx, []) is False


def test_entry_requires_price_above_yesterday_high() -> None:
    bars = set_bar(set_bar(make_bars(CROSS), -1, open=10.0), -2, high=10.4)     # 昨高 10.4 等於現價
    ctx = make_context(bars, trend=Trend.BULL)
    signal = E1.evaluate(ctx)

    assert signal is not None and ENTRY.matches(ctx, [signal]) is False


def test_entry_requires_rising_ma5() -> None:
    # 5 根前的收盤 10.6 高於現價 10.4：MA5 10.0 < 昨日 10.04，但 E1 仍成立
    ctx = e1_context([10.0, 10.6, 10.0, 10.0, 10.0, 9.6, 10.4])
    signal = E1.evaluate(ctx)

    assert signal is not None and ENTRY.matches(ctx, [signal]) is False


def test_e1_requires_crossing() -> None:
    assert E1.evaluate(e1_context([10.0, 10.0, 10.0, 10.0, 10.0, 10.2, 10.4])) is None


def test_e1_applies_only_to_bull() -> None:
    assert [E1.applies_to(t) for t in Trend] == [True, False, False, False]


# --- E4 空頭跌破五日線黑 K（與 E1 對稱）-----------------------------------------

E4 = BearMa5Breakdown({"trends": ["bear"], "ma": 5})
BREAK = [10.0, 10.0, 10.0, 10.0, 10.0, 10.4, 9.6]      # 昨收 10.4 ≥ MA5 10.08；現價 9.6 < MA5 10.0


def e4_context(closes: list[float] = BREAK, today_open: float = 10.0):
    return make_context(set_bar(make_bars(closes), -1, open=today_open), trend=Trend.BEAR)


def test_e4_triggers_on_black_candle_breaking_ma5() -> None:
    signal = E4.evaluate(e4_context())

    assert signal is not None
    assert (signal.rule_id, signal.rule_name, signal.side) == ("bear_ma5_breakdown", "空頭跌破五日線黑K", Side.SHORT)
    assert signal.detail == "現價 9.6 < MA5 10（昨收 10.4 ≥ 10.08）"


@pytest.mark.parametrize("today_open", [9.6, 9.5], ids=["flat-candle", "red-candle"])
def test_e4_requires_black_candle(today_open: float) -> None:
    assert E4.evaluate(e4_context(today_open=today_open)) is None


def test_e4_requires_breaking() -> None:
    assert E4.evaluate(e4_context([10.0, 10.0, 10.0, 10.0, 10.0, 9.8, 9.6])) is None


def test_e4_applies_only_to_bear() -> None:
    assert [E4.applies_to(t) for t in Trend] == [False, False, True, False]


# --- E2 盤整長紅爆量過前高 ----------------------------------------------------

E2 = RangeBreakoutVolume({"trends": ["range"], "min_body_pct": 3.0, "volume_ratio": 1.5, "high_lookback_days": 40})


def e2_context(before: int = 41, close: float = 103.0, volume: int = 1500, earlier_high: float | None = None):
    """今日之前 before 根收在 100（成交量 1000 張）；今日開 100。"""
    bars = set_bar(make_bars([100.0] * before + [close]), -1, open=100.0, volume=volume)
    if earlier_high is not None:
        bars = set_bar(bars, -10, high=earlier_high)
    return make_context(bars, trend=Trend.RANGE)


def test_e2_triggers_on_long_red_volume_spike_above_prior_high() -> None:
    signal = E2.evaluate(e2_context())

    assert signal is not None
    assert signal.rule_name == "盤整長紅爆量過前高"
    assert signal.detail == "實體 3.0%，量比 1.50x，突破 40 日高 100"


def test_e2_shows_volume_ratio_to_two_decimals() -> None:
    # 1462 / 1000 = 1.462：顯示到小數一位會變成 1.5x，看起來像達到 1.5 倍
    rule = RangeBreakoutVolume({"trends": ["range"], "min_body_pct": 3.0, "volume_ratio": 1.3,
                                "high_lookback_days": 40})

    signal = rule.evaluate(e2_context(volume=1462))

    assert signal is not None and "量比 1.46x" in signal.detail


@pytest.mark.parametrize(
    "kwargs",
    [
        {"close": 102.9},                   # 實體 2.9% < 3%
        {"volume": 1499},                   # 量比不到 1.5 倍
        {"earlier_high": 103.0},            # 現價 103 沒有大於前高 103
        {"before": 39},                     # 今日之前不足 40 根，算不出前高（D23）
    ],
    ids=["small-body", "low-volume", "not-above-high", "not-enough-history"],
)
def test_e2_requires_every_condition(kwargs: dict) -> None:
    assert E2.evaluate(e2_context(**kwargs)) is None


def test_e2_does_not_trigger_when_yesterday_volume_is_zero() -> None:
    # 昨日不到 500 股會四捨五入成 0 張（例：掛牌前最後一天的興櫃交易）：算不出量比，不觸發（D39）
    ctx = make_context(set_bar(e2_context().bars, -2, volume=0), trend=Trend.RANGE)

    assert E2.evaluate(ctx) is None


def test_e2_prior_high_window_is_last_40_bars() -> None:
    ctx = e2_context()
    ctx = make_context(set_bar(ctx.bars, -42, high=200.0), trend=Trend.RANGE)    # 第 41 根前，不在區間內

    assert E2.evaluate(ctx) is not None


# --- E6 盤整長黑破前低（與 E2 對稱，但不看量）------------------------------------

E6 = RangeBreakdown({"trends": ["range"], "min_body_pct": 3.0, "low_lookback_days": 40})


def e6_context(before: int = 41, close: float = 97.0, volume: int = 1000, earlier_low: float | None = None):
    """今日之前 before 根收在 100（成交量 1000 張）；今日開 100。"""
    bars = set_bar(make_bars([100.0] * before + [close]), -1, open=100.0, volume=volume)
    if earlier_low is not None:
        bars = set_bar(bars, -10, low=earlier_low)
    return make_context(bars, trend=Trend.RANGE)


def test_e6_triggers_on_long_black_below_prior_low() -> None:
    signal = E6.evaluate(e6_context())

    assert signal is not None
    assert (signal.rule_id, signal.rule_name, signal.side) == ("range_breakdown", "盤整長黑破前低", Side.SHORT)
    assert signal.detail == "實體 3.0%，跌破 40 日低 100"


def test_e6_does_not_look_at_volume() -> None:
    assert E6.evaluate(e6_context(volume=100)) is not None          # 量縮也觸發


@pytest.mark.parametrize(
    "kwargs",
    [
        {"close": 97.1},                    # 實體 2.9% < 3%
        {"earlier_low": 97.0},              # 現價 97 沒有小於前低 97
        {"before": 39},                     # 今日之前不足 40 根，算不出前低（D23）
    ],
    ids=["small-body", "not-below-low", "not-enough-history"],
)
def test_e6_requires_long_black_below_prior_low(kwargs: dict) -> None:
    assert E6.evaluate(e6_context(**kwargs)) is None


def test_e6_applies_only_to_range() -> None:
    assert [E6.applies_to(t) for t in Trend] == [False, True, False, False]


# --- E3 三線多排 -------------------------------------------------------------

E3 = MaBullAlignment({"trends": [], "mas": [5, 10, 20], "only_on_formation": True})
E3_EVERY_DAY = MaBullAlignment({"trends": [], "mas": [5, 10, 20], "only_on_formation": False})
FORMED_TODAY = [100.0] * 20 + [110.0]           # 今日 MA5 102 > MA10 101 > MA20 100.5；昨日三條都是 100
STILL_ALIGNED = FORMED_TODAY + [111.0]


def test_e3_triggers_on_formation_day() -> None:
    signal = E3.evaluate(make_context(make_bars(FORMED_TODAY)))

    assert signal is not None
    assert signal.detail == "MA5 102 > MA10 101 > MA20 100.5"


def test_e3_does_not_repeat_while_aligned() -> None:
    ctx = make_context(make_bars(STILL_ALIGNED))

    assert E3.evaluate(ctx) is None
    assert E3_EVERY_DAY.evaluate(ctx) is not None


def test_e3_needs_yesterdays_ma_to_confirm_formation() -> None:
    ctx = make_context(make_bars([100.0] * 19 + [110.0]))   # 今日剛好 20 根，昨日沒有 MA20（D23）

    assert E3.evaluate(ctx) is None
    assert E3_EVERY_DAY.evaluate(ctx) is not None


def test_e3_requires_strict_order() -> None:
    assert E3.evaluate(make_context(make_bars([100.0] * 21))) is None


def test_empty_trends_apply_to_every_trend_including_unknown() -> None:
    assert all(E3.applies_to(t) for t in Trend)
    assert all(E5.applies_to(t) for t in Trend)


# --- E5 三線空排（與 E3 對稱）--------------------------------------------------

E5 = MaBearAlignment({"trends": [], "mas": [5, 10, 20], "only_on_formation": True})
E5_EVERY_DAY = MaBearAlignment({"trends": [], "mas": [5, 10, 20], "only_on_formation": False})
FORMED_TODAY_BEAR = [100.0] * 20 + [90.0]       # 今日 MA5 98 < MA10 99 < MA20 99.5；昨日三條都是 100


def test_e5_triggers_on_formation_day() -> None:
    signal = E5.evaluate(make_context(make_bars(FORMED_TODAY_BEAR)))

    assert signal is not None
    assert (signal.rule_name, signal.side) == ("三線空排", Side.SHORT)
    assert signal.detail == "MA5 98 < MA10 99 < MA20 99.5"


def test_e5_does_not_repeat_while_aligned() -> None:
    ctx = make_context(make_bars(FORMED_TODAY_BEAR + [89.0]))

    assert E5.evaluate(ctx) is None
    assert E5_EVERY_DAY.evaluate(ctx) is not None


def test_e5_needs_yesterdays_ma_to_confirm_formation() -> None:
    ctx = make_context(make_bars([100.0] * 19 + [90.0]))

    assert E5.evaluate(ctx) is None
    assert E5_EVERY_DAY.evaluate(ctx) is not None


def test_bull_and_bear_alignment_are_exclusive() -> None:
    assert E5.evaluate(make_context(make_bars(FORMED_TODAY))) is None
    assert E3.evaluate(make_context(make_bars(FORMED_TODAY_BEAR))) is None
