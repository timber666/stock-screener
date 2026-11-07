"""盤中掃描（PROJECT_PLAN.md 7.2、8.2）與回放模式（第 12 節）的完整流程。"""

import gzip
import io
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, load_fixture, make_bars, to_bars
from screener.config import load_config, rules_fingerprint
from screener.data.base import DataSourceError
from screener.data.db import SqlitePriceStore
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs import intraday_scan as intraday_scan_module
from screener.jobs.intraday_scan import intraday_scan, replay_scan, run
from screener.models import (DailyBar, Market, PriceSubstitute, Quote, ScanRun, Side, SignalRecord, StockContext,
                             StockInfo, Trend)
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


def settings_in(tmp_path: Path):
    """資料庫與快照都指向暫存目錄。"""
    return replace(CONFIG.settings, db_path=tmp_path / "screener.db", snapshot_dir=tmp_path / "snapshots")


@pytest.fixture
def replay_config(store: SqlitePriceStore, tmp_path: Path):
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222"), stock("4444")))
    return replace(CONFIG, watchlist=watchlist, settings=settings_in(tmp_path))


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


# --- 符合進場（5.5）----------------------------------------------------------

# bull_zigzag 的趨勢為多頭，最後一天（02-05）收盤 114 = 昨高，5 根前的收盤 123，所以今日現價 > 123 時 MA5 上揚。
# 現價 122～125 都觸發 E1。以昨收 114 計算的漲停價為 125（114 × 1.1 = 125.4，升降單位 0.5）。
ZIGZAG = to_bars(load_fixture("bull_zigzag"))
ZIGZAG_NOW = datetime(2026, 2, 6, 13, 10)


def entry_hit(tmp_path: Path, *, last: float, prev_close: float = 114.0):
    """盤中掃描 bull_zigzag 的下一個交易日，回傳唯一的 Hit。"""
    quote_ = Quote(code="1111", date=ZIGZAG_NOW.date(), open=120.0, high=last, low=120.0, last=last,
                   prev_close=prev_close, volume=1500, best_bid=last - 0.5, best_ask=last + 0.5)
    with SqlitePriceStore(tmp_path / "zigzag.db") as store:
        store.upsert_bars(daily_bars("1111", ZIGZAG))
        report = intraday_scan([stock("1111")], source=FakeQuotes([quote_]), store=store, engine=ENGINE,
                               trend_config=CONFIG.rules.trend, settings=MIS, now=ZIGZAG_NOW, sleep=FakeSleep())
    (hit,) = report.hits
    assert "bull_ma5_breakout" in [signal.rule_id for signal in hit.signals]
    return hit


@pytest.mark.parametrize(
    ("last", "prev_close", "expected"),
    [(124.0, 114.0, True), (122.0, 114.0, False), (125.0, 114.0, False), (125.0, 116.0, True)],
    ids=["matches", "ma5-falling", "at-limit-up", "limit-up-from-mis-reference"],
)
def test_intraday_marks_entry(tmp_path: Path, last: float, prev_close: float, expected: bool) -> None:
    assert entry_hit(tmp_path, last=last, prev_close=prev_close).entry is expected


def zigzag_with_today(close: float) -> pd.DataFrame:
    today = pd.DataFrame({"open": [120.0], "high": [close], "low": [120.0], "close": [close], "volume": [1500]},
                         index=pd.Index([ZIGZAG_NOW.date()], name="date"))
    return pd.concat([ZIGZAG, today])


@pytest.mark.parametrize(("close", "expected"), [(124.0, True), (125.0, False)], ids=["matches", "at-limit-up"])
def test_replay_marks_entry_with_limit_from_previous_close(tmp_path: Path, close: float, expected: bool) -> None:
    with SqlitePriceStore(tmp_path / "zigzag.db") as store:
        store.upsert_bars(daily_bars("1111", zigzag_with_today(close)))
        report = scan(store, [stock("1111")], day=ZIGZAG_NOW.date())

    (hit,) = report.hits
    assert hit.entry is expected


