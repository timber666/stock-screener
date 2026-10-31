"""回放模式（PROJECT_PLAN.md 第 12 節）：以資料庫的日 K 當作「今日」跑完整流程。"""

import io
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, make_bars
from screener.config import load_config
from screener.data.db import SqlitePriceStore
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs.intraday_scan import replay_scan, run
from screener.models import DailyBar, Market, Side, StockInfo

CONFIG = load_config(REPO_CONFIG_DIR)
ENGINE = Engine.from_config(CONFIG.rules)

# 1111：今日形成三線多排（MA5 102 > MA10 101 > MA20 100.5）；2222：一路平盤；3333：今日沒有 K 棒；
# 4444：今日形成三線空排（MA5 98 < MA10 99 < MA20 99.5）
FORMS_TODAY = make_bars([100.0] * 20 + [110.0])
FLAT = make_bars([100.0] * 21)
FALLS_TODAY = make_bars([100.0] * 20 + [90.0])
DAY = FORMS_TODAY.index[-1]


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=Market.TSE, group="PCB", subcategory="銅箔基板")


def daily_bars(code: str, bars: pd.DataFrame) -> list[DailyBar]:
    return [DailyBar(code, day, r.open, r.high, r.low, r.close, int(r.volume))
            for day, r in zip(bars.index, bars.itertuples())]


@pytest.fixture
def store(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars(daily_bars("1111", FORMS_TODAY) + daily_bars("2222", FLAT)
                          + daily_bars("3333", FLAT.iloc[:-1]) + daily_bars("4444", FALLS_TODAY))
        yield store


def scan(store: SqlitePriceStore, stocks: list[StockInfo], day=DAY):
    return replay_scan(stocks, store=store, engine=ENGINE, trend_config=CONFIG.rules.trend, day=day)


def test_replay_evaluates_stocks_with_a_bar_on_that_day(store: SqlitePriceStore) -> None:
    report = scan(store, [stock("1111"), stock("2222"), stock("3333"), stock("4444")])

    assert report.is_replay and report.day == DAY and report.observed == 4
    assert [(h.ctx.info.code, h.side, [s.rule_name for s in h.signals]) for h in report.hits] == [
        ("1111", Side.LONG, ["三線多排"]),
        ("4444", Side.SHORT, ["三線空排"]),
    ]
    assert [h.change_pct for h in report.hits] == [10.0, -10.0]     # 回放以前一日收盤計算漲跌幅
    assert report.not_evaluated == ("3333",)


def test_replay_ignores_bars_after_that_day(store: SqlitePriceStore) -> None:
    report = scan(store, [stock("1111")], day=FORMS_TODAY.index[-2])

    assert report.hits == ()                        # 前一天還沒形成多排


def test_day_without_any_bars_fails(store: SqlitePriceStore) -> None:
    with pytest.raises(JobFailed, match="沒有任何股票 .* 的日 K"):
        scan(store, [stock("3333")])


def test_run_prints_report(store: SqlitePriceStore, tmp_path: Path) -> None:
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222")))
    config = replace(CONFIG, watchlist=watchlist,
                     settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))
    out = io.StringIO()

    run(config, dry_run=True, replay=DAY, out=out)

    assert out.getvalue().startswith(f"【回放】{DAY}（以收盤 K 棒計算）\n\n━━ 多方 ━━\n▍PCB\n1111 股1111  110  ▲10.0%｜銅箔基板｜")


@pytest.mark.parametrize(
    ("dry_run", "replay", "message"),
    [(True, None, "盤中掃描尚未實作（M4）"), (False, DAY, "Discord 推播尚未實作（M5）")],
)
def test_unimplemented_modes_fail_before_doing_any_work(dry_run: bool, replay, message: str) -> None:
    with pytest.raises(NotImplementedError, match=message):
        run(CONFIG, dry_run=dry_run, replay=replay)
