import logging
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest

from screener.data.base import DataSourceError
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed
from screener.jobs.daily_update import update_prices
from screener.models import DailyBar, Market, StockInfo

TODAY = date(2026, 10, 30)
BACKFILL_DAYS = 365


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股票{code}", market=Market.TSE, group="PCB", subcategory="CCL")


def bar(code: str, day: date) -> DailyBar:
    return DailyBar(code=code, date=day, open=99.0, high=101.0, low=98.0, close=100.0, volume=1000)


class FakeSource:
    """回傳預先準備的日 K 中落在查詢區間內的部分，並記錄每次查詢。"""

    def __init__(self, bars: Sequence[DailyBar] = (), fail_on: str | None = None) -> None:
        self.bars = list(bars)
        self.fail_on = fail_on
        self.calls: list[tuple[str, date, date]] = []

    def fetch_daily_prices(self, code: str, start: date, end: date) -> list[DailyBar]:
        self.calls.append((code, start, end))
        if code == self.fail_on:
            raise DataSourceError("已達 FinMind 每小時請求上限")
        return [b for b in self.bars if b.code == code and start <= b.date <= end]


@pytest.fixture
def store(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        yield store


def run_update(stocks: list[StockInfo], source: FakeSource, store: SqlitePriceStore):
    return update_prices(stocks, source=source, store=store, today=TODAY, backfill_days=BACKFILL_DAYS)


def test_new_stock_is_backfilled(store: SqlitePriceStore) -> None:
    source = FakeSource([bar("2330", TODAY - timedelta(days=1)), bar("2330", TODAY)])

    summary = run_update([stock("2330")], source, store)

    assert source.calls == [("2330", TODAY - timedelta(days=BACKFILL_DAYS), TODAY)]
    assert [b.date for b in store.get_bars("2330")] == [TODAY - timedelta(days=1), TODAY]
    assert (summary.rows, summary.updated, summary.backfilled) == (2, 1, ["2330"])


def test_existing_stock_fetches_from_day_after_last_bar(store: SqlitePriceStore) -> None:
    store.upsert_bars([bar("2330", date(2026, 10, 27))])
    source = FakeSource([bar("2330", date(2026, 10, 28)), bar("2330", TODAY)])

    summary = run_update([stock("2330")], source, store)

    assert source.calls == [("2330", date(2026, 10, 28), TODAY)]
    assert store.latest_date("2330") == TODAY
    assert (summary.rows, summary.backfilled) == (2, [])


def test_up_to_date_stock_is_not_requested(store: SqlitePriceStore) -> None:
    store.upsert_bars([bar("2330", TODAY)])
    source = FakeSource()

    summary = run_update([stock("2330")], source, store)

    assert source.calls == []
    assert summary.up_to_date == 1


def test_failure_stops_job_and_keeps_completed_stocks(store: SqlitePriceStore) -> None:
    source = FakeSource([bar("1111", TODAY), bar("3333", TODAY)], fail_on="2222")

    with pytest.raises(JobFailed, match="2222 股票2222 抓取失敗.*剩餘 2 檔未更新"):
        run_update([stock("1111"), stock("2222"), stock("3333")], source, store)

    assert [code for code, _, _ in source.calls] == ["1111", "2222"]
    assert store.latest_date("1111") == TODAY
    assert store.latest_date("3333") is None


def test_rerun_after_failure_resumes(store: SqlitePriceStore) -> None:
    stocks = [stock("1111"), stock("2222")]
    with pytest.raises(JobFailed):
        run_update(stocks, FakeSource([bar("1111", TODAY)], fail_on="2222"), store)

    source = FakeSource([bar("1111", TODAY), bar("2222", TODAY)])
    summary = run_update(stocks, source, store)

    assert [code for code, _, _ in source.calls] == ["2222"]
    assert (summary.up_to_date, summary.backfilled) == (1, ["2222"])


def test_backfill_without_data_is_warned(store: SqlitePriceStore, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        summary = run_update([stock("9999")], FakeSource(), store)

    assert summary.empty_backfill == ["9999"]
    assert "查無任何資料" in caplog.text and "9999" in caplog.text
