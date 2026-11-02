"""訊號成績單（RESEARCH_PLAN.md 4.1）：一致性分類、報酬、基準、除權息調整、訊息與 CSV。

日期：2026-02-02（一）～ 02-06（五）為一週。手工資料的預期值都寫在各測試的註解中。
"""

import io
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR
from screener.config import load_config, load_research_config, rules_fingerprint
from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.jobs import signal_report as job
from screener.models import (DailyBar, DividendResult, Market, PriceSubstitute, Quote, ScanRun, Side, SignalRecord,
                             StockInfo, Trend)
from screener.research.signal_report import (APPEARED, CONSISTENT, EVENTS, NOT_EVALUATED, SNAPSHOT, VANISHED,
                                             Period, build_report, consistency, extremes, performance,
                                             read_m6_validation, report_period)
from screener.research.signal_report_output import format_messages, write_csv

CONFIG = load_config(REPO_CONFIG_DIR)
RESEARCH = load_research_config(REPO_CONFIG_DIR / "research.yaml")
ENGINE = Engine.from_config(CONFIG.rules)
FINGERPRINT = rules_fingerprint(CONFIG.rules)
MON, TUE, WED, THU, FRI = (date(2026, 2, 2) + timedelta(days=i) for i in range(5))
WEEK = Period(MON, FRI)


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=Market.TSE, group="PCB", subcategory="銅箔基板")


def bars(code: str, closes: list[float], last_day: date) -> list[DailyBar]:
    """以 last_day 為最後一天、往前連續平日的日 K（開高低等於收盤）。"""
    days = pd.bdate_range(end=last_day, periods=len(closes)).date
    return [DailyBar(code, day, close, close, close, close, 1000) for day, close in zip(days, closes)]


def quote(code: str, day: date, price: float | None, *, prev_close: float | None = 100.0,
          open: float | None = 100.0) -> Quote:
    traded = open is not None
    return Quote(code, day, open, 111.0 if traded else None, 99.0 if traded else None, price, prev_close,
                 1500 if traded else None, None, None)


def record(code: str, rule: str, price: float, *, side: Side = Side.LONG,
           substitute: PriceSubstitute | None = None) -> SignalRecord:
    name = {"ma_bull_alignment": "三線多排", "ma_bear_alignment": "三線空排"}[rule]
    return SignalRecord(code, rule, name, side, Trend.UNKNOWN, price, substitute, 1.0, "detail")


def scan(store: SqlitePriceStore, day: date, signals: list[SignalRecord], quotes: list[Quote], *,
         at: str = "13:10:00", fingerprint: str = FINGERPRINT) -> int:
    run = ScanRun(day=day, scanned_at=datetime.fromisoformat(f"{day}T{at}"), rules_fingerprint=fingerprint,
                  observed=len(quotes), evaluated=len(quotes), not_evaluated=0, substituted=0,
                  prev_close_mismatch=0, raw_file=None)
    return store.record_scan(run, signals, quotes)


def report_of(store: SqlitePriceStore, codes: list[str], period: Period = WEEK, horizons=(1, 5)):
    return build_report(store, [stock(c) for c in codes], engine=ENGINE, trend_config=CONFIG.rules.trend,
                        fingerprint=FINGERPRINT, period=period, horizons=horizons)


@pytest.fixture
def store(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        yield store


# --- 期間 -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("today", "end", "expected"),
    [
        (date(2026, 11, 14), None, (date(2026, 11, 9), date(2026, 11, 13))),       # 週六排程：上一週
        (date(2026, 11, 16), None, (date(2026, 11, 9), date(2026, 11, 13))),       # 錯過後週一補跑：仍是上一週
        (date(2026, 11, 13), None, (date(2026, 11, 2), date(2026, 11, 6))),        # 週五當天：這週還沒結束
        (date(2026, 11, 14), date(2026, 11, 11), (date(2026, 11, 9), date(2026, 11, 11))),  # --end 指定週三
    ],
    ids=["saturday", "monday-catch-up", "friday", "explicit-end"],
)
def test_report_period(today: date, end: date | None, expected: tuple[date, date]) -> None:
    assert report_period(end, today) == Period(*expected)


