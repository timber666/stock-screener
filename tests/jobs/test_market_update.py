"""market-update：回補、續跑、非交易日、交易日曆的檢查與事件（RESEARCH_PLAN.md 4.3）。"""

import copy
import gzip
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES_DIR, REPO_CONFIG_DIR
from screener.__main__ import EXIT_CONFIG_ERROR, main
from screener.config import load_research_config
from screener.data.base import DataSourceError
from screener.jobs import JobFailed
from screener.jobs.market_update import RawArchive, Sources, update_market
from screener.models import ActionKind, CorporateAction, Market
from screener.research.market.models import Delisting
from screener.research.market.store import SqliteMarketStore

TODAY = date(2026, 11, 7)
MARKET = load_research_config(REPO_CONFIG_DIR / "research.yaml").market


def fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / "market" / f"{name}.json").read_text(encoding="utf-8"))


def twse_body(day: date) -> Any:
    body = copy.deepcopy(fixture("twse_daily_20261106"))
    body["date"] = f"{day:%Y%m%d}"
    return body


def tpex_body(day: date) -> Any:
    body = copy.deepcopy(fixture("tpex_daily_20261019"))
    body["date"] = f"{day:%Y%m%d}"
    return body


def tpex_holiday(day: date) -> Any:
    body = copy.deepcopy(fixture("tpex_daily_20261009"))
    body["date"] = f"{day:%Y%m%d}"
    return body


class FakeExchange:
    """trading 以外的日子回傳非交易日；fail_on 的日子拋出錯誤。"""

    def __init__(self, market: Market, trading: Sequence[date], *, fail_on: Sequence[date] = ()) -> None:
        self.market = market
        self.trading = set(trading)
        self.fail_on = set(fail_on)
        self.calls: list[tuple] = []

    def daily(self, day: date) -> Any:
        self.calls.append(("daily", day))
        if day in self.fail_on:
            raise DataSourceError("HTTP 403")
        if self.market is Market.TSE:
            return twse_body(day) if day in self.trading else fixture("twse_daily_20261009")
        return tpex_body(day) if day in self.trading else tpex_holiday(day)

    def dividend_results(self, start: date, end: date) -> Any:
        self.calls.append(("dividends", start, end))
        if start != date(2026, 10, 1):
            return {"stat": "很抱歉，沒有符合條件的資料!"} if self.market is Market.TSE else {
                "stat": "ok", "tables": [{"fields": [], "data": []}]}
        return fixture("twse_exright" if self.market is Market.TSE else "tpex_exright")

    def capital_reductions(self, start: date, end: date) -> Any:
        self.calls.append(("reductions", start, end))
        return fixture("twse_capital_reduction" if self.market is Market.TSE else "tpex_capital_reduction")

    def return_index(self, month: date) -> Any:
        self.calls.append(("return_index", month))
        return fixture("twse_return_index") if month == date(2026, 10, 1) else {"stat": "很抱歉，沒有符合條件的資料!"}

    def valuation(self, day: date) -> Any:
        self.calls.append(("valuation", day))
        return fixture("twse_valuation_20261106" if self.market is Market.TSE else "tpex_valuation_20261106")

    def shares(self, day: date) -> Any:
        self.calls.append(("shares", day))
        return fixture("twse_shares_20261106")


class FakeMops:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def revenue_page(self, market: Market, month: date, *, foreign: bool) -> bytes:
        self.calls.append((market, month, foreign))
        return (FIXTURES_DIR / "market" / "mops_revenue_sii.html").read_bytes()


class FakeCalendar:
    def __init__(self, days: Sequence[date]) -> None:
        self.days = sorted(days)

    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self.days if start <= d <= end]

    def fetch_delistings(self, end: date) -> list[Delisting]:
        return [Delisting("1538", date(2026, 12, 1), "正峰")]

    def fetch_splits(self, start: date, end: date) -> list[CorporateAction]:
        return [CorporateAction("2327", date(2026, 10, 5), ActionKind.SPLIT, 100.0, 10.0),
                CorporateAction("0050", date(2025, 6, 18), ActionKind.SPLIT, 188.65, 47.16)]   # ETF 不存


