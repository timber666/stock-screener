from datetime import date
from pathlib import Path

from screener.data.db import SqlitePriceStore
from screener.models import DailyBar


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


def test_data_persists_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "screener.db"
    with SqlitePriceStore(path) as store:
        store.upsert_bars([bar("2330", "2026-10-30")])

    with SqlitePriceStore(path) as store:
        assert store.get_bars("2330") == [bar("2330", "2026-10-30")]
