"""逐筆成交 → 1／3／5 分 K（REALTIME_PLAN.md 2.1，L4）。預期值的推導見 tests/fixtures/realtime/ticks_basic.csv。"""

from datetime import date, timedelta

import pytest

from rt_fixtures import SESSION, at, load_trades
from screener.realtime.bars import Trade, bucket_start, build_bars, resample, session_trades

DAY = date(2026, 11, 19)


def _ohlcv(bars):
    return [(bar.start.strftime("%H:%M"), bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in bars]


@pytest.fixture
def included() -> list[Trade]:
    trades, _ = load_trades("ticks_basic", DAY)
    return session_trades(trades, DAY, SESSION).included


def test_session_trades_excludes_trial_and_after_close() -> None:
    trades, _ = load_trades("ticks_basic", DAY)
    result = session_trades(trades, DAY, SESSION)

    assert result.trial == 3                                   # 開盤前、緩漲、收盤前的試撮
    assert [(t.time.strftime("%H:%M:%S"), reason) for t, reason in result.ignored] == [("14:30:00", "收盤之後")]
    assert len(result.included) == 8


def test_session_trades_sorts_and_flags_other_days() -> None:
    late = Trade(time=at(DAY, "09:01:00"), price=10.0, volume=1)
    early = Trade(time=at(DAY, "09:00:10"), price=11.0, volume=1)
    before_open = Trade(time=at(DAY, "08:59:59"), price=11.0, volume=1)    # 沒有標示試撮，但在開盤前
    other_day = Trade(time=at(DAY - timedelta(days=1), "09:30:00"), price=12.0, volume=1)

    result = session_trades([late, early, before_open, other_day], DAY, SESSION)

    assert result.included == [early, late]
    assert {reason for _, reason in result.ignored} == {"開盤前", "不是這個交易日"}


def test_one_minute_bars(included: list[Trade]) -> None:
    bars = build_bars(included, 1, SESSION)

    assert _ohlcv(bars) == [
        ("09:00", 100.0, 101.0, 100.0, 100.5, 65),    # 開盤成交與 09:00:59.999999 都在 09:00
        ("09:01", 101.5, 101.5, 101.5, 101.5, 20),
        ("09:03", 102.0, 102.0, 102.0, 102.0, 30),    # 沒有 09:02：不補量為 0 的 K 棒
        ("09:05", 102.5, 102.5, 102.5, 102.5, 10),
        ("13:24", 101.0, 101.0, 101.0, 101.0, 7),
        ("13:30", 101.5, 101.5, 101.5, 101.5, 60),    # 收盤成交自成一根
    ]
    assert bars[0].amount == pytest.approx(6_512_500)
    assert bars[0].end == at(DAY, "09:01")


def test_three_and_five_minute_bars_align_to_open(included: list[Trade]) -> None:
    assert _ohlcv(build_bars(included, 3, SESSION)) == [
        ("09:00", 100.0, 101.5, 100.0, 101.5, 85),
        ("09:03", 102.0, 102.5, 102.0, 102.5, 40),
        ("13:24", 101.0, 101.0, 101.0, 101.0, 7),
        ("13:30", 101.5, 101.5, 101.5, 101.5, 60),
    ]
    assert _ohlcv(build_bars(included, 5, SESSION)) == [
        ("09:00", 100.0, 102.0, 100.0, 102.0, 115),
        ("09:05", 102.5, 102.5, 102.5, 102.5, 10),
        ("13:20", 101.0, 101.0, 101.0, 101.0, 7),
        ("13:30", 101.5, 101.5, 101.5, 101.5, 60),
    ]


@pytest.mark.parametrize("minutes", [3, 5, 7])
def test_resample_matches_building_from_trades(included: list[Trade], minutes: int) -> None:
    one_minute = build_bars(included, 1, SESSION)

    assert resample(one_minute, minutes, SESSION) == build_bars(included, minutes, SESSION)


@pytest.mark.parametrize("minutes", [1, 3, 5, 7, 60])
def test_close_trade_is_its_own_bar_for_any_minutes(minutes: int) -> None:
    # 13:30 不是 N 的倍數時（例：7 分 K 的 13:29～13:35），收盤成交仍自成一根，不和 13:2x 的成交合併
    trades = [Trade(time=at(DAY, "13:24:00"), price=10.0, volume=1), Trade(time=at(DAY, "13:30:00"), price=11.0, volume=2)]

    bars = build_bars(trades, minutes, SESSION)

    assert bars[-1].start == at(DAY, "13:30") and bars[-1].volume == 2
    assert len(bars) == 2


def test_bucket_start() -> None:
    assert bucket_start(at(DAY, "09:02:59.999999"), 3, SESSION) == at(DAY, "09:00")
    assert bucket_start(at(DAY, "09:03:00"), 3, SESSION) == at(DAY, "09:03")
    assert bucket_start(at(DAY, "13:24:59"), 5, SESSION) == at(DAY, "13:20")
    assert bucket_start(at(DAY, "13:30:00.5"), 5, SESSION) == at(DAY, "13:30")


def test_unsorted_trades_are_rejected() -> None:
    trades = [Trade(time=at(DAY, "09:05:00"), price=10.0, volume=1), Trade(time=at(DAY, "09:01:00"), price=10.0, volume=1)]

    with pytest.raises(ValueError, match="依時間排序"):
        build_bars(trades, 1, SESSION)
