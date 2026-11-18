"""Shioaji 歷史分 K、逐筆成交與錄製的即時成交的解析與轉換（REALTIME_PLAN.md 第 3 節、7.1 的 T5、T6）。"""

from datetime import date, datetime, time

import pytest

from fake_shioaji import to_ts
from rt_fixtures import SESSION, at
from screener.realtime.history import (kbars_to_bars, parse_kbars, parse_ticks, split_days, summarize_kbars,
                                       summarize_ticks, ticks_to_trades)
from screener.realtime.ticks import TickFormatError, trade_from_tick, trades_by_code
from screener.realtime.timestamps import from_shioaji_ts

DAY = date(2026, 11, 18)


def _kbars(rows: list[tuple[str, float, int]]) -> dict[str, list]:
    """(標示時間, 收盤, 量) → Shioaji kbars 的欄位結構。"""
    return {"ts": [to_ts(at(DAY, label)) for label, _, _ in rows], "Open": [c for _, c, _ in rows],
            "High": [c for _, c, _ in rows], "Low": [c for _, c, _ in rows], "Close": [c for _, c, _ in rows],
            "Volume": [v for _, _, v in rows], "Amount": [c * v * 1000 for _, c, v in rows]}


def test_shioaji_ts_is_local_time_encoded_as_utc() -> None:
    # T1：4977 2026-11-18 第一根的 ts
    assert from_shioaji_ts(1794992460000000000) == datetime(2026, 11, 18, 9, 1)


def test_kbars_to_bars_shifts_labels_and_drops_filler() -> None:
    # T5：Shioaji 以結束時間標示（09:01 = 09:00 那根），收盤集合競價期間與沒有成交的分鐘補上量為 0 的 K 棒，
    # 13:30 那根是收盤成交
    rows = parse_kbars(_kbars([("09:01", 100.0, 5), ("09:02", 100.0, 0), ("09:03", 101.0, 2), ("13:25", 101.0, 1),
                               ("13:26", 101.0, 0), ("13:29", 101.0, 0), ("13:30", 102.0, 9)]))

    bars = kbars_to_bars(rows, SESSION)

    assert [(bar.start.strftime("%H:%M"), bar.close, bar.volume) for bar in bars] == [
        ("09:00", 100.0, 5), ("09:02", 101.0, 2), ("13:24", 101.0, 1), ("13:30", 102.0, 9)]
    assert bars[0].amount == 500_000


def test_summarize_kbars_counts_filler_bars() -> None:
    rows = parse_kbars(_kbars([("09:01", 100.0, 5), ("09:02", 100.0, 0), ("13:26", 100.0, 0), ("13:30", 101.0, 3)]))

    summary = summarize_kbars(rows, (time(13, 26), time(13, 29)))[DAY]

    assert summary["count"] == 4 and summary["first"] == "09:01:00" and summary["last"] == "13:30:00"
    assert summary["zero_volume_closing_call"] == 1 and summary["zero_volume_intraday"] == ["09:02:00"]
    assert (summary["open"], summary["close"], summary["volume"]) == (100.0, 101.0, 8)


def test_ticks_parse_and_summary() -> None:
    moments = [at(DAY, "09:00:00.776560"), at(DAY, "13:24:59.651267"), at(DAY, "13:30:00"), at(DAY, "14:30:00")]
    columns = {"ts": [to_ts(m) for m in moments], "close": [278.0, 251.0, 253.5, 253.5], "volume": [2477, 1, 4731, 124],
               "bid_price": [0.0] * 4, "bid_volume": [0] * 4, "ask_price": [0.0] * 4, "ask_volume": [0] * 4,
               "tick_type": [1, 2, 1, 0]}

    rows = parse_ticks(columns)
    summary = summarize_ticks(rows, SESSION.open, SESSION.closing_call, SESSION.close)

    assert rows[0].time == moments[0]
    assert summary["at_open_second"] == 1 and summary["at_or_after_close"] == 2 and summary["closing_call"] == 0
    assert summary["with_microseconds"] == 2 and summary["volume"] == 7333
    assert [t.volume for t in ticks_to_trades(rows)] == [2477, 1, 4731, 124]


def test_split_days() -> None:
    rows = parse_kbars(_kbars([("09:01", 100.0, 1)]))
    bars = kbars_to_bars(rows, SESSION)

    assert list(split_days(bars)) == [DAY]


# --- 錄製的即時成交 -----------------------------------------------------------------

def _tick(**values):
    data = {"code": "4977", "datetime": "2026-11-19T09:00:07.599550", "close": "187.0", "volume": 333,
            "simtrade": False, "intraday_odd": False}
    data.update(values)
    return data


def test_trade_from_tick() -> None:
    trade = trade_from_tick(_tick())

    assert (trade.time, trade.price, trade.volume, trade.is_trial) == (datetime(2026, 11, 19, 9, 0, 7, 599550), 187.0,
                                                                       333, False)
    assert trade_from_tick(_tick(simtrade=True)).is_trial
    assert trade_from_tick(_tick(intraday_odd=True)) is None          # 盤中零股不計入


def test_trade_from_tick_reports_missing_fields() -> None:
    with pytest.raises(TickFormatError, match="缺少欄位 close"):
        trade_from_tick({"code": "4977", "datetime": "2026-11-19T09:00:00", "volume": 1})


def test_trades_by_code_reads_only_wanted_ticks() -> None:
    records = [{"type": "tick", "data": _tick()}, {"type": "bidask", "data": {"code": "4977"}},
               {"type": "tick", "data": _tick(code="1303")}, {"type": "event", "data": {}}]

    trades = trades_by_code(records, ["4977", "4951"])

    assert len(trades["4977"]) == 1 and trades["4951"] == []