def make_sources(trading: Sequence[date], calendar: Sequence[date] | None = None, *,
                 tse_trading: Sequence[date] | None = None, fail_on: Sequence[date] = ()) -> Sources:
    return Sources(twse=FakeExchange(Market.TSE, trading if tse_trading is None else tse_trading, fail_on=fail_on),
                   tpex=FakeExchange(Market.OTC, trading),
                   finmind=FakeCalendar(trading if calendar is None else calendar), mops=FakeMops())


def run(tmp_path: Path, sources: Sources, start: date, end: date, today: date = TODAY):
    with SqliteMarketStore(tmp_path / "market.db") as store:
        return update_market(store, sources, MARKET, archive=RawArchive(tmp_path / "raw"), start=start, end=end,
                             today=today)


def daily_calls(sources: Sources) -> list[date]:
    return [call[1] for call in sources.twse.calls if call[0] == "daily"]


OCT_12, OCT_13 = date(2026, 10, 12), date(2026, 10, 13)


def test_backfill_queries_weekdays_and_records_closed_days(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])

    summary = run(tmp_path, sources, date(2026, 10, 9), OCT_13)

    # 10/09（週五，國慶日補假）也要查詢，以交易所的回應判斷；週末不在交易日曆上，不查詢
    assert daily_calls(sources) == [date(2026, 10, 9), OCT_12, OCT_13]
    assert [c[1] for c in sources.tpex.calls if c[0] == "daily"] == daily_calls(sources)
    assert (summary.trading_days, summary.closed_days, summary.skipped_days) == (2, 1, 0)
    with SqliteMarketStore(tmp_path / "market.db") as store:
        assert store.trading_days() == [OCT_12, OCT_13]
        assert store.trading_days(Market.OTC) == [OCT_12, OCT_13]
        assert store.fetch_records("tse_daily")[date(2026, 10, 9)].status == "closed"
        quotes = store.quotes_of("2330")
        assert [q.date for q in quotes] == [OCT_12, OCT_13] and quotes[0].close == 2135.0
        assert [q.market for q in store.quotes_of("1294")] == [Market.OTC, Market.OTC]
        assert store.return_index() == {OCT_12: 91806.06, OCT_13: 91806.06}
    raw = tmp_path / "raw" / "tse_daily" / "2026" / "20261012.json.gz"
    with gzip.open(raw, "rt", encoding="utf-8") as f:
        assert json.load(f)["date"] == "20261012"


def test_rerun_skips_recorded_days(tmp_path: Path) -> None:
    run(tmp_path, make_sources([OCT_12, OCT_13]), date(2026, 10, 9), OCT_13)
    sources = make_sources([OCT_12, OCT_13, date(2026, 10, 14)])

    summary = run(tmp_path, sources, date(2026, 10, 9), date(2026, 10, 14))

    assert daily_calls(sources) == [date(2026, 10, 14)]
    assert summary.skipped_days == 3 and summary.trading_days == 1


def test_saturday_trading_day_on_calendar_is_queried(tmp_path: Path) -> None:
    saturday = date(2016, 6, 4)                 # 補班交易日
    sources = make_sources([date(2016, 6, 3), saturday])

    run(tmp_path, sources, date(2016, 6, 3), date(2016, 6, 5))

    assert daily_calls(sources) == [date(2016, 6, 3), saturday]


def test_interrupted_backfill_resumes(tmp_path: Path) -> None:
    with pytest.raises(JobFailed, match="2026-10-13.*已完成 1/2 天"):
        run(tmp_path, make_sources([OCT_12, OCT_13], fail_on=[OCT_13]), OCT_12, OCT_13)
    with SqliteMarketStore(tmp_path / "market.db") as store:
        assert store.trading_days() == [OCT_12]

    sources = make_sources([OCT_12, OCT_13])
    run(tmp_path, sources, OCT_12, OCT_13)
    assert daily_calls(sources) == [OCT_13]


def test_markets_must_agree(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13], tse_trading=[OCT_12])

    with pytest.raises(JobFailed, match="2026-10-13.*上市沒有行情、上櫃有行情"):
        run(tmp_path, sources, OCT_12, OCT_13)
    with SqliteMarketStore(tmp_path / "market.db") as store:
        assert store.trading_days() == [OCT_12]         # 不一致的那天不寫入，重新執行會再查


