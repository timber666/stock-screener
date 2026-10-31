"""盤中掃描（PROJECT_PLAN.md 7.2、8.2）與回放模式（第 12 節）的完整流程。"""

import io
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, make_bars
from screener.config import load_config
from screener.data.base import DataSourceError
from screener.data.db import SqlitePriceStore
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs import intraday_scan as intraday_scan_module
from screener.jobs.intraday_scan import intraday_scan, replay_scan, run
from screener.models import DailyBar, Market, PriceSubstitute, Quote, Side, StockInfo
from screener.notify.base import NotifyError

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


# --- 回放 -------------------------------------------------------------------

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


class FakeNotifier:
    """記錄送出的訊息；fail 為 True 時拋出 NotifyError。"""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, content: str) -> None:
        if self.fail:
            raise NotifyError("Discord webhook 請求失敗，已重試 3 次：HTTP 503")
        self.sent.append(content)


@pytest.fixture
def replay_config(store: SqlitePriceStore, tmp_path: Path):
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222"), stock("4444")))
    return replace(CONFIG, watchlist=watchlist, settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))


def test_dry_run_prints_report(replay_config) -> None:
    out = io.StringIO()

    run(replay_config, replay=DAY, notifier=None, out=out)

    assert out.getvalue().startswith(f"【回放】{DAY}（以收盤 K 棒計算）\n\n━━ 多方 ━━\n▍PCB\n1111 股1111  110  ▲10.0%｜銅箔基板｜")


def test_report_is_pushed_to_notifier(replay_config) -> None:
    notifier, out = FakeNotifier(), io.StringIO()

    run(replay_config, replay=DAY, notifier=notifier, out=out)

    (message,) = notifier.sent
    assert message.startswith(f"【回放】{DAY}（以收盤 K 棒計算）") and "1111 股1111" in message
    assert out.getvalue() == ""


def test_long_report_is_split(replay_config, monkeypatch: pytest.MonkeyPatch) -> None:
    # 多方、空方各一個族群，上限設成放不下兩個族群
    monkeypatch.setattr(intraday_scan_module, "MESSAGE_LIMIT", 80)
    notifier, out = FakeNotifier(), io.StringIO()

    run(replay_config, replay=DAY, notifier=notifier)
    run(replay_config, replay=DAY, notifier=None, out=out)

    assert len(notifier.sent) > 1 and all(len(message) <= 80 for message in notifier.sent)
    printed = out.getvalue()
    assert printed.startswith(f"──── 第 1/{len(notifier.sent)} 則 ────\n【回放】")
    assert f"\n\n──── 第 2/{len(notifier.sent)} 則 ────\n" in printed


def test_push_failure_fails_the_job(replay_config) -> None:
    with pytest.raises(JobFailed, match="推播失敗（第 1/1 則）"):
        run(replay_config, replay=DAY, notifier=FakeNotifier(fail=True))


# --- 盤中 -------------------------------------------------------------------

# 資料庫有 20 根收盤 100 的日 K，今天是下一個交易日。今日現價 110 時形成三線多排
# （MA5 102 > MA10 101 > MA20 100.5），與回放的 FORMS_TODAY 相同。
HISTORY = make_bars([100.0] * 20)
LAST_TRADING_DAY = HISTORY.index[-1]                       # 2026-01-30（五）
TODAY = LAST_TRADING_DAY + timedelta(days=3)               # 2026-02-02（一）
NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 13, 10)
MIS = CONFIG.settings.mis


def quote(code: str, *, day: date = TODAY, open: float | None = 100.0, last: float | None = 110.0,
          prev_close: float | None = 100.0, best_bid: float | None = 109.5,
          best_ask: float | None = 110.5) -> Quote:
    """open 為 None 代表今日尚無成交，此時高低價與成交量也都沒有值。"""
    traded = open is not None
    return Quote(code=code, date=day, open=open, high=111.0 if traded else None, low=99.0 if traded else None,
                 last=last, prev_close=prev_close, volume=1500 if traded else None, best_bid=best_bid,
                 best_ask=best_ask)


class FakeQuotes:
    """依序回傳每一輪預先設定的報價（只回傳有查詢的代號）；元素為例外時改為拋出。"""

    def __init__(self, *rounds: Sequence[Quote] | Exception) -> None:
        self.rounds = list(rounds)
        self.calls: list[list[str]] = []

    def fetch_quotes(self, stocks: Sequence[StockInfo]) -> dict[str, Quote]:
        codes = [stock.code for stock in stocks]
        self.calls.append(codes)
        outcome = self.rounds.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {q.code: q for q in outcome if q.code in codes}