def test_entry_mark_reaches_the_message_and_only_on_that_stock(tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars(daily_bars("1111", zigzag_with_today(124.0)))
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"),))
    config = replace(CONFIG, watchlist=watchlist, settings=settings_in(tmp_path))
    out = io.StringIO()

    run(config, replay=ZIGZAG_NOW.date(), notifier=None, out=out)

    assert "1111 股1111  124  ▲8.8%｜銅箔基板｜多頭｜✅ 符合進場\n  └ 多頭突破五日線紅K" in out.getvalue()


@pytest.mark.parametrize(
    ("side", "price", "expected"),
    [(Side.LONG, 110.0, True), (Side.LONG, 109.5, False), (Side.SHORT, 90.0, True), (Side.SHORT, 90.5, False)],
    ids=["long-at-up", "long-below-up", "short-at-down", "short-above-down"],
)
def test_at_price_limit_depends_on_the_side(side: Side, price: float, expected: bool) -> None:
    ctx = StockContext(info=stock("1111"), bars=make_bars([100.0, price]), is_tentative=False, pivots=[], trend=Trend.RANGE)

    assert intraday_scan_module._at_price_limit(ctx, side, reference=100.0) is expected


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


def test_requery_failure_keeps_first_quotes_and_uses_substitute(history_store: SqlitePriceStore,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    # 第一次查詢成功、重新查詢失敗（例：MIS 盤中限流）：不中止掃描，沿用第一次的報價改用替代價（D40）
    source = FakeQuotes([quote("1111", last=None), quote("2222")], DataSourceError("MIS 請求失敗，已重試 3 次"))

    with caplog.at_level(logging.WARNING):
        report = live_scan(history_store, [stock("1111"), stock("2222")], source, missing_price_retry=2)

    assert len(source.calls) == 2                   # 失敗後不再重新查詢
    assert sorted(codes(report.hits)) == ["1111", "2222"] and report.not_evaluated == ()
    (hit,) = [h for h in report.hits if h.ctx.info.code == "1111"]
    assert hit.ctx.today["close"] == 109.5 and hit.price_substitute is PriceSubstitute.BEST_BID
    assert "重新查詢成交價失敗，沿用第一次的報價" in caplog.text


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
    """回傳一個函式：以指定的報價取代 MIS，並把觀察清單縮小為 1111、2222。

    傳入 Quote 時為只查詢一次的報價；傳入多個 list 時依序為每一輪（重新查詢成交價）的報價。
    """
    def configure(*quotes: Quote | list[Quote]):
        rounds = list(quotes) if quotes and isinstance(quotes[0], list) else [list(quotes)]
        monkeypatch.setattr(intraday_scan_module, "MisClient", lambda settings: FakeQuotes(*rounds))
        watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222")))
        return replace(CONFIG, watchlist=watchlist, settings=settings_in(tmp_path))
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


# --- 推播後寫入訊號紀錄（D45、D46）-------------------------------------------

def with_raw(quote: Quote, z: str) -> Quote:
    """附上 MIS 原始資料列（只放幾個欄位，供確認存檔內容）。"""
    return replace(quote, raw={"c": quote.code, "z": z, "f": "425_729_314_270_208_"})


def recorded(tmp_path: Path):
    """回傳（執行紀錄、訊號、快照）；沒有紀錄時為 None。"""
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        runs = store.get_scan_runs(date(2000, 1, 1), date(2100, 1, 1))
        if not runs:
            return None
        (scan,) = runs
        return scan, store.get_signal_records(scan.id), store.get_snapshot(scan.id)


def test_pushed_scan_is_recorded(intraday_config, tmp_path: Path) -> None:
    # 1111 觸發三線多排；2222 第一次查詢沒有成交價，重新查詢後有：快照與原始資料都以重新查詢的取代
    config = intraday_config(
        [with_raw(quote("1111"), "110.0000"), with_raw(quote("2222", last=None), "-")],
        [with_raw(quote("2222", last=100.0), "100.0000")],
    )
    notifier = FakeNotifier()

    run(config, replay=None, notifier=notifier, now=NOW)

    (message,) = notifier.sent
    scan, signals, snapshot = recorded(tmp_path)
    raw_file = tmp_path / "snapshots" / "2026-02-02_131000.json.gz"
    assert scan == ScanRun(day=TODAY, scanned_at=NOW, rules_fingerprint=rules_fingerprint(config.rules), observed=2,
                           evaluated=2, not_evaluated=0, substituted=0, prev_close_mismatch=0,
                           raw_file=raw_file.as_posix(), id=scan.id)
    assert signals == [SignalRecord("1111", "ma_bull_alignment", "三線多排", Side.LONG, Trend.RANGE, 110.0, None,
                                    10.0, "MA5 102 > MA10 101 > MA20 100.5")]
    assert "1111 股1111  110" in message and "三線多排" in message     # 與推播內容一致
    assert snapshot == {"1111": quote("1111"), "2222": quote("2222", last=100.0)}
    with gzip.open(raw_file, "rt", encoding="utf-8") as f:
        archived = json.load(f)
    assert archived["date"] == TODAY.isoformat() and archived["scanned_at"] == "2026-02-02T13:10:00"
    assert [(r["c"], r["z"]) for r in archived["records"]] == [("1111", "110.0000"), ("2222", "100.0000")]


def test_counts_of_substituted_and_not_evaluated_are_recorded(intraday_config, tmp_path: Path) -> None:
    config = intraday_config(
        [quote("1111", last=None), quote("2222", open=None, last=None, best_bid=None, best_ask=None)],
        [quote("1111", last=None)],
    )

    run(config, replay=None, notifier=FakeNotifier(), now=NOW)

    scan, signals, snapshot = recorded(tmp_path)
    assert (scan.evaluated, scan.not_evaluated, scan.substituted) == (1, 1, 1)
    assert [(s.price, s.price_substitute) for s in signals] == [(109.5, PriceSubstitute.BEST_BID)]
    assert set(snapshot) == {"1111", "2222"}               # 未評估的股票也保存快照


def test_dry_run_is_not_recorded(intraday_config, tmp_path: Path) -> None:
    run(intraday_config(quote("1111"), quote("2222")), replay=None, notifier=None, out=io.StringIO(), now=NOW)

    assert recorded(tmp_path) is None and not (tmp_path / "snapshots").exists()


def test_replay_is_not_recorded(replay_config, tmp_path: Path) -> None:
    notifier = FakeNotifier()

    run(replay_config, replay=DAY, notifier=notifier)

    assert notifier.sent and recorded(tmp_path) is None and not (tmp_path / "snapshots").exists()


def test_failed_push_is_not_recorded(intraday_config, tmp_path: Path) -> None:
    with pytest.raises(JobFailed, match="推播失敗"):
        run(intraday_config(quote("1111"), quote("2222")), replay=None, notifier=FakeNotifier(fail=True), now=NOW)

    assert recorded(tmp_path) is None


def test_non_trading_day_is_not_recorded(intraday_config, tmp_path: Path) -> None:
    config = intraday_config(quote("1111", day=LAST_TRADING_DAY), quote("2222", day=LAST_TRADING_DAY))

    run(config, replay=None, notifier=FakeNotifier(), now=NOW)

    assert recorded(tmp_path) is None


def test_record_failure_after_push_fails_the_job(intraday_config, tmp_path: Path) -> None:
    (tmp_path / "snapshots").write_text("不是資料夾", encoding="utf-8")       # 無法建立快照的資料夾
    notifier = FakeNotifier()

    with pytest.raises(JobFailed, match="已推播 Discord，但訊號紀錄寫入失敗"):
        run(intraday_config(quote("1111"), quote("2222")), replay=None, notifier=notifier, now=NOW)

    assert len(notifier.sent) == 1                          # 已推播的訊息不受影響
    assert recorded(tmp_path) is None