# --- 盤中 vs 收盤 -------------------------------------------------------------

@pytest.fixture
def consistency_store(store: SqlitePriceStore) -> SqlitePriceStore:
    # 20 根收盤 100 之後，週一收盤 110：形成三線多排（MA5 102 > MA10 101 > MA20 100.5）；一路 100 則沒有
    forms = [100.0] * 20 + [110.0]
    flat = [100.0] * 21
    store.upsert_bars(bars("1111", forms, MON) + bars("2222", flat, MON) + bars("3333", forms, MON)
                      + bars("4444", forms, MON))
    scan(store, MON,
         [record("1111", "ma_bull_alignment", 110.0),           # 收盤仍多排：一致；110 是漲停價（參考價 100）
          record("2222", "ma_bull_alignment", 109.0)],          # 13:10 漲到 109、收盤回到 100：收盤消失
         [quote("1111", MON, 110.0), quote("2222", MON, 109.0), quote("3333", MON, 104.0),  # 3333 13:10 沒觸發
          quote("4444", MON, None, open=None)])                 # 4444 13:10 尚無成交：沒有評估
    return store


def test_closing_signals_are_classified(consistency_store: SqlitePriceStore) -> None:
    report = report_of(consistency_store, ["1111", "2222", "3333", "4444"])

    rows = report.week_rows.set_index("code")
    assert rows["status"].to_dict() == {"1111": CONSISTENT, "2222": VANISHED, "3333": APPEARED,
                                        "4444": NOT_EVALUATED}
    assert rows["pushed"].to_dict() == {"1111": True, "2222": True, "3333": False, "4444": False}
    table = consistency(report)
    assert table.loc["ma_bull_alignment", [CONSISTENT, VANISHED, APPEARED]].tolist() == [1, 1, 1]
    assert table.loc["ma_bull_alignment", "rate"] == 50.0      # 一致 /（一致 + 收盤消失）；未評估的不列入（R4）


def test_limit_locked_signal_is_flagged(consistency_store: SqlitePriceStore) -> None:
    rows = report_of(consistency_store, ["1111", "2222", "3333", "4444"]).week_rows.set_index("code")

    # 參考價 100 的漲停價為 110（R5）；收盤才出現的訊號沒有 13:10 價格，無從判斷
    assert rows["limit_locked"].to_dict() == {"1111": True, "2222": False, "3333": None, "4444": None}


def test_only_the_first_push_of_a_day_is_used(consistency_store: SqlitePriceStore) -> None:
    # 13:20 又推播一次（例：手動執行 scan），訊號不同：成績單只用 13:10 那一次（D45）
    scan(consistency_store, MON, [record("3333", "ma_bull_alignment", 104.0)], [quote("3333", MON, 104.0)],
         at="13:20:00")

    report = report_of(consistency_store, ["1111", "2222", "3333", "4444"])

    assert report.week_rows.set_index("code").loc["3333", "status"] == APPEARED
    assert [d.run.scanned_at.time().isoformat() for d in report.scan_days] == ["13:10:00"]


def test_changed_rules_are_flagged(store: SqlitePriceStore) -> None:
    store.upsert_bars(bars("1111", [100.0] * 21, MON))
    scan(store, MON, [], [quote("1111", MON, 100.0)], fingerprint="old-rules")

    report = report_of(store, ["1111"])

    assert report.changed_rule_days == (MON,)
    assert "⚠️ 期間內規則有變更：02/02 推播時的規則檔與目前不同" in format_messages(
        report, m6={}, top_n=3, csv_path=Path("x.csv"), limit=2000)[0]


def test_day_without_bars_fails(store: SqlitePriceStore) -> None:
    store.upsert_bars(bars("1111", [100.0] * 5, date(2026, 1, 30)))    # 日 K 只到上週五：尚未執行 update
    scan(store, MON, [], [quote("1111", MON, 100.0)])

    with pytest.raises(JobFailed, match="資料庫沒有 2026-02-02 的日 K"):
        report_of(store, ["1111"])


