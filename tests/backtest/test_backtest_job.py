"""訊號回測的整體流程、統計與輸出（PROJECT_PLAN.md 14.1）。"""

import io
import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, make_bars, read_yaml, write_yaml
from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, EXIT_OK, main
from screener.analysis.adjustment import adjust_bars, event_factors
from screener.backtest.signals import StockHistory
from screener.backtest.stats import select
from screener.config import ConfigError, load_backtest_config, load_config
from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.jobs.backtest import backtest_histories, build_engine, run
from screener.models import DailyBar, Listing, Market, StockInfo

CONFIG = load_config(REPO_CONFIG_DIR)
BACKTEST = replace(load_backtest_config(REPO_CONFIG_DIR / "backtest.yaml"), horizons=(1, 2), summary_horizon=1,
                   validation_start=date(2026, 3, 1), dedupe_days=5)

# 1111：第 21 根形成三線多排（收盤 105，未達漲停），隔天漲到 115.5；2222：一路持平。基準為兩檔的平均
RISES = make_bars([100.0] * 20 + [105.0, 115.5, 115.5])
FLAT = make_bars([100.0] * 23)
DAY = RISES.index[20]


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=Market.TSE, group="PCB", subcategory="銅箔基板")


def history(code: str, bars: pd.DataFrame) -> StockHistory:
    return StockHistory(stock(code), bars, adjust_bars(bars, [], []), event_factors(bars, [], []))


def backtest(workers: int = 1, **kwargs):
    histories = [history("1111", RISES), history("2222", FLAT)]
    return backtest_histories(histories, rules=CONFIG.rules, rule_ids=kwargs.get("rule_ids"), backtest=BACKTEST,
                              start=RISES.index[0], end=RISES.index[-1], workers=workers)


def test_returns_are_compared_with_the_same_day_average() -> None:
    result = backtest()

    (signal,) = result.signals.to_dict("records")
    assert (signal["code"], signal["rule_name"], signal["date"]) == ("1111", "三線多排", DAY)
    assert signal["ret_1"] == pytest.approx(10.0)                   # 105 → 115.5
    assert signal["base_1"] == pytest.approx(5.0)                   # (10% + 0%) / 2
    assert signal["excess_1"] == pytest.approx(5.0)
    assert signal["ret_2"] == pytest.approx(10.0)
    assert signal["segment"] == "觀察段" and signal["kept"]


def test_summary_counts_by_rule_and_segment() -> None:
    summary = backtest().summary

    row = select(summary, rule_id="ma_bull_alignment", segment="全期間", horizon=1).iloc[0]
    assert (row["count"], row["win_rate"], row["mean"], row["excess"]) == pytest.approx((1, 100.0, 10.0, 5.0))
    assert row["net"] == pytest.approx(10.0 - BACKTEST.cost.round_trip_pct)
    assert select(summary, rule_id="ma_bull_alignment", segment="驗證段", horizon=1).iloc[0]["count"] == 0
    assert list(summary["rule_id"].unique()) == [rule.id for rule in build_engine(CONFIG.rules, None).entry_rules]


def test_parallel_workers_give_the_same_result() -> None:
    pd.testing.assert_frame_equal(backtest(workers=2).signals, backtest(workers=1).signals)


def test_only_selected_rules_are_backtested() -> None:
    result = backtest(rule_ids=["bull_ma5_breakout"])

    assert result.signals.empty
    assert list(result.rule_names) == ["bull_ma5_breakout"]


def test_unknown_rule_id_is_rejected() -> None:
    with pytest.raises(ConfigError, match="沒有已啟用的進場規則 nope"):
        build_engine(CONFIG.rules, ["nope"])


# --- 完整執行與輸出 ----------------------------------------------------------

def daily_bars(code: str, bars: pd.DataFrame) -> list[DailyBar]:
    return [DailyBar(code, day, r.open, r.high, r.low, r.close, int(r.volume))
            for day, r in zip(bars.index, bars.itertuples())]


@pytest.fixture
def config(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars(daily_bars("1111", RISES) + daily_bars("2222", FLAT))
        for code in ("1111", "2222"):
            store.set_event_coverage(code, EventCoverage(RISES.index[0], RISES.index[-1]))
            store.upsert_listings([Listing(code, Market.TSE, date(2000, 1, 4))])
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222")))
    return replace(CONFIG, watchlist=watchlist, settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))


def test_run_writes_report_and_files(config, tmp_path: Path) -> None:
    out = io.StringIO()
    bt = replace(BACKTEST, output_dir=tmp_path / "out")

    run(config, bt, rules=CONFIG.rules, rules_label="rules.yaml", rule_ids=None, start=None, end=None,
        horizon=None, out=out, workers=1)

    text = out.getvalue()
    assert text.startswith(f"訊號回測 {BACKTEST.signal_start} ~ {RISES.index[-1]}")
    assert "■ 持有 1 日（去重，排除漲跌停鎖死）" in text and "三線多排" in text
    signals = pd.read_csv(tmp_path / "out" / "signals.csv", encoding="utf-8-sig")
    assert list(signals.columns[:4]) == ["日期", "代號", "名稱", "族群"]
    assert signals.loc[0, "報酬1日(%)"] == pytest.approx(10.0)
    summary = pd.read_csv(tmp_path / "out" / "summary.csv", encoding="utf-8-sig")
    assert set(summary["判斷價格"]) == {"未還原", "還原"}
    html = (tmp_path / "out" / "report.html").read_text(encoding="utf-8")
    assert html.count("<h2>") == 4 and "plotly" in html.lower()


