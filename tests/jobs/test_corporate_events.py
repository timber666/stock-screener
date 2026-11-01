"""除權息與公司行動的抓取與交叉驗證（PROJECT_PLAN.md 14.2）。"""

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest

from screener.data.base import DataSourceError, EventCoverage
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed
from screener.jobs.corporate_events import check_reference_prices, update_events
from screener.models import (ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult, Listing,
                             Market, StockInfo)

TODAY = date(2026, 10, 30)
FIRST_BAR = date(2024, 7, 23)
REFETCH_DAYS = 7
TOLERANCE_PCT = 0.5


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=Market.TSE, group="PCB", subcategory="CCL")


def bar(code: str, day: date, close: float) -> DailyBar:
    return DailyBar(code, day, close, close, close, close, 1000)


class FakeEventSource:
    """回傳預先準備的事件中落在查詢區間內的部分，並記錄每次查詢。"""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, date, date]] = []
        self.dividends = [DividendEvent("2884", date(2024, 7, 25), 1.2, 0.200332)]
        self.results = [DividendResult("2884", date(2024, 7, 26), 29.80, 28.03)]
        self.reductions = [CorporateAction("2884", date(2025, 3, 3), ActionKind.CAPITAL_REDUCTION, 30.0, 33.0)]
        self.splits = [CorporateAction("2884", date(2025, 8, 25), ActionKind.SPLIT, 40.0, 10.0),
                       CorporateAction("9999", date(2025, 8, 25), ActionKind.SPLIT, 40.0, 10.0)]

    def _filter(self, kind: str, items, code: str, start: date, end: date):
        self.calls.append((kind, code, start, end))
        if code == self.fail_on:
            raise DataSourceError("已達 FinMind 每小時請求上限")
        return [x for x in items if x.code == code and start <= x.date <= end]

    def fetch_dividends(self, code, start, end):
        return self._filter("dividends", self.dividends, code, start, end)

    def fetch_dividend_results(self, code, start, end):
        return self._filter("results", self.results, code, start, end)

    def fetch_capital_reductions(self, code, start, end):
        return self._filter("reductions", self.reductions, code, start, end)

    def fetch_splits(self, start, end):
        self.calls.append(("splits", "全市場", start, end))
        return [x for x in self.splits if start <= x.date <= end]


class FakeListings:
    def __init__(self, listings: dict[str, Listing] | None = None, fail: bool = False) -> None:
        self.listings = listings if listings is not None else {
            "2884": Listing("2884", Market.TSE, date(2002, 1, 28)), "2330": Listing("2330", Market.TSE, date(1994, 9, 5))}
        self.fail = fail

    def fetch_listings(self) -> dict[str, Listing]:
        if self.fail:
            raise DataSourceError("證交所 ISIN 清單拒絕請求（HTTP 403）")
        return self.listings