# --- 報酬、基準與除權息調整 ----------------------------------------------------

@pytest.fixture
def returns_store(store: SqlitePriceStore) -> SqlitePriceStore:
    """週一 13:10 推播 5555 三線多排（多方）、7777 三線空排（空方）；週二收盤後持有 1 日：

    - 5555：13:10 進場 100，週二除息，快照的參考價 96 ≠ 週一收盤 100 → 因子 0.96；週二收盤 97.92
      → 97.92 / (100 × 0.96) − 1 = +2%
    - 6666：13:10 進場 50（只在基準中）。週二沒有快照，改用事件資料：官方除權息結果 50 → 48，因子 0.96；
      週二收盤 48.96 → 48.96 / (50 × 0.96) − 1 = +2%
    - 7777：13:10 進場 20，週二沒有快照、事件資料也沒涵蓋 → 未調整；週二收盤 19 → −5%，空方 +5%
    - 基準（不分方向）=（2 + 2 − 5）/ 3 = −1/3%。多方超額 = 2 − (−1/3) = 2.333%；空方的基準取負號
      = +1/3%，超額 = 5 − 1/3 = 4.667%
    """
    store.upsert_bars(bars("5555", [100.0, 100.0, 97.92], TUE) + bars("6666", [50.0, 50.0, 48.96], TUE)
                      + bars("7777", [20.0, 20.0, 19.0], TUE))
    store.upsert_dividend_results([DividendResult("6666", TUE, 50.0, 48.0)])
    store.set_event_coverage("6666", EventCoverage(date(2026, 1, 1), FRI))
    scan(store, MON, [record("5555", "ma_bull_alignment", 100.0),
                      record("7777", "ma_bear_alignment", 20.0, side=Side.SHORT)],
         [quote("5555", MON, 100.0), quote("6666", MON, 50.0, prev_close=50.0),
          quote("7777", MON, 20.0, prev_close=20.0)])
    scan(store, TUE, [], [quote("5555", TUE, 97.0, prev_close=96.0)])
    return store


def test_returns_baseline_and_excess(returns_store: SqlitePriceStore) -> None:
    rows = report_of(returns_store, ["5555", "6666", "7777"], period=Period(MON, TUE)).week_rows.set_index("code")

    assert rows.loc["5555", "ret_1"] == pytest.approx(2.0)
    assert rows.loc["5555", "base_1"] == pytest.approx(-1 / 3)
    assert rows.loc["5555", "excess_1"] == pytest.approx(2 + 1 / 3)
    assert rows.loc["7777", "ret_1"] == pytest.approx(5.0)       # 空方取負號
    assert rows.loc["7777", "base_1"] == pytest.approx(1 / 3)
    assert rows.loc["7777", "excess_1"] == pytest.approx(5 - 1 / 3)
    assert rows["ret_5"].isna().all()                           # 期間內持有不到 5 日，不列入


def test_ex_dividend_adjustment_sources(returns_store: SqlitePriceStore) -> None:
    rows = report_of(returns_store, ["5555", "6666", "7777"], period=Period(MON, TUE)).week_rows.set_index("code")

    assert rows.loc["5555", "adjustment"] == f"02/03 {SNAPSHOT} ×0.9600"
    assert not rows.loc["5555", "unadjusted"]
    assert rows.loc["7777", "adjustment"] == "未調整：02/03 沒有快照與事件資料"
    assert rows.loc["7777", "unadjusted"]


def test_event_data_is_used_when_there_is_no_snapshot(returns_store: SqlitePriceStore) -> None:
    # 6666 只在基準中；改成推播 6666 的訊號，確認它的調整來源是事件資料
    scan(returns_store, MON, [record("6666", "ma_bull_alignment", 50.0)], [quote("6666", MON, 50.0)],
         at="13:05:00")      # 比原本的 13:10 早，成為當天第一次推播

    rows = report_of(returns_store, ["5555", "6666", "7777"], period=Period(MON, TUE)).week_rows

    (row,) = rows[rows["pushed"]].itertuples()
    assert row.code == "6666" and row.ret_1 == pytest.approx(2.0)
    assert row.adjustment == f"02/03 {EVENTS} ×0.9600"


