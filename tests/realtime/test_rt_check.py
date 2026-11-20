"""rt-check：1 分 K 合起來和日 K 比對（REALTIME_PLAN.md 7.2 第 4、8 點）。

手工構造的資料（CODE，日 K 量都是 100 張）：

| 日期 | 日 K 開／高／低／收 | 1 分 K | 預期 |
|---|---|---|---|
| 11-12 | 50／52／49／51 | 09:00 50／51／49／50.5 量 40；10:00 50.5／52／50.5／51.5 量 30；13:30 51 量 27 | 完全相同；量 97 ÷ 100 = 0.97 |
| 11-13 | 50／52／49／51 | 同 11-12，但 13:30 收 51.5 | 收盤不同 |
| 11-16 | 50／52／49／51 | 08:59 50 量 10；09:00 50／52／49／51 量 40；13:24 51 量 10 | 開高低收相同（開 = 08:59 那根）；量 60 ÷ 100 = 0.6 < 0.8；沒有 13:30 那根；交易時段以外 1 根（08:59） |
| 11-17 | 50／52／49／51 | 09:00 50／52／49／50 量 60；13:30 51 量 50 | 量 110 ÷ 100 = 1.1 > 1 |
| 11-18 | 50／52／49／51 | 查過、沒有資料（days.bars = 0）；上市日 11-19 之前 | 有日 K、沒有 1 分 K（上市櫃前） |
| 11-19 | 50／52／49／51 | 還沒回補 | 只計數 |
| 11-20 | 沒有日 K | 09:00 50 量 1 | 有 1 分 K、沒有日 K |
"""

import io
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

from rt_fixtures import SESSION
from screener.config import load_config
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed, rt_check
from screener.models import DailyBar, Listing, Market
from screener.realtime.bars import Bar
from screener.realtime.config import load_daytrade_config, load_realtime_config
from screener.realtime.intraday import FetchRecord, IntradayStore
from screener.realtime.quality import COMPARED, NO_DAILY, NO_DATA, NOT_FETCHED, aggregate_days, check_stock

CODE = "4977"
DAILY_DAYS = [date(2026, 11, d) for d in (12, 13, 16, 17, 18, 19)]
LISTED_ON = date(2026, 11, 19)


def _bar(day: date, hhmm: str, o: float, h: float, lo: float, c: float, v: int) -> Bar:
    return Bar(datetime.combine(day, time.fromisoformat(hhmm)), 1, o, h, lo, c, v, c * v * 1000)


BARS = {
    date(2026, 11, 12): [_bar(date(2026, 11, 12), "09:00", 50, 51, 49, 50.5, 40),
                         _bar(date(2026, 11, 12), "10:00", 50.5, 52, 50.5, 51.5, 30),
                         _bar(date(2026, 11, 12), "13:30", 51, 51, 51, 51, 27)],
    date(2026, 11, 13): [_bar(date(2026, 11, 13), "09:00", 50, 51, 49, 50.5, 40),
                         _bar(date(2026, 11, 13), "10:00", 50.5, 52, 50.5, 51.5, 30),
                         _bar(date(2026, 11, 13), "13:30", 51.5, 51.5, 51.5, 51.5, 27)],
    date(2026, 11, 16): [_bar(date(2026, 11, 16), "08:59", 50, 50, 50, 50, 10),
                         _bar(date(2026, 11, 16), "09:00", 50, 52, 49, 51, 40),
                         _bar(date(2026, 11, 16), "13:24", 51, 51, 51, 51, 10)],
    date(2026, 11, 17): [_bar(date(2026, 11, 17), "09:00", 50, 52, 49, 50, 60),
                         _bar(date(2026, 11, 17), "13:30", 51, 51, 51, 51, 50)],
    date(2026, 11, 20): [_bar(date(2026, 11, 20), "09:00", 50, 50, 50, 50, 1)],
}


def _frame(bars: list[Bar]) -> pd.DataFrame:
    return pd.DataFrame([(b.start.date().isoformat(), b.start.strftime("%H:%M"), b.open, b.high, b.low, b.close,
                          b.volume) for b in bars], columns=["date", "minute", "open", "high", "low", "close", "volume"])


def _daily() -> pd.DataFrame:
    return pd.DataFrame([(day.isoformat(), 50.0, 52.0, 49.0, 51.0, 100) for day in DAILY_DAYS],
                        columns=["date", "open", "high", "low", "close", "volume"])


def test_aggregate_days_first_open_last_close() -> None:
    agg = aggregate_days(_frame(BARS[date(2026, 11, 12)]), SESSION)

    row = agg.loc["2026-11-12"]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"], row["bars"]) == (50, 52, 49, 51, 97, 3)
    assert row["has_open_bar"] and row["has_close_bar"] and row["outside_session"] == 0


