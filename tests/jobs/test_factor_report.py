"""factor-report：以小型的 market.db 跑完整流程（RESEARCH_PLAN.md 4.4）。"""

import io
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, EXIT_OK, main
from screener.config import ConfigError, load_backtest_config, load_config, load_research_config
from screener.jobs import factor_report
from screener.models import Market
from screener.research.market.models import DailyQuotes, FetchRecord, MarketQuote, MonthlyRevenue, SharesOutstanding, Valuation
from screener.research.market.store import SqliteMarketStore

CODES = [f"{1100 + i}" for i in range(12)]


def edit_yaml(path: Path, edit) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def trading_days(n: int) -> list[date]:
    days, day = [], date(2025, 1, 2)
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def populate(db_path: Path, days: list[date]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteMarketStore(db_path) as store:
        for t, day in enumerate(days):
            quotes = []
            for i, code in enumerate(CODES):
                close = round(50 + i + (i - 5) * 0.02 * t + (t % 7) * 0.1, 2)
                quotes.append(MarketQuote(code, day, Market.TSE, f"S{i}", close, close + 1, close - 1, close,
                                          (i + 1) * 100_000, int((i + 1) * 1e8), 100, 0.0))
            store.record_day(day, [("tse_daily", DailyQuotes(day, Market.TSE, tuple(quotes))), ("otc_daily", None)], days[-1])
        months = sorted({date(d.year, d.month, 1) for d in days})
        for m in months:
            record = FetchRecord("tse_revenue", m, m, "ok", 12, days[-1])
            store.record_revenues(record, [MonthlyRevenue(code, m, Market.TSE, code, "半導體業" if i % 2 else "航運業",
                                                          100 + i, 100, 90, 1000, 900) for i, code in enumerate(CODES)])
        for sample in {min(d for d in days if (d.year, d.month) == (m.year, m.month)) for m in months}:
            record = FetchRecord("tse_valuation", sample, sample, "ok", 12, days[-1])
            store.record_valuations(record, [Valuation(code, sample, Market.TSE, 10.0 + i, 1.0 + i, 2.0)
                                             for i, code in enumerate(CODES)])
            store.record_shares(FetchRecord("tse_shares", sample, sample, "ok", 12, days[-1]),
                                [SharesOutstanding(code, sample, Market.TSE, 1_000_000) for code in CODES])


@pytest.fixture
def study_dir(config_dir: Path) -> Path:
    def edit(data: dict) -> None:
        data["market"]["universe"] = {"size": 12, "lookback_days": 20}
        data["factors"]["min_stocks"] = 5
        data["factors"]["min_industry_stocks"] = 2

    edit_yaml(config_dir / "research.yaml", edit)
    edit_yaml(config_dir / "backtest.yaml", lambda d: d.update(validation_start=date(2025, 6, 1)))
    research = load_research_config(config_dir / "research.yaml")
    populate(research.market.db_path, trading_days(140))
    return config_dir


def test_factor_report_end_to_end(study_dir: Path) -> None:
    research = load_research_config(study_dir / "research.yaml")
    backtest = load_backtest_config(study_dir / "backtest.yaml")
    out = io.StringIO()

    result = factor_report.run(load_config(study_dir), research, backtest,
                               factor_ids=["ma5_gap", "rev_yoy", "ep", "size", "trend", "profitable"], out=out)

    assert {"ma5_gap", "rev_yoy", "ep", "size"} == set(result.summary["factor"])
    # IC 只算一次：收盤 t 的「全部」有 IC，「排除收漲跌停」只有分位數；隔日開盤只有 IC
    selected = result.summary[(result.summary["factor"] == "ma5_gap") & (result.summary["horizon"] == 5)
                              & (result.summary["segment"] == "觀察段") & (result.summary["neutral"] == "未中性")]
    assert set(selected["entry"]) == {"close", "next_open"}
    included = selected[(selected["entry"] == "close") & (selected["limit"] == "全部")].iloc[0]
    excluded = selected[(selected["entry"] == "close") & (selected["limit"] == "排除收漲跌停")].iloc[0]
    assert not pd.isna(included["ic_mean"]) and pd.isna(excluded["ic_mean"])
    assert not pd.isna(excluded["top_bottom"])
    assert {"多頭", "盤整", "空頭", "資料不足"} >= set(result.groups.loc[result.groups["factor"] == "trend", "group"])
    profitable = result.groups[(result.groups["factor"] == "profitable") & (result.groups["limit"] == "全部")]
    assert (profitable.loc[profitable["group"] == "獲利", "days"] > 0).all()
    assert (profitable.loc[profitable["group"] == "虧損", "days"] == 0).all()

    directory = research.factors.output_dir
    for name in ("summary.csv", "ic.csv", "decay.csv", "groups.csv", "report.html"):
        assert (directory / name).is_file()
    text = out.getvalue()
    assert "單因子研究" in text and "ma5_gap" in text and "不構成投資建議" in text
    assert "單因子研究報表" in (directory / "report.html").read_text(encoding="utf-8")


def test_unknown_factor_id_is_rejected(study_dir: Path) -> None:
    research = load_research_config(study_dir / "research.yaml")
    backtest = load_backtest_config(study_dir / "backtest.yaml")

    with pytest.raises(ConfigError, match="沒有這些因子：nope"):
        factor_report.run(load_config(study_dir), research, backtest, factor_ids=["nope"])


def test_cli_runs_selected_factors(study_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--config-dir", str(study_dir), "factor-report", "--factors", "ma5_gap"])

    assert code == EXIT_OK
    assert "ma5_gap" in capsys.readouterr().out


def test_cli_reports_a_missing_database(config_dir: Path) -> None:
    code = main(["--config-dir", str(config_dir), "factor-report", "--factors", "ma5_gap"])

    assert code == EXIT_FAILED


def test_cli_reports_a_bad_factor_definition(study_dir: Path) -> None:
    edit_yaml(study_dir / "research.yaml", lambda d: d["factors"]["definitions"].append(
        {"id": "bad", "kind": "momentum", "params": {"days": 5}}))

    code = main(["--config-dir", str(study_dir), "factor-report", "--factors", "ma5_gap"])

    assert code == EXIT_CONFIG_ERROR