def test_week_holds_to_the_last_trading_day(store: SqlitePriceStore) -> None:
    # 週一、週五各一個訊號，持有到週五收盤 110：週一 100 進場 +10%；週五 13:10 108 進場只算到當天收盤 +1.85%
    store.upsert_bars(bars("1111", [100.0, 101.0, 102.0, 103.0, 104.0, 110.0], FRI))
    scan(store, MON, [record("1111", "ma_bull_alignment", 100.0)], [quote("1111", MON, 100.0, prev_close=100.0)])
    scan(store, FRI, [record("1111", "ma_bull_alignment", 108.0)], [quote("1111", FRI, 108.0, prev_close=104.0)])

    report = report_of(store, ["1111"])

    rows = report.week_rows
    assert rows["ret_week"].tolist() == pytest.approx([10.0, (110 / 108 - 1) * 100])
    assert rows["ret_close"].tolist() == pytest.approx([(101 / 100 - 1) * 100, (110 / 108 - 1) * 100])
    assert report.missing_days == (TUE, WED, THU)               # 有日 K、沒有推播紀錄的交易日
    stats = performance(rows, "week", ["ma_bull_alignment"])
    assert stats.loc["ma_bull_alignment", "count"] == 2
    assert stats.loc["ma_bull_alignment", "win_rate"] == 100.0
    best, worst = extremes(report, 1)
    assert (best[0].day, best[0].ret) == (MON, pytest.approx(10.0)) and worst[0].day == FRI


def test_cumulative_uses_earlier_weeks(store: SqlitePriceStore) -> None:
    # 前一週（01/26）的訊號，持有 5 日在期間內完成：列入累積，但不在本週的明細。
    # 日 K：01/23～01/30 收盤 100，02/02～02/06 為 101～105。01/26 進場 100：
    # 持有 1 日到 01/27 收盤 100 → 0%；持有 5 日到 02/02 收盤 101 → +1%
    store.upsert_bars(bars("1111", [100.0] * 6 + [101.0, 102.0, 103.0, 104.0, 105.0], FRI))
    scan(store, MON - timedelta(days=7), [record("1111", "ma_bull_alignment", 100.0)],
         [quote("1111", MON - timedelta(days=7), 100.0)])

    report = report_of(store, ["1111"])

    assert report.week_rows.empty and report.first_day == MON - timedelta(days=7)
    (row,) = report.pushed_rows.itertuples()
    assert row.ret_1 == pytest.approx(0.0) and row.ret_5 == pytest.approx(1.0)
    assert performance(report.pushed_rows, 5, ["ma_bull_alignment"])["count"].tolist() == [1]


# --- 訊息與 CSV --------------------------------------------------------------

def test_message_format(consistency_store: SqlitePriceStore) -> None:
    report = report_of(consistency_store, ["1111", "2222", "3333", "4444"])
    m6 = {("三線多排", 1): -0.15}

    (message,) = format_messages(report, m6=m6, top_n=3, csv_path=Path("data/research/signal_report/x.csv"),
                                 limit=2000)

    lines = message.splitlines()
    assert lines[:2] == ["【訊號成績單】2026-02-02 ～ 02-06（1 個交易日）",
                         "推播 1 天｜訊號 2 個（多方 2、空方 0）｜替代價 0 次｜未評估 0 次"]
    assert "━━ 盤中 vs 收盤 ━━" in lines
    assert "三線多排  一致 50%（收盤消失 1、收盤才出現 1）" in lines
    assert "13:10 未評估、收盤才觸發：1 個（不列入）" in lines
    assert "━━ 本週表現（13:10 進場，持有到 02/02 收盤）━━" in lines
    # 週一就是期間最後一個交易日：1111 13:10 110 → 收盤 110 為 0%，2222 109 → 100 為 −8.2569%，平均 −4.13%。
    # 基準：1111 0%、2222 −8.2569%、3333 104 → 110 為 +5.7692%（4444 沒有 13:10 價格），平均 −0.8292%；
    # 平均超額 = −4.1284 + 0.8292 = −3.30%
    assert "三線多排  2 次｜平均 -4.13%｜勝率 0%｜超額 -3.30%" in lines
    assert lines[-2:] == ["明細：data/research/signal_report/x.csv", "僅為統計紀錄，不構成投資建議"]
    assert "━━ 累積（M7 上線以來，2026-02-02 起，持有 1 日）━━" in lines
    assert "（尚無持有滿 1 日的訊號）" in lines


