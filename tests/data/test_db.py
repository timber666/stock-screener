import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.models import (ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult, Listing,
                             Market, PriceSubstitute, Quote, ScanRun, Side, SignalRecord, Trend)


def bar(code: str, day: str, close: float = 100.0, volume: int = 1000) -> DailyBar:
    return DailyBar(code=code, date=date.fromisoformat(day), open=99.0, high=101.0, low=98.0,
                    close=close, volume=volume)


def test_creates_database_and_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "screener.db"

    with SqlitePriceStore(path) as store:
        assert store.latest_date("2330") is None

    assert path.is_file()


def test_bars_round_trip_sorted_by_date(tmp_path: Path) -> None:
    bars = [bar("2330", "2026-10-30"), bar("2330", "2026-10-28"), bar("2330", "2026-10-29")]

    with SqlitePriceStore(tmp_path / "screener.db") as store:
        assert store.upsert_bars(bars) == 3
        stored = store.get_bars("2330")

    assert stored == sorted(bars, key=lambda b: b.date)


def test_upsert_overwrites_existing_day(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars([bar("2330", "2026-10-30", close=100.0, volume=1000)])
        store.upsert_bars([bar("2330", "2026-10-30", close=105.0, volume=2000)])

        assert store.get_bars("2330") == [bar("2330", "2026-10-30", close=105.0, volume=2000)]


def test_latest_date_is_tracked_per_code(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars([bar("2330", "2026-10-29"), bar("2330", "2026-10-30"), bar("2454", "2026-10-28")])

        assert store.latest_date("2330") == date(2026, 10, 30)
        assert store.latest_date("2454") == date(2026, 10, 28)
        assert store.latest_date("3008") is None


def test_earliest_date_is_tracked_per_code(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars([bar("2330", "2026-10-29"), bar("2330", "2026-10-27"), bar("2454", "2026-10-30")])

        assert store.earliest_date("2330") == date(2026, 10, 27)
        assert store.earliest_date("9999") is None


def test_data_persists_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "screener.db"
    with SqlitePriceStore(path) as store:
        store.upsert_bars([bar("2330", "2026-10-30")])

    with SqlitePriceStore(path) as store:
        assert store.get_bars("2330") == [bar("2330", "2026-10-30")]


# --- 除權息、公司行動、上市櫃日（PROJECT_PLAN.md 14.2）---------------------------

def test_events_round_trip_and_overwrite(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_dividends([DividendEvent("2884", date(2024, 7, 25), 1.0, 0.2),
                                DividendEvent("2884", date(2023, 7, 20), 0.5, 0.0)])
        store.upsert_dividends([DividendEvent("2884", date(2024, 7, 25), 1.2, 0.2)])          # 同一天覆寫
        store.upsert_dividend_results([DividendResult("2884", date(2024, 7, 26), 29.8, 28.03)])
        store.upsert_actions([CorporateAction("2884", date(2025, 8, 25), ActionKind.SPLIT, 40.0, 10.0),
                              CorporateAction("2884", date(2025, 8, 25), ActionKind.CAPITAL_REDUCTION, 40.0, 44.0)])
        store.set_event_coverage("2884", EventCoverage(date(2020, 6, 1), date(2026, 10, 30)))
        store.upsert_listings([Listing("2884", Market.TSE, date(2002, 1, 28))])

        assert store.get_dividends("2884") == [DividendEvent("2884", date(2023, 7, 20), 0.5, 0.0),
                                               DividendEvent("2884", date(2024, 7, 25), 1.2, 0.2)]
        assert store.get_dividend_results("2884") == [DividendResult("2884", date(2024, 7, 26), 29.8, 28.03)]
        assert {a.kind for a in store.get_actions("2884")} == {ActionKind.SPLIT, ActionKind.CAPITAL_REDUCTION}
        assert store.event_coverage("2884") == EventCoverage(date(2020, 6, 1), date(2026, 10, 30))
        assert store.get_listing("2884") == Listing("2884", Market.TSE, date(2002, 1, 28))
        assert store.event_coverage("2330") is None and store.get_listing("2330") is None
        assert store.get_dividends("2330") == []


# --- 盤中掃描的紀錄（RESEARCH_PLAN.md 4.1）------------------------------------

def scan_run(day: str = "2026-11-04", at: str = "13:10:05", **overrides) -> ScanRun:
    values = {"day": date.fromisoformat(day), "scanned_at": datetime.fromisoformat(f"{day}T{at}"),
              "rules_fingerprint": "0123456789ab", "observed": 290, "evaluated": 288, "not_evaluated": 2,
              "substituted": 1, "prev_close_mismatch": 3, "raw_file": f"data/snapshots/{day}_131005.json.gz"}
    return ScanRun(**{**values, **overrides})


SIGNALS = [
    SignalRecord("2330", "bull_ma5_breakout", "多頭突破五日線紅K", Side.LONG, Trend.BULL, 2340.0, None, 1.27,
                 "突破 MA5 2325.5"),
    SignalRecord("2308", "ma_bear_alignment", "三線空排", Side.SHORT, Trend.UNKNOWN, 1335.0,
                 PriceSubstitute.BEST_ASK, -9.8, "MA5 1400 < MA10 1420 < MA20 1450"),
]
QUOTES = [
    Quote("2330", date(2026, 11, 4), 2310.0, 2345.0, 2310.0, 2340.0, 2370.0, 27196, 2335.0, 2340.0,
          raw={"c": "2330"}),
    Quote("6488", date(2026, 11, 4), None, None, None, None, 756.0, None, None, None),     # 今日尚無成交
]


def test_scan_record_round_trip(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        run_id = store.record_scan(scan_run(), SIGNALS, QUOTES)

        (run,) = store.get_scan_runs(date(2026, 11, 4), date(2026, 11, 4))
        assert run == scan_run(id=run_id)
        assert store.get_signal_records(run_id) == SIGNALS
        assert store.get_snapshot(run_id) == {q.code: q for q in QUOTES}
        assert store.get_snapshot(run_id)["2330"].raw is None       # 原始資料另存壓縮檔，不進資料庫（D46）


def test_scan_runs_are_filtered_by_date_and_ordered_by_time(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        later = store.record_scan(scan_run("2026-11-04", "13:20:00"), [], [])
        first = store.record_scan(scan_run("2026-11-04", "13:10:05"), [], [])
        other = store.record_scan(scan_run("2026-11-05"), [], [])
        store.record_scan(scan_run("2026-11-10"), [], [])

        runs = store.get_scan_runs(date(2026, 11, 4), date(2026, 11, 5))

    assert [run.id for run in runs] == [first, later, other]


def test_failed_scan_record_leaves_nothing(tmp_path: Path) -> None:
    # 同一檔同一規則重複：寫入訊號時失敗，整次掃描（含執行紀錄）都不寫入
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.record_scan(scan_run(), [SIGNALS[0], SIGNALS[0]], QUOTES)

        assert store.get_scan_runs(date(2026, 1, 1), date(2026, 12, 31)) == []
