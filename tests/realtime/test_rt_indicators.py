"""四條線（REALTIME_PLAN.md 2.2–2.4，L5–L7、L13）。預期值的推導見 tests/fixtures/realtime/ 的 CSV 開頭。"""

from datetime import date, timedelta

import pytest

from rt_fixtures import SESSION, at, load_trades
from screener.realtime.bars import Trade, bucket_start, build_bars, session_trades
from screener.realtime.indicators import (drawn_value, live_moving_average_by_bar, live_moving_average_by_trade,
                                          moving_average, value_at, volume_profile_by_bar, volume_profile_by_trade,
                                          vwap_by_bar, vwap_by_trade)

FRIDAY = date(2026, 11, 13)
MONDAY = date(2026, 11, 16)


def _bars(trades, day, minutes):
    return build_bars(session_trades(trades, day, SESSION).included, minutes, SESSION)


def _values(points):
    return {point.bar_start.strftime("%H:%M"): point.value for point in points}


# --- 3MA、5MA -------------------------------------------------------------------

@pytest.fixture
def tw_university() -> tuple[list[Trade], list[Trade]]:
    return load_trades("ticks_tw_university", MONDAY, prev_day=FRIDAY)


def test_matches_tw_university_example(tw_university: tuple[list[Trade], list[Trade]]) -> None:
    # 2.3：依 2.2 的定義算出的值，四捨五入到台股大學堂顯示的小數位數後相同
    today, previous = tw_university
    ma3 = _values(moving_average(_bars(today, MONDAY, 3), 3, prior=_bars(previous, FRIDAY, 3)))
    ma5 = _values(moving_average(_bars(today, MONDAY, 5), 5, prior=_bars(previous, FRIDAY, 5)))

    assert ma3["09:00"] == pytest.approx(56.6)
    assert round(ma3["09:03"], 1) == 57.0
    assert ma3["09:06"] == pytest.approx(57.4)
    assert round(ma5["09:20"], 1) == 57.3
    assert ma5["09:15"] == ma5["09:20"] == pytest.approx(57.32)      # 09:15～09:24 是平的


def test_continues_from_previous_trading_day_across_weekend(tw_university: tuple[list[Trade], list[Trade]]) -> None:
    # 週一 09:00 的 3MA 用的是週五最後兩根 3 分 K（13:24 收 56.3、13:30 收盤 56.5）
    today, previous = tw_university
    prior = _bars(previous, FRIDAY, 3)
    assert [bar.start.strftime("%H:%M") for bar in prior[-2:]] == ["13:24", "13:30"]

    first = moving_average(_bars(today, MONDAY, 3), 3, prior=prior)[0]

    assert first.value == pytest.approx((56.3 + 56.5 + 57.0) / 3)


def _every_minute(day: date, until: str) -> list[Trade]:
    """09:00 起每分鐘一筆成交，價格 100、101、102……"""
    trades = []
    moment = at(day, "09:00:30")
    while moment < at(day, until):
        trades.append(Trade(time=moment, price=100.0 + len(trades), volume=1))
        moment += timedelta(minutes=1)
    return trades


def test_not_enough_bars_without_previous_day() -> None:
    # L13：新掛牌第一天沒有前一交易日的 K 棒，3MA 要到 09:09、5MA 要到 09:25 才有值；之前為 None（不畫、規則不成立）
    trades = _every_minute(MONDAY, "09:30")
    ma3 = moving_average(_bars(trades, MONDAY, 3), 3)
    ma5 = moving_average(_bars(trades, MONDAY, 5), 5)

    first3 = next(point for point in ma3 if point.value is not None)
    first5 = next(point for point in ma5 if point.value is not None)
    assert (first3.bar_start, first3.available_at) == (at(MONDAY, "09:06"), at(MONDAY, "09:09"))
    assert (first5.bar_start, first5.available_at) == (at(MONDAY, "09:20"), at(MONDAY, "09:25"))
    assert all(point.value is None for point in ma3[:2]) and all(point.value is None for point in ma5[:4])


def test_value_is_known_only_after_bar_completes(tw_university: tuple[list[Trade], list[Trade]]) -> None:
    # 2.4：09:06～09:08 那一階（57.4）要等 09:08 那根走完（09:09）才知道；之前規則只能用 09:03 那一階
    today, previous = tw_university
    ma3 = moving_average(_bars(today, MONDAY, 3), 3, prior=_bars(previous, FRIDAY, 3))
    step_0903 = ma3[1].value

    assert value_at(ma3, at(MONDAY, "09:08:59")) == step_0903
    assert value_at(ma3, at(MONDAY, "09:09")) == pytest.approx(57.4)
    assert value_at(ma3, at(MONDAY, "09:02:59")) is None              # 第一根還沒走完
    assert drawn_value(ma3, at(MONDAY, "09:07")) == pytest.approx(57.4)  # 歷史圖上畫在 09:06～09:08