def test_run_requires_events_for_every_stock(config, tmp_path: Path) -> None:
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars([DailyBar("1111", date(2020, 6, 1), 1.0, 1.0, 1.0, 1.0, 1)])     # 日 K 比事件期間早

    with pytest.raises(Exception, match="1 檔尚未查詢除權息與公司行動.*請先執行 events：1111"):
        run(config, BACKTEST, rules=CONFIG.rules, rules_label="rules.yaml", rule_ids=None, start=None, end=None,
            horizon=None, workers=1)


def test_stock_without_bars_or_listing_does_not_block_backtest(config, tmp_path: Path,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    # 3333：資料庫沒有日 K（events 也不會查它）；1111：不在證交所清單（例：已下市），events 查過卻沒有上市櫃日
    with closing(sqlite3.connect(tmp_path / "screener.db")) as conn, conn:
        conn.execute("DELETE FROM listings WHERE code = '1111'")
    config = replace(config, watchlist=replace(config.watchlist, stocks=(*config.watchlist.stocks, stock("3333"))))

    with caplog.at_level(logging.WARNING):
        result = run(config, replace(BACKTEST, output_dir=tmp_path / "out"), rules=CONFIG.rules,
                     rules_label="rules.yaml", rule_ids=None, start=None, end=None, horizon=None,
                     out=io.StringIO(), workers=1)

    assert list(result.signals["code"]) == ["1111"]         # 沒有上市櫃日：不排除興櫃期間，照常回測
    assert "1 檔資料庫沒有日 K，不列入回測：3333" in caplog.text
    assert "1 檔不在證交所的上市、上櫃清單中" in caplog.text and "照常回測：1111" in caplog.text


def test_horizon_must_be_configured(config) -> None:
    with pytest.raises(ConfigError, match="--horizon 7"):
        run(config, BACKTEST, rules=CONFIG.rules, rules_label="rules.yaml", rule_ids=None, start=None, end=None,
            horizon=7, workers=1)


# --- CLI ---------------------------------------------------------------------

@pytest.fixture
def cli_config_dir(config_dir: Path, config) -> Path:
    """repo 的設定檔，觀察清單縮小為 1111、2222，資料庫與輸出指向暫存目錄。"""
    watchlist = read_yaml(config_dir / "watchlist.yaml")
    watchlist["stocks"] = [{"code": c, "name": f"股{c}", "market": "tse", "industry": "PCB/銅箔基板"}
                           for c in ("1111", "2222")]
    write_yaml(config_dir / "watchlist.yaml", watchlist)
    settings = read_yaml(config_dir / "settings.yaml")
    settings["db_path"] = str(config.settings.db_path)
    write_yaml(config_dir / "settings.yaml", settings)
    bt = read_yaml(config_dir / "backtest.yaml")
    bt["output_dir"] = str(config_dir.parent / "out")
    write_yaml(config_dir / "backtest.yaml", bt)
    return config_dir


def test_cli_backtest_with_alternative_rules_file(cli_config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = read_yaml(cli_config_dir / "rules.yaml")
    rules["entry_rules"] = [r for r in rules["entry_rules"] if r["id"] == "ma_bull_alignment"]
    write_yaml(cli_config_dir.parent / "experiment.yaml", rules)

    argv = ["--config-dir", str(cli_config_dir), "backtest", "--rules-file",
            str(cli_config_dir.parent / "experiment.yaml"), "--end", str(RISES.index[-1])]
    assert main(argv) == EXIT_OK

    out = capsys.readouterr().out
    assert "experiment.yaml" in out and "三線多排" in out and "空頭跌破五日線黑K" not in out
    assert (cli_config_dir.parent / "out" / "report.html").is_file()


@pytest.mark.parametrize(
    ("args", "code"),
    [(["--rules", "nope"], EXIT_CONFIG_ERROR), (["--horizon", "7"], EXIT_CONFIG_ERROR)],
)
def test_cli_backtest_errors(cli_config_dir: Path, args: list[str], code: int) -> None:
    assert main(["--config-dir", str(cli_config_dir), "backtest", *args]) == code


def test_cli_backtest_config_error(cli_config_dir: Path) -> None:
    bt = read_yaml(cli_config_dir / "backtest.yaml")
    bt["summary_horizon"] = 7
    write_yaml(cli_config_dir / "backtest.yaml", bt)

    assert main(["--config-dir", str(cli_config_dir), "backtest"]) == EXIT_CONFIG_ERROR


def test_cli_backtest_without_events_fails(cli_config_dir: Path, config) -> None:
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_bars([DailyBar("1111", date(2020, 6, 1), 1.0, 1.0, 1.0, 1.0, 1)])

    assert main(["--config-dir", str(cli_config_dir), "backtest"]) == EXIT_FAILED