def test_check_stock_statuses_and_comparisons() -> None:
    bars = [bar for day_bars in BARS.values() for bar in day_bars]
    fetched = {day: len(BARS.get(day, [])) for day in (*BARS, date(2026, 11, 18))}

    result = check_stock(_daily(), fetched, _frame(bars), SESSION, listed_on=LISTED_ON).set_index("date")

    assert result["status"].to_dict() == {"2026-11-12": COMPARED, "2026-11-13": COMPARED, "2026-11-16": COMPARED,
                                          "2026-11-17": COMPARED, "2026-11-18": NO_DATA, "2026-11-19": NOT_FETCHED,
                                          "2026-11-20": NO_DAILY}
    compared = result[result["status"] == COMPARED]
    assert compared["same_ohlc"].to_dict() == {"2026-11-12": True, "2026-11-13": False, "2026-11-16": True,
                                               "2026-11-17": True}
    assert compared["volume_ratio"].to_dict() == {"2026-11-12": 0.97, "2026-11-13": 0.97, "2026-11-16": 0.6,
                                                  "2026-11-17": 1.1}
    assert not result.loc["2026-11-16", "has_close_bar"] and result.loc["2026-11-16", "outside_session"] == 1
    assert bool(result.loc["2026-11-18", "before_listing"]) and not bool(result.loc["2026-11-19", "before_listing"])


@pytest.fixture
def setup(config_dir: Path):
    # CODE 當作轉市場的股票（L25）：上市櫃日 11-19 之前也檢查，before_listing 的標示才看得到
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    realtime = replace(realtime, backfill=replace(realtime.backfill, market_transfers=(CODE,)))
    daytrade = load_daytrade_config(config_dir / "daytrade.yaml")
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_bars([DailyBar(CODE, day, 50.0, 52.0, 49.0, 51.0, 100) for day in DAILY_DAYS])
        store.upsert_listings([Listing(CODE, Market.OTC, LISTED_ON)])
    with IntradayStore(realtime.backfill.db_path) as store:
        all_bars = [bar for day_bars in BARS.values() for bar in day_bars]
        counts = {day: len(BARS.get(day, [])) for day in (*BARS, date(2026, 11, 18))}
        store.save_query(FetchRecord(CODE, date(2026, 11, 12), date(2026, 11, 20), datetime(2026, 11, 21, 14, 30),
                                     len(counts), len(all_bars), 3, 700, "ok"), all_bars, counts)
        store.log_failure(FetchRecord(CODE, date(2026, 11, 19), date(2026, 11, 19), datetime(2026, 11, 21, 14, 31),
                                      1, 0, 0, 900, "failed", "kbars 失敗（3 次）：TimeoutError"))
    return config, realtime, daytrade


def test_rt_check_writes_report_and_detail_csvs(setup) -> None:
    config, realtime, daytrade = setup
    out = io.StringIO()

    out_dir = rt_check.run(config, realtime, daytrade, codes=[CODE], out=out, now=datetime(2026, 11, 21, 15, 0))

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "| 已回補，有 1 分 K | 4 |" in report and "| 還沒回補 | 1 |" in report
    assert "| 已回補，沒有資料 | 1 |" in report and "| 有 1 分 K、沒有日 K | 1 |" in report
    assert "比對 4 個股日：完全相同 3、不同 1" in report
    assert "| 4977 眾達-KY | 2026-11-13 | 50／52／49／51 | 50／52／49／51.5 | 3 |" in report
    assert "低於 0.8：1 個股日；高於 1：1 個股日" in report
    assert "- 有日 K、沒有 1 分 K：1 個股日，其中上市櫃日之前（興櫃，D38）1" in report
    assert "- 沒有 13:30 那根：1 個股日" in report
    assert "| 2026 | 4 | 2.8 | 100.0% | 75.0% | 1 | 3 |" in report
    assert "- 失敗：1 次" in report and "TimeoutError" in report

    mismatch = pd.read_csv(out_dir / "ohlc_mismatch.csv", dtype={"code": str})
    assert mismatch[["code", "date"]].values.tolist() == [[CODE, "2026-11-13"]]
    outliers = pd.read_csv(out_dir / "volume_outliers.csv")
    assert outliers[["date", "problem"]].values.tolist() == [["2026-11-16", "低於下限"], ["2026-11-17", "高於 1"]]
    missing = pd.read_csv(out_dir / "missing.csv")
    assert missing[["date", "problem"]].values.tolist() == [["2026-11-18", "有日 K、沒有 1 分 K"],
                                                            ["2026-11-20", "有 1 分 K、沒有日 K"],
                                                            ["2026-11-16", "沒有 13:30 那根"]]
    assert "開高低收不一致 1、量的比例超出範圍 2、缺漏 3；還沒回補 1 個股日" in out.getvalue()


def test_rt_check_starts_at_listing_date(setup) -> None:
    # 不是轉市場的股票：上市櫃日（11-19）之前是興櫃交易，不回補也不檢查（L25）；只剩 11-19（還沒回補）與 11-20（沒有日 K）
    config, realtime, daytrade = setup
    realtime = replace(realtime, backfill=replace(realtime.backfill, market_transfers=()))

    out_dir = rt_check.run(config, realtime, daytrade, codes=[CODE], out=io.StringIO())

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "| screener.db 的股日 | 1 |" in report and "| 還沒回補 | 1 |" in report
    assert "| 已回補，有 1 分 K | 0 |" in report and "| 有 1 分 K、沒有日 K | 1 |" in report


def test_rt_check_requires_intraday_db(config_dir: Path) -> None:
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    daytrade = load_daytrade_config(config_dir / "daytrade.yaml")

    with pytest.raises(JobFailed, match="先執行 rt-backfill"):
        rt_check.run(config, realtime, daytrade, out=io.StringIO())


def test_rt_check_rejects_codes_outside_watchlist(setup) -> None:
    config, realtime, daytrade = setup

    with pytest.raises(JobFailed, match="不在觀察清單的代號"):
        rt_check.run(config, realtime, daytrade, codes=["2486"], out=io.StringIO())
