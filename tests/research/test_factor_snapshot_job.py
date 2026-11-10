"""因子快照的計算與 factor-snapshot 指令（RESEARCH_PLAN.md 4.7、R42）：以手工的 market.db 驗證，不連網。"""

import io
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR
from screener.config import load_config, load_research_config
from screener.data.factor_snapshot import read_snapshot
from screener.jobs import JobFailed, factor_snapshot
from screener.models import Market, SnapshotStatus
from screener.research.factor_snapshot import build_snapshot, latest_month, require_fresh
from screener.research.market.models import DailyQuotes, MarketQuote
from screener.research.market.store import SqliteMarketStore

MARKET = load_research_config(REPO_CONFIG_DIR / "research.yaml").market     # revenue_available_day = 10
SATURDAY = date(2026, 11, 14)


def weekdays(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


def revenue_row(code: str, month: str, revenue: float, last_year: float) -> dict:
    return {"code": code, "month": month, "market": "tse", "name": f"股{code}", "industry": "電子", "revenue": revenue,
            "previous": 100.0, "last_year": last_year, "cumulative": 100.0, "cumulative_last_year": 100.0}


REVENUE = pd.DataFrame([
    revenue_row("2330", "2026-09-01", 150, 100),
    revenue_row("2330", "2026-10-01", 160, 100),   # +60%
    revenue_row("2317", "2026-10-01", 120, 100),   # +20%
    revenue_row("1101", "2026-10-01", 120, 100),   # +20%：與 2317 同名次
    revenue_row("6488", "2026-10-01", 90, 100),    # −10%
    revenue_row("6024", "2026-10-01", -50, 100),   # 證券業的評價損失：營收為負也照排（−150%）
    revenue_row("3333", "2026-09-01", 300, 100),   # 晚公告：還沒有 10 月營收
    revenue_row("4444", "2026-10-01", 200, 0),     # 去年同月沒有營收：沒有值
    revenue_row("5555", "2026-10-01", 500, 100),   # 已下市
    revenue_row("0050", "2026-10-01", 500, 100),   # 不是普通股（ETF）
    revenue_row("9103", "2026-10-01", 500, 100),   # 不是普通股（存託憑證）
])


def test_latest_month_follows_the_available_day() -> None:
    months = ["2026-09-01", "2026-10-01"]

    assert latest_month(months, weekdays(date(2026, 10, 1), date(2026, 11, 10)), MARKET) == date(2026, 9, 1)
    assert latest_month(months, weekdays(date(2026, 10, 1), date(2026, 11, 11)), MARKET) == date(2026, 10, 1)
    assert latest_month(months, weekdays(date(2026, 10, 1), date(2026, 10, 12)), MARKET) == date(2026, 9, 1)
    assert latest_month(months, weekdays(date(2026, 10, 1), date(2026, 10, 9)), MARKET) is None


def test_snapshot_ranks_common_stocks_of_the_latest_month() -> None:
    snapshot = build_snapshot(REVENUE, delisted={"5555"}, days=weekdays(date(2026, 10, 1), date(2026, 11, 13)),
                              market=MARKET, snapshot_date=SATURDAY)

    assert (snapshot.month, snapshot.snapshot_date, snapshot.count) == (date(2026, 10, 1), SATURDAY, 5)
    assert snapshot.values == {"2330": (pytest.approx(0.6), 1), "2317": (pytest.approx(0.2), 2),
                               "1101": (pytest.approx(0.2), 2), "6488": (pytest.approx(-0.1), 4),
                               "6024": (pytest.approx(-1.5), 5)}


def test_before_the_available_day_the_previous_month_is_used() -> None:
    snapshot = build_snapshot(REVENUE, delisted=set(), days=weekdays(date(2026, 10, 1), date(2026, 11, 6)),
                              market=MARKET, snapshot_date=date(2026, 11, 7))

    assert snapshot.month == date(2026, 9, 1)
    assert snapshot.values == {"3333": (pytest.approx(2.0), 1), "2330": (pytest.approx(0.5), 2)}


@pytest.mark.parametrize(("today", "covered", "fresh"), [
    (date(2026, 11, 14), date(2026, 11, 13), True),       # 週六：要到昨天（週五）
    (date(2026, 11, 14), date(2026, 11, 12), False),
    (date(2026, 11, 13), date(2026, 11, 6), True),        # 週五：要到上週五
    (date(2026, 11, 16), date(2026, 11, 13), True),       # 週一
    (date(2026, 11, 16), date(2026, 11, 12), False),
])
def test_market_db_must_cover_the_last_friday(today: date, covered: date, fresh: bool) -> None:
    if fresh:
        require_fresh(covered, today)
    else:
        with pytest.raises(JobFailed, match=f"market.db 只更新到 {covered}"):
            require_fresh(covered, today)


# --- 指令 -------------------------------------------------------------------

def _quote(code: str, day: date) -> MarketQuote:
    return MarketQuote(code=code, date=day, market=Market.TSE, name=f"股{code}", open=100.0, high=100.0, low=100.0,
                       close=100.0, volume=1000, value=100_000, trades=10, change=0.0, flag=None, next_limit_up=None,
                       next_limit_down=None)


def build_market_db(path: Path, end: date) -> None:
    with SqliteMarketStore(path) as store:
        for day in weekdays(date(2026, 10, 1), end):
            quotes = tuple(_quote(code, day) for code in ("2330", "2317", "5555"))
            store.record_day(day, [("tse_daily", DailyQuotes(day, Market.TSE, quotes, 1000.0)),
                                   ("otc_daily", DailyQuotes(day, Market.OTC, ()))], day)
        for row in REVENUE.to_dict("records"):
            store._conn.execute("INSERT INTO monthly_revenue (code, month, market, name, industry, revenue, previous,"
                                " last_year, cumulative, cumulative_last_year) VALUES (:code, :month, :market, :name,"
                                " :industry, :revenue, :previous, :last_year, :cumulative, :cumulative_last_year)", row)
        store._conn.commit()
        store.refresh_securities()
        store._conn.execute("UPDATE securities SET delisted_on = '2026-11-20' WHERE code = '5555'")
        store._conn.commit()


@pytest.fixture
def env(config_dir: Path):
    return load_config(config_dir), load_research_config(config_dir / "research.yaml")


def test_job_writes_the_snapshot(env) -> None:
    config, research = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    out = io.StringIO()

    factor_snapshot.run(config, research, dry_run=False, out=out, today=SATURDAY)

    result = read_snapshot(config.settings.factor_snapshot.path, today=SATURDAY, max_age_days=10)
    assert result.status is SnapshotStatus.OK
    assert (result.snapshot.month, result.snapshot.count) == (date(2026, 10, 1), 5)
    assert out.getvalue().splitlines() == [
        "因子快照 2026-11-14：2026-10 月營收，5 檔上市櫃普通股（資料到 2026-11-13），觀察清單 3/290 檔有值",
        f"已寫入 {config.settings.factor_snapshot.path}",
    ]


def test_dry_run_prints_watchlist_values_without_writing(env) -> None:
    config, research = env
    build_market_db(research.market.db_path, date(2026, 11, 13))
    out = io.StringIO()

    factor_snapshot.run(config, research, dry_run=True, out=out, today=SATURDAY)

    assert not config.settings.factor_snapshot.path.exists()
    assert out.getvalue().splitlines()[1:] == [
        "  2330 台積電  營收年增 +60.00%（第 1 名，前 20%）",
        "  2317 鴻海  營收年增 +20.00%（第 2 名，前 40%）",
        "  6488 環球晶  營收年增 -10.00%（第 4 名，前 80%）",
        "（--dry-run：沒有寫檔）",
    ]


def test_job_fails_when_market_db_is_not_updated(env) -> None:
    config, research = env
    build_market_db(research.market.db_path, date(2026, 11, 6))

    with pytest.raises(JobFailed, match="market.db 只更新到 2026-11-06，需要到 2026-11-13"):
        factor_snapshot.run(config, research, dry_run=False, out=io.StringIO(), today=SATURDAY)
    assert not config.settings.factor_snapshot.path.exists()


def test_job_fails_without_market_db(env) -> None:
    config, research = env

    with pytest.raises(JobFailed, match="請先執行 market-update"):
        factor_snapshot.run(config, research, dry_run=False, out=io.StringIO(), today=SATURDAY)