@pytest.fixture
def store(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars([bar("2884", FIRST_BAR, 29.80), bar("2884", date(2024, 7, 26), 27.00),
                           bar("2330", date(2025, 1, 2), 1000.0)])
        yield store


def run(store: SqlitePriceStore, source: FakeEventSource, codes=("2884", "2330"), listings: FakeListings | None = None):
    return update_events([stock(c) for c in codes], source=source, listings=listings or FakeListings(), store=store,
                         today=TODAY, refetch_days=REFETCH_DAYS)


def test_first_run_covers_each_stock_from_its_first_bar(store: SqlitePriceStore) -> None:
    source = FakeEventSource()

    summary = run(store, source)

    assert source.calls[0] == ("splits", "全市場", FIRST_BAR, TODAY)       # 全市場一次，從最早的日 K 起
    assert ("dividends", "2884", FIRST_BAR, TODAY) in source.calls
    assert ("dividends", "2330", date(2025, 1, 2), TODAY) in source.calls
    assert store.get_dividends("2884") == source.dividends
    assert store.get_dividend_results("2884") == source.results
    assert store.get_actions("2884") == [source.reductions[0], source.splits[0]]     # 不在觀察清單的 9999 不存
    assert store.event_coverage("2884") == EventCoverage(FIRST_BAR, TODAY)
    assert (summary.queried, summary.dividends, summary.actions) == (2, 1, 2)


def test_later_run_only_refetches_recent_days(store: SqlitePriceStore) -> None:
    store.set_event_coverage("2884", EventCoverage(FIRST_BAR, date(2026, 10, 20)))
    source = FakeEventSource()

    run(store, source, codes=("2884",))

    assert ("dividends", "2884", date(2026, 10, 20) - timedelta(days=REFETCH_DAYS), TODAY) in source.calls
    assert store.event_coverage("2884") == EventCoverage(FIRST_BAR, TODAY)


def test_stock_already_queried_today_is_skipped(store: SqlitePriceStore) -> None:
    # 超過請求上限中止後重新執行：已完成的股票不再查詢
    store.set_event_coverage("2884", EventCoverage(FIRST_BAR, TODAY))
    source = FakeEventSource()

    summary = run(store, source)

    assert not [call for call in source.calls if call[1] == "2884"]
    assert (summary.up_to_date, summary.queried) == (1, 1)


def test_history_extended_before_coverage_is_fetched_from_the_new_first_bar(store: SqlitePriceStore) -> None:
    store.set_event_coverage("2884", EventCoverage(date(2025, 1, 2), date(2026, 10, 20)))
    source = FakeEventSource()

    run(store, source, codes=("2884",))

    assert ("dividends", "2884", FIRST_BAR, TODAY) in source.calls
    assert store.event_coverage("2884") == EventCoverage(FIRST_BAR, TODAY)


def test_stock_without_bars_is_not_queried(store: SqlitePriceStore) -> None:
    source = FakeEventSource()

    summary = run(store, source, codes=("2884", "1111"))

    assert summary.no_bars == ["1111"]
    assert not [call for call in source.calls if call[1] == "1111"]


def test_failure_keeps_finished_stocks_and_can_resume(store: SqlitePriceStore) -> None:
    with pytest.raises(JobFailed, match=r"\[2/2\] 2330 .*重新執行會從中斷處繼續"):
        run(store, FakeEventSource(fail_on="2330"))

    assert store.event_coverage("2884") == EventCoverage(FIRST_BAR, TODAY)
    assert store.event_coverage("2330") is None


def test_empty_database_fails() -> None:
    class EmptyStore:
        def earliest_date(self, code):
            return None

    class EmptyListingStore(EmptyStore):
        def upsert_listings(self, listings):
            return len(listings)

    with pytest.raises(JobFailed, match="請先執行 update"):
        update_events([stock("2884")], source=FakeEventSource(), listings=FakeListings(), store=EmptyListingStore(),
                      today=TODAY, refetch_days=REFETCH_DAYS)


# --- 上市櫃日（D38）------------------------------------------------------------

def test_listing_dates_are_stored_and_markets_checked(store: SqlitePriceStore, caplog: pytest.LogCaptureFixture) -> None:
    listings = FakeListings({"2884": Listing("2884", Market.OTC, date(2002, 1, 28))})

    with caplog.at_level(logging.WARNING):
        summary = run(store, FakeEventSource(), listings=listings)

    assert store.get_listing("2884") == Listing("2884", Market.OTC, date(2002, 1, 28))
    assert (summary.wrong_market, summary.unlisted) == (["2884"], ["2330"])
    assert "market 為 tse，證交所清單為 otc" in caplog.text and "不在證交所的上市、上櫃清單中" in caplog.text


def test_listing_failure_stops_before_any_finmind_request(store: SqlitePriceStore) -> None:
    source = FakeEventSource()

    with pytest.raises(JobFailed, match="上市櫃日抓取失敗"):
        run(store, source, listings=FakeListings(fail=True))

    assert source.calls == []


# --- 官方參考價交叉驗證 ------------------------------------------------------

def test_official_reference_price_matches_after_aligning_closure_day(store: SqlitePriceStore,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    # 2884 原定 07-25 除權息遇颱風休市，官方結果記在實際的 07-26：順延後 (29.80 − 1.2) / 1.0200332 = 28.0386，官方 28.03
    run(store, FakeEventSource())

    with caplog.at_level(logging.INFO):
        assert check_reference_prices([stock("2884")], store=store, tolerance_pct=TOLERANCE_PCT) == []

    assert "官方參考價比對：1 筆，最大誤差 0.03%；差異超過 0.5% 的 0 筆，官方有而股利資料沒有的 0 筆" in caplog.text


def test_reference_price_mismatch_and_missing_dividend_are_reported(store: SqlitePriceStore,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    store.upsert_dividends([DividendEvent("2884", date(2024, 7, 25), 2.0, 0.0)])          # 金額錯誤
    store.upsert_dividend_results([DividendResult("2884", date(2024, 7, 26), 29.80, 28.03)])
    store.upsert_bars([bar("2330", date(2025, 1, 3), 990.0)])
    store.upsert_dividend_results([DividendResult("2330", date(2025, 1, 3), 1000.0, 995.0)])   # 沒有股利資料

    with caplog.at_level(logging.INFO):
        mismatches = check_reference_prices([stock("2884"), stock("2330")], store=store,
                                            tolerance_pct=TOLERANCE_PCT)

    assert [(m.code, m.day, m.computed is None) for m in mismatches] == [
        ("2884", date(2024, 7, 26), False), ("2330", date(2025, 1, 3), True)]
    assert "參考價差異" in caplog.text and "只有官方結果" in caplog.text