class FakeSleep:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def history_store(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars(daily_bars("1111", HISTORY) + daily_bars("2222", HISTORY))
        yield store


def live_scan(store: SqlitePriceStore, stocks: list[StockInfo], source: FakeQuotes,
              sleep: FakeSleep | None = None, **settings):
    return intraday_scan(stocks, source=source, store=store, engine=ENGINE, trend_config=CONFIG.rules.trend,
                         settings=replace(MIS, **settings), now=NOW, sleep=sleep or FakeSleep())


def codes(hits) -> list[str]:
    return [hit.ctx.info.code for hit in hits]


def test_tentative_bar_from_quote_is_appended_to_history(history_store: SqlitePriceStore) -> None:
    report = live_scan(history_store, [stock("1111")], FakeQuotes([quote("1111")]))

    assert not report.is_replay and report.day == TODAY and report.scan_time == "13:10"
    (hit,) = report.hits
    assert (hit.side, [s.rule_name for s in hit.signals]) == (Side.LONG, ["三線多排"])
    assert hit.ctx.is_tentative and len(hit.ctx.bars) == len(HISTORY) + 1
    assert hit.ctx.bars.index[-1] == TODAY
    assert hit.ctx.today.to_dict() == {"open": 100.0, "high": 111.0, "low": 99.0, "close": 110.0, "volume": 1500}
    assert hit.change_pct == 10.0 and hit.price_substitute is None
    assert report.prev_close_mismatch == () and report.not_evaluated == ()


def test_bar_for_today_already_in_database_is_replaced_by_quote(history_store: SqlitePriceStore) -> None:
    history_store.upsert_bars([DailyBar("1111", TODAY, 100.0, 100.0, 100.0, 100.0, 1000)])

    (hit,) = live_scan(history_store, [stock("1111")], FakeQuotes([quote("1111")])).hits

    assert len(hit.ctx.bars) == len(HISTORY) + 1 and hit.ctx.today["close"] == 110.0


def test_non_trading_day_returns_none_without_requerying(history_store: SqlitePriceStore) -> None:
    # 非交易日 MIS 回傳最後一個交易日的資料
    source = FakeQuotes([quote("1111", day=LAST_TRADING_DAY, last=None)])

    assert live_scan(history_store, [stock("1111")], source) is None
    assert len(source.calls) == 1


def test_stock_without_trade_today_is_not_evaluated(history_store: SqlitePriceStore) -> None:
    source = FakeQuotes([quote("1111"), quote("2222", open=None, last=None, best_bid=None, best_ask=None)])

    report = live_scan(history_store, [stock("1111"), stock("2222")], source)

    assert codes(report.hits) == ["1111"]
    assert report.not_evaluated == ("2222",)
    assert len(source.calls) == 1                   # 今日尚無成交的股票不重新查詢


def test_stock_without_quote_or_with_another_date_is_not_evaluated(history_store: SqlitePriceStore) -> None:
    # 3333：代號或市場錯誤，MIS 沒有回傳
    source = FakeQuotes([quote("1111"), quote("2222", day=LAST_TRADING_DAY)])

    report = live_scan(history_store, [stock("1111"), stock("2222"), stock("3333")], source)

    assert report.not_evaluated == ("2222", "3333")


def test_missing_price_is_requeried_after_waiting(history_store: SqlitePriceStore) -> None:
    source = FakeQuotes([quote("1111", last=None), quote("2222")], [quote("1111")])
    sleep = FakeSleep()

    report = live_scan(history_store, [stock("1111"), stock("2222")], source, sleep)

    assert source.calls == [["1111", "2222"], ["1111"]]     # 只重新查詢沒有成交價的股票
    assert sleep.sleeps == [MIS.missing_price_wait_sec]
    (hit,) = [h for h in report.hits if h.ctx.info.code == "1111"]
    assert hit.ctx.today["close"] == 110.0 and hit.price_substitute is None


@pytest.mark.parametrize(
    ("best_bid", "price", "substitute"),
    [(109.5, 109.5, PriceSubstitute.BEST_BID), (None, 110.5, PriceSubstitute.BEST_ASK)],
    ids=["best-bid", "best-ask-when-no-bid"],      # 跌停鎖死時沒有買單，賣價即跌停價（D28）
)
def test_missing_price_falls_back_to_best_bid_then_ask(history_store: SqlitePriceStore, best_bid, price: float,
                                                       substitute: PriceSubstitute) -> None:
    source = FakeQuotes(*[[quote("1111", last=None, best_bid=best_bid, best_ask=110.5)]] * 3)

    report = live_scan(history_store, [stock("1111")], source, missing_price_retry=2)

    assert len(source.calls) == 3
    (hit,) = report.hits
    assert hit.ctx.today["close"] == price and hit.price_substitute is substitute
    assert hit.change_pct == pytest.approx(price - 100.0)


def test_missing_price_without_bid_or_ask_is_not_evaluated(history_store: SqlitePriceStore) -> None:
    source = FakeQuotes(*[[quote("1111", last=None, best_bid=None, best_ask=None), quote("2222")]] * 2)

    report = live_scan(history_store, [stock("1111"), stock("2222")], source)

    assert report.not_evaluated == ("1111",)


@pytest.mark.parametrize(
    ("prev_close", "mismatched"),
    [(100.0, ()), (104.0, ("1111",)), (None, ())],
    ids=["same", "ex-dividend-or-stale", "no-reference-price"],
)
def test_reference_price_is_checked_against_database(history_store: SqlitePriceStore, prev_close,
                                                     mismatched: tuple[str, ...]) -> None:
    report = live_scan(history_store, [stock("1111")], FakeQuotes([quote("1111", prev_close=prev_close)]))

    assert report.prev_close_mismatch == mismatched
    (hit,) = report.hits                            # 不一致時規則照常評估（規則的昨收仍用資料庫）
    assert [s.rule_name for s in hit.signals] == ["三線多排"]
    expected = None if prev_close is None else (110.0 - prev_close) / prev_close * 100
    assert hit.change_pct == pytest.approx(expected)    # 漲跌幅以 MIS 參考價計算


def test_stock_without_history_is_flagged_as_mismatch(history_store: SqlitePriceStore) -> None:
    report = live_scan(history_store, [stock("1111"), stock("3333")], FakeQuotes([quote("1111"), quote("3333")]))

    assert report.prev_close_mismatch == ("3333",)
    assert codes(report.hits) == ["1111"]           # 3333 只有今日一根 K 棒，算不出均線


@pytest.mark.parametrize(
    ("outcome", "message"),
    [([], "MIS 沒有回傳任何觀察清單股票的報價"),
     ([quote("1111", open=None, last=None)], "沒有任何股票 .* 有成交"),
     (DataSourceError("MIS 拒絕請求"), "盤中報價抓取失敗：MIS 拒絕請求")],
    ids=["no-quotes", "no-trades", "source-error"],
)
def test_scan_fails_when_nothing_can_be_evaluated(history_store: SqlitePriceStore, outcome, message: str) -> None:
    with pytest.raises(JobFailed, match=message):
        live_scan(history_store, [stock("1111")], FakeQuotes(outcome))


@pytest.fixture
def intraday_config(history_store: SqlitePriceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """回傳一個函式：以指定的報價取代 MIS，並把觀察清單縮小為 1111、2222。"""
    def configure(*quotes: Quote):
        monkeypatch.setattr(intraday_scan_module, "MisClient", lambda settings: FakeQuotes(list(quotes)))
        watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222")))
        return replace(CONFIG, watchlist=watchlist, settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))
    return configure


def test_run_prints_intraday_report(intraday_config) -> None:
    config = intraday_config(quote("1111"), quote("2222", open=None, last=None, best_bid=None, best_ask=None))
    out = io.StringIO()

    run(config, replay=None, notifier=None, out=out, now=NOW)

    assert out.getvalue() == "\n".join([
        f"【盤中選股】{TODAY} 13:10（暫定 K 棒，收盤前可能變動）",
        "",
        "━━ 多方 ━━",
        "▍PCB",
        "1111 股1111  110  ▲10.0%｜銅箔基板｜盤整",
        "  └ 三線多排",
        "",
        "觀察 2 檔｜觸發 1 檔（多方 1、空方 0）",
        "⚠️ 1 檔今日無成交或無報價，未評估：2222",
        "",
    ])


def test_run_prints_nothing_on_non_trading_day(intraday_config) -> None:
    config = intraday_config(quote("1111", day=LAST_TRADING_DAY), quote("2222", day=LAST_TRADING_DAY))
    out = io.StringIO()

    run(config, replay=None, notifier=None, out=out, now=NOW)

    assert out.getvalue() == ""