def test_calendar_day_without_quotes_fails(tmp_path: Path) -> None:
    sources = make_sources([OCT_12], calendar=[OCT_12, OCT_13])

    with pytest.raises(JobFailed, match="交易日曆有這一天"):
        run(tmp_path, sources, OCT_12, OCT_13)


def test_trading_day_missing_from_calendar_is_reported(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13], calendar=[OCT_13])

    summary = run(tmp_path, sources, OCT_12, OCT_13)

    assert summary.off_calendar == [OCT_12]
    assert summary.trading_days == 2


def test_events_are_fetched_per_month_and_year(tmp_path: Path) -> None:
    sources = make_sources([OCT_12])

    summary = run(tmp_path, sources, date(2026, 9, 30), OCT_12)

    assert [c for c in sources.twse.calls if c[0] not in ("daily", "valuation", "shares")] == [
        ("dividends", date(2026, 9, 1), date(2026, 9, 30)), ("return_index", date(2026, 9, 1)),
        ("dividends", date(2026, 10, 1), date(2026, 10, 31)), ("return_index", date(2026, 10, 1)),
        ("reductions", date(2026, 1, 1), date(2026, 12, 31)),
    ]
    assert summary.dividends == 2 + 3 and summary.reductions == 2 + 2 and summary.splits == 1
    with SqliteMarketStore(tmp_path / "market.db") as store:
        results = store.dividend_results()
        assert [(r.code, r.date, m) for r, m in results][:2] == [("1784", date(2026, 10, 1), Market.OTC),
                                                                 ("2414", date(2026, 11, 6), Market.TSE)]
        assert {(a.code, a.kind, source) for a, source in store.actions()} == {
            ("1414", ActionKind.CAPITAL_REDUCTION, "tse"), ("2380", ActionKind.CAPITAL_REDUCTION, "tse"),
            ("4131", ActionKind.CAPITAL_REDUCTION, "otc"), ("8047", ActionKind.CAPITAL_REDUCTION, "otc"),
            ("2327", ActionKind.SPLIT, "finmind")}
        assert len(store.return_index(monthly=True)) == 19


def test_unfinished_periods_are_fetched_again(tmp_path: Path) -> None:
    run(tmp_path, make_sources([OCT_12]), OCT_12, OCT_12, today=date(2026, 10, 20))     # 10 月還沒結束
    sources = make_sources([OCT_12])

    run(tmp_path, sources, OCT_12, OCT_12, today=TODAY)

    assert [c[0] for c in sources.twse.calls] == ["dividends", "return_index", "reductions"]   # 年度也還沒結束
    sources = make_sources([OCT_12])
    run(tmp_path, sources, OCT_12, OCT_12, today=date(2027, 1, 2))
    assert [c[0] for c in sources.twse.calls] == ["reductions"]


def test_securities_are_rebuilt_from_quotes(tmp_path: Path) -> None:
    run(tmp_path, make_sources([OCT_12, OCT_13]), OCT_12, OCT_13)

    with SqliteMarketStore(tmp_path / "market.db") as store:
        securities = {s.code: s for s in store.securities()}
    assert len(securities) == 7 + 4
    assert securities["2330"].first_date == OCT_12 and securities["2330"].last_date == OCT_13
    assert securities["1294"].market is Market.OTC
    assert securities["1538"].delisted_on == date(2026, 12, 1)
    assert securities["2330"].delisted_on is None


def test_end_must_be_before_today(config_dir: Path) -> None:
    assert main(["--config-dir", str(config_dir), "market-update", "--end", date.today().isoformat()]) \
        == EXIT_CONFIG_ERROR


# --- 基本面（M9，RESEARCH_PLAN.md 4.4、4.5）------------------------------------------

def fundamental_calls(exchange: FakeExchange, kind: str) -> list[date]:
    return [call[1] for call in exchange.calls if call[0] == kind]