def test_moving_average_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        moving_average([], 0)
    with pytest.raises(ValueError):
        live_moving_average_by_trade([], 3, 0, SESSION)


def test_live_value_follows_price_until_bar_completes(tw_university: tuple[list[Trade], list[Trade]]) -> None:
    # L5（2026-11-19）：形成中的 3 分 K 以現價代入，台股大學堂的垂直線跟著現價上下移動；這根走完時等於走完的值
    today, previous = tw_university
    trades = session_trades(today, MONDAY, SESSION).included
    points = live_moving_average_by_trade(trades, 3, 3, SESSION, prior=_bars(previous, FRIDAY, 3))
    live = {point.time.strftime("%H:%M:%S"): point.value for point in points}

    # 09:00 那根的成交：前一交易日最後兩根（56.3、56.5）與現價 57.0，當天第一根走完前也有值
    assert live["09:02:10"] == pytest.approx((56.3 + 56.5 + 57.0) / 3)
    assert live["09:04:10"] == pytest.approx((56.5 + 57.0 + 56.9) / 3)    # 09:03 那根形成中，現價 56.9 → 56.80
    assert live["09:05:10"] == pytest.approx((56.5 + 57.0 + 57.4) / 3)    # 現價 57.4 → 56.97，即 09:03 那根走完的值


def test_live_value_at_bar_end_equals_completed_value(tw_university: tuple[list[Trade], list[Trade]]) -> None:
    # 每根 N 分 K 最後一根有成交的 1 分 K 走完時，即時值 = 這根 N 分 K 走完的值（歷史圖上的那一階）
    today, previous = tw_university
    minute_bars = _bars(today, MONDAY, 1)
    for minutes, period in ((3, 3), (5, 5)):
        prior = _bars(previous, FRIDAY, minutes)
        live = live_moving_average_by_bar(minute_bars, minutes, period, SESSION, prior=prior)
        completed = moving_average(_bars(today, MONDAY, minutes), period, prior=prior)

        at_bucket_end = {bucket_start(bar.start, minutes, SESSION): point.value for bar, point in zip(minute_bars, live)}
        assert at_bucket_end == pytest.approx({point.bar_start: point.value for point in completed})


def test_live_value_needs_enough_bars_without_previous_day() -> None:
    # L13：沒有前一交易日時，前兩根 3 分 K 走完、第三根有第一筆成交（09:06:30）才有即時值
    live = live_moving_average_by_trade(_every_minute(MONDAY, "09:30"), 3, 3, SESSION)

    first = next(point for point in live if point.value is not None)
    assert first.time == at(MONDAY, "09:06:30")
    assert first.value == pytest.approx((102 + 105 + 106) / 3)            # 09:00、09:03 兩根的收盤與現價


# --- 均價線 -----------------------------------------------------------------------

def test_vwap_is_cumulative_amount_over_volume() -> None:
    trades, _ = load_trades("ticks_basic", MONDAY)
    included = session_trades(trades, MONDAY, SESSION).included

    by_trade = vwap_by_trade(included)
    by_bar = vwap_by_bar(build_bars(included, 1, SESSION))

    assert by_trade[0].value == 100.0
    assert by_trade[2].value == pytest.approx(6512.5 / 65)            # 09:00 那根的最後一筆
    assert by_bar[0].time == at(MONDAY, "09:01")
    assert by_bar[0].value == by_trade[2].value
    assert by_bar[-1].value == by_trade[-1].value                     # 收盤後的值相同
    total = 6512.5 + 101.5 * 20 + 102.0 * 30 + 102.5 * 10 + 101.0 * 7 + 101.5 * 60
    assert by_bar[-1].value == pytest.approx(total / 192)


# --- VP -------------------------------------------------------------------------

@pytest.fixture
def vp_trades() -> list[Trade]:
    trades, _ = load_trades("ticks_vp", MONDAY)
    return session_trades(trades, MONDAY, SESSION).included


def test_volume_profile_during_the_day(vp_trades: list[Trade]) -> None:
    values = [point.value for point in volume_profile_by_trade(vp_trades, SESSION)]

    assert values == [None, None, 50.5, 50.5, 52.0, 52.5, 52.0, 52.0, 53.5]


def test_volume_profile_at_each_completed_bar(vp_trades: list[Trade]) -> None:
    points = volume_profile_by_bar(build_bars(vp_trades, 1, SESSION))

    assert [(point.time.strftime("%H:%M"), point.value) for point in points] == [
        ("09:01", 50.5), ("09:02", 50.5), ("09:03", 52.0), ("09:04", 52.0), ("13:31", 53.5)]


def test_volume_profile_by_trade_ends_where_by_bar_ends(vp_trades: list[Trade]) -> None:
    # 量只會增加：形成中的 K 棒成為最大量後，走完時就固定在它的收盤價
    by_trade = volume_profile_by_trade(vp_trades, SESSION)
    by_bar = volume_profile_by_bar(build_bars(vp_trades, 1, SESSION))

    assert by_trade[-1].value == by_bar[-1].value