def test_long_message_is_split_by_section(consistency_store: SqlitePriceStore) -> None:
    report = report_of(consistency_store, ["1111", "2222", "3333", "4444"])

    messages = format_messages(report, m6={}, top_n=3, csv_path=Path("x.csv"), limit=150)

    assert len(messages) > 1 and all(len(m) <= 150 for m in messages)
    assert messages[0].startswith("【訊號成績單】")


def test_csv_has_one_row_per_signal(consistency_store: SqlitePriceStore, tmp_path: Path) -> None:
    path = tmp_path / "out" / "2026-02-06.csv"

    write_csv(report_of(consistency_store, ["1111", "2222", "3333", "4444"]), path)

    table = pd.read_csv(path, encoding="utf-8-sig", dtype={"代號": str})
    assert table["代號"].tolist() == ["1111", "2222", "3333", "4444"]
    assert table["盤中 vs 收盤"].tolist() == [CONSISTENT, VANISHED, APPEARED, NOT_EVALUATED]
    assert table.loc[0, "13:10 現價"] == 110.0 and table.loc[0, "當日收盤"] == 110.0
    assert table.loc[1, "13:10→收盤(%)"] == pytest.approx(-8.2569, abs=1e-4)
    assert {"本週報酬(%)", "報酬1日(%)", "超額5日(%)", "除權息調整", "13:10 漲跌停鎖死"} <= set(table.columns)


def test_m6_validation_is_read_from_backtest_summary(tmp_path: Path) -> None:
    path = tmp_path / "summary.csv"
    base = {"判斷價格": "未還原", "規則": "三線多排", "方向": "多方", "段": "驗證段", "樣本": "去重",
            "漲跌停鎖死": "排除", "跳空事件": "含", "持有天數": 5, "次數": 100, "平均超額報酬(%)": -0.15}
    pd.DataFrame([base, {**base, "段": "觀察段", "平均超額報酬(%)": -0.18},
                  {**base, "判斷價格": "還原", "平均超額報酬(%)": 9.9},
                  {**base, "持有天數": 1, "平均超額報酬(%)": 0.02}]).to_csv(path, index=False, encoding="utf-8-sig")

    assert read_m6_validation(path, (1, 5)) == {("三線多排", 5): -0.15, ("三線多排", 1): 0.02}
    assert read_m6_validation(tmp_path / "missing.csv", (1, 5)) == {}


# --- 指令 -------------------------------------------------------------------

class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, content: str) -> None:
        self.sent.append(content)


def test_job_writes_csv_and_pushes(consistency_store: SqlitePriceStore, tmp_path: Path) -> None:
    config = replace(CONFIG, watchlist=replace(CONFIG.watchlist, stocks=tuple(stock(c) for c in
                                                                              ("1111", "2222", "3333", "4444"))),
                     settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))
    research = replace(RESEARCH, signal_report=replace(RESEARCH.signal_report, output_dir=tmp_path / "report",
                                                       backtest_summary=tmp_path / "missing.csv"))
    notifier, out = FakeNotifier(), io.StringIO()

    job.run(config, research, end=None, notifier=notifier, today=date(2026, 2, 7))
    job.run(config, research, end=FRI, notifier=None, out=out)

    assert notifier.sent[0].startswith("【訊號成績單】2026-02-02 ～ 02-06")
    assert out.getvalue().startswith("【訊號成績單】2026-02-02 ～ 02-06")
    assert (tmp_path / "report" / "2026-02-06.csv").is_file()
    assert "M6 驗證段" not in "".join(notifier.sent)          # 沒有回測結果時不顯示
