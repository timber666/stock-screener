from datetime import date
from pathlib import Path

from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.models import (ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult, Listing,
                             Market)


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