def test_fundamentals_fetch_published_revenue_and_monthly_samples(tmp_path: Path) -> None:
    sources = make_sources([date(2026, 10, 12), date(2026, 10, 13), date(2026, 11, 2)])

    summary = run(tmp_path, sources, date(2026, 10, 12), date(2026, 11, 2), today=date(2026, 11, 7))

    # 10 月的營收要到 11/10 之後才抓；11 月還沒有
    assert sources.mops.calls == []
    assert fundamental_calls(sources.twse, "valuation") == [date(2026, 10, 12), date(2026, 11, 2)]      # 每月第一個交易日
    assert fundamental_calls(sources.tpex, "valuation") == [date(2026, 10, 12), date(2026, 11, 2)]
    assert fundamental_calls(sources.twse, "shares") == [date(2026, 10, 12), date(2026, 11, 2)]
    assert fundamental_calls(sources.tpex, "shares") == []              # 上櫃的發行股數取自已存檔的每日行情
    assert summary.valuations > 0 and summary.shares > 0
    with SqliteMarketStore(tmp_path / "market.db") as store:
        valuations = store.valuation_frame()
        assert {"2330", "1101", "6488"} <= set(valuations["code"]) and "0050" not in set(valuations["code"])
        shares = store.shares_frame()
        assert set(shares["market"]) == {"tse", "otc"}
        assert shares.loc[shares["code"] == "2330", "shares"].iloc[0] == 25932370067


def test_revenue_is_fetched_after_the_deadline_for_all_four_pages(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])

    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 14))

    # 9 月營收：10/10 之後才抓（今天 10/14），國內與外國公司 × 上市與上櫃；10 月還不能抓
    assert sorted(sources.mops.calls, key=str) == sorted(
        [(market, date(2026, 9, 1), foreign) for market in Market for foreign in (False, True)], key=str)
    with SqliteMarketStore(tmp_path / "market.db") as store:
        rows = store.revenue_frame()
    assert set(rows["month"]) == {"2026-09-01"}
    assert "2330" not in set(rows["code"]) and "1101" in set(rows["code"])
    assert set(rows["industry"]) == {"水泥工業", "食品工業"}


def test_revenue_is_fetched_again_until_settled_then_not_at_all(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])
    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 14))
    assert len(sources.mops.calls) == 4

    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 15))     # 10/15 尚未超過 10/10 + 5 天
    assert len(sources.mops.calls) == 8

    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 16))
    assert len(sources.mops.calls) == 12                                           # 10/15 抓到的還不算完成

    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 17))
    assert len(sources.mops.calls) == 12


def test_samples_are_not_fetched_again(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])
    run(tmp_path, sources, OCT_12, OCT_13, today=date(2026, 10, 14))
    assert fundamental_calls(sources.twse, "valuation") == [OCT_12]

    run(tmp_path, sources, OCT_12, OCT_13, today=date(2026, 10, 20))

    assert fundamental_calls(sources.twse, "valuation") == [OCT_12]


def test_otc_shares_fall_back_to_a_request_when_not_archived(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])
    run(tmp_path, sources, OCT_12, OCT_13, today=OCT_13 + (date(2026, 10, 14) - OCT_13))
    with SqliteMarketStore(tmp_path / "market.db") as store:
        store._conn.execute("DELETE FROM fetch_log WHERE source = 'otc_shares'")     # 模擬尚未解析發行股數的回應
        store._conn.commit()
    (tmp_path / "raw" / "otc_daily" / "2026" / "20261012.json.gz").unlink()
    before = len([c for c in sources.tpex.calls if c[0] == "daily"])

    run(tmp_path, sources, OCT_12, OCT_13, today=date(2026, 10, 15))

    assert len([c for c in sources.tpex.calls if c[0] == "daily"]) == before + 1


def test_a_failed_page_can_be_resumed(tmp_path: Path) -> None:
    sources = make_sources([OCT_12, OCT_13])
    calls = {"n": 0}
    original = sources.mops.revenue_page

    def flaky(market: Market, month: date, *, foreign: bool) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            raise DataSourceError("HTTP 503")
        return original(market, month, foreign=foreign)

    sources.mops.revenue_page = flaky
    with pytest.raises(JobFailed, match="已完成 1/"):
        run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 20))

    run(tmp_path, sources, date(2026, 9, 1), OCT_13, today=date(2026, 10, 20))

    assert len(sources.mops.calls) == 4         # 第一份已完成，不重抓；之後從中斷處繼續
