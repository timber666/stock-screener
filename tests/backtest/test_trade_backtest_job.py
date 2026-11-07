"""交易回測的整體流程、輸出與 CLI（RESEARCH_PLAN.md 4.2）。"""

import io
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backtest.test_trades import ZIGZAG, zigzag_then
from conftest import REPO_CONFIG_DIR, read_yaml, write_yaml
from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, EXIT_OK, main
from screener.config import ConfigError, load_backtest_config, load_config
from screener.data.base import EventCoverage
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed
from screener.jobs.trade_backtest import run, select_runs
from screener.models import DailyBar, Listing, Market, StockInfo

CONFIG = load_config(REPO_CONFIG_DIR)
# 1111：02-06 收 124 進場，02-10 收 118 跌破 MA5 賣出（見 test_trades）；2222：一路 100。觀察段到 02-06
HISTORY = zigzag_then(124.0, 126.0, 118.0, 130.0)
FLAT = HISTORY.bars.assign(open=100.0, high=100.0, low=100.0, close=100.0)
VALIDATION_START = date(2026, 2, 9)


def stock(code: str) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=Market.TSE, group="PCB", subcategory="銅箔基板")


def daily_bars(code: str, bars: pd.DataFrame) -> list[DailyBar]:
    return [DailyBar(code, day, r.open, r.high, r.low, r.close, int(r.volume))
            for day, r in zip(bars.index, bars.itertuples())]


@pytest.fixture
def config(tmp_path: Path):
    with SqlitePriceStore(tmp_path / "screener.db") as store:
        store.upsert_bars(daily_bars("1111", HISTORY.bars) + daily_bars("2222", FLAT))
        for code in ("1111", "2222"):
            store.set_event_coverage(code, EventCoverage(ZIGZAG.index[0], HISTORY.bars.index[-1]))
            store.upsert_listings([Listing(code, Market.TSE, date(2000, 1, 4))])
    watchlist = replace(CONFIG.watchlist, stocks=(stock("1111"), stock("2222")))
    return replace(CONFIG, watchlist=watchlist, settings=replace(CONFIG.settings, db_path=tmp_path / "screener.db"))


@pytest.fixture
def backtest(tmp_path: Path):
    bt = load_backtest_config(REPO_CONFIG_DIR / "backtest.yaml")
    trade = replace(bt.trade, max_positions=(1, 2), random_runs=5, output_dir=tmp_path / "trade")
    return replace(bt, signal_start=ZIGZAG.index[0], validation_start=VALIDATION_START, trade=trade)


def run_job(config, backtest, **kwargs):
    out = io.StringIO()
    options = {"validate": False, "max_positions": None, "pick": None, **kwargs}
    result = run(config, backtest, rules=CONFIG.rules, rules_label="rules.yaml", out=out, workers=1, **options)
    return result, out.getvalue()


def test_observe_runs_every_combination_and_writes_files(config, backtest, tmp_path: Path) -> None:
    result, text = run_job(config, backtest)

    assert result.segment == "觀察段" and result.end == date(2026, 2, 6)
    assert [(c.code, c.entry_day, c.exit_day) for c in result.candidates] == [("1111", date(2026, 2, 6), date(2026, 2, 10))]
    assert [(r.settings.max_positions, r.settings.pick) for r in result.runs] == [
        (1, "nearest_ma"), (1, "volume_ratio"), (2, "nearest_ma"), (2, "volume_ratio")]
    assert {cap: len(runs) for cap, runs in result.random_runs.items()} == {1: 5, 2: 5}
    assert text.startswith("交易回測｜觀察段 2026-01-05 ~ 2026-02-06｜初始資金 300,000 元")
    assert "距 MA5 最近" in text and "量比最大" in text and "隨機（5 次中位數）" in text

    out = tmp_path / "trade" / "observe"
    trades = pd.read_csv(out / "trades.csv", encoding="utf-8-sig", dtype={"代號": str})
    one = trades[(trades["上限"] == 1) & (trades["挑選"] == "距 MA5 最近")].iloc[0]
    # 上限 1 檔：300,000 / 124 = 2419 股，加手續費超過，所以 2418 股；上限 2 檔：150,000 / 124 = 1209 股
    assert (one["代號"], one["股數"], one["買進金額"], one["賣出價"], one["賣出原因"]) == ("1111", 2418, 299_832.0, 118.0, "跌破均線")
    assert set(trades.loc[trades["上限"] == 2, "股數"]) == {1209}
    equity = pd.read_csv(out / "equity.csv", encoding="utf-8-sig")
    first = equity[(equity["上限"] == 1) & (equity["挑選"] == "距 MA5 最近")]
    assert first["權益"].iloc[-1] - 300_000 == pytest.approx(one["損益"], abs=0.01)
    assert list(first["日期"])[-1] == "2026-02-10"                # 觀察段結束後照規則賣完
    summary = pd.read_csv(out / "summary.csv", encoding="utf-8-sig")
    assert len(summary) == 6 and set(summary["挑選"]) == {"距 MA5 最近", "量比最大", "隨機"}
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "交易回測報表" in html and html.count("<h2>") == 4


def test_validate_runs_only_the_chosen_combination(config, backtest, tmp_path: Path) -> None:
    result, text = run_job(config, backtest, validate=True, max_positions=2, pick="nearest_ma")

    assert result.segment == "驗證段" and result.start == VALIDATION_START and result.candidates == ()
    assert [(r.settings.max_positions, r.settings.pick) for r in result.runs] == [(2, "nearest_ma")]
    assert list(result.random_runs) == [2]
    assert text.startswith("交易回測｜驗證段 2026-02-09")
    assert (tmp_path / "trade" / "validate" / "summary.csv").is_file()
    assert not (tmp_path / "trade" / "observe").exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"validate": True}, "--validate 需要同時指定"),
     ({"validate": True, "max_positions": 2}, "--validate 需要同時指定"),
     ({"max_positions": 2}, "只能和 --validate 一起使用"),
     ({"validate": True, "max_positions": 4, "pick": "nearest_ma"}, "--max-positions 4：應為"),
     ({"validate": True, "max_positions": 2, "pick": "random"}, "--pick random：應為")],
)
def test_select_runs_rejects_unplanned_combinations(backtest, kwargs, message: str) -> None:
    options = {"validate": False, "max_positions": None, "pick": None, **kwargs}

    with pytest.raises(ConfigError, match=message):
        select_runs(backtest.trade, **options)


def test_rules_without_exit_rules_are_rejected(config, backtest) -> None:
    rules = replace(CONFIG.rules, exit_rules=())

    with pytest.raises(ConfigError, match="沒有啟用的出場規則"):
        run(config, backtest, rules=rules, rules_label="rules.yaml", validate=False, max_positions=None, pick=None,
            workers=1)


def test_segment_without_bars_fails(config, backtest) -> None:
    with pytest.raises(JobFailed, match="驗證段（2027-01-01 起）沒有任何交易日的日 K"):
        run_job(config, replace(backtest, validation_start=date(2027, 1, 1)), validate=True, max_positions=1,
                pick="nearest_ma")


# --- CLI ---------------------------------------------------------------------

@pytest.fixture
def cli_config_dir(config_dir: Path, config, tmp_path: Path) -> Path:
    """repo 的設定檔，觀察清單縮小為 1111、2222，資料庫與輸出指向暫存目錄。"""
    watchlist = read_yaml(config_dir / "watchlist.yaml")
    watchlist["stocks"] = [{"code": c, "name": f"股{c}", "market": "tse", "industry": "PCB/銅箔基板"}
                           for c in ("1111", "2222")]
    write_yaml(config_dir / "watchlist.yaml", watchlist)
    settings = read_yaml(config_dir / "settings.yaml")
    settings["db_path"] = str(config.settings.db_path)
    write_yaml(config_dir / "settings.yaml", settings)
    bt = read_yaml(config_dir / "backtest.yaml")
    bt.update(signal_start=ZIGZAG.index[0], validation_start=VALIDATION_START)
    bt["trade"].update(random_runs=2, output_dir=str(tmp_path / "cli-trade"))
    write_yaml(config_dir / "backtest.yaml", bt)
    return config_dir


def test_cli_trade_backtest(cli_config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config-dir", str(cli_config_dir), "trade-backtest"]) == EXIT_OK

    assert "交易回測｜觀察段" in capsys.readouterr().out
    assert (tmp_path / "cli-trade" / "observe" / "report.html").is_file()


@pytest.mark.parametrize(
    ("args", "code"),
    [(["--validate"], EXIT_CONFIG_ERROR), (["--max-positions", "3"], EXIT_CONFIG_ERROR),
     (["--validate", "--max-positions", "5", "--pick", "volume_ratio"], EXIT_OK)],
)
def test_cli_trade_backtest_arguments(cli_config_dir: Path, args: list[str], code: int) -> None:
    assert main(["--config-dir", str(cli_config_dir), "trade-backtest", *args]) == code


def test_cli_trade_backtest_without_database_fails(cli_config_dir: Path) -> None:
    settings = read_yaml(cli_config_dir / "settings.yaml")
    settings["db_path"] = str(cli_config_dir.parent / "missing.db")
    write_yaml(cli_config_dir / "settings.yaml", settings)

    assert main(["--config-dir", str(cli_config_dir), "trade-backtest"]) == EXIT_FAILED
