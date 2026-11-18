"""rt-probe：以假的連線跑完 T1–T7，檢查報告與 T1 失敗時停止（REALTIME_PLAN.md 7.1 第 3 點）。"""

import io
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from fake_shioaji import FakeClient, FakeContract, kbar_columns
from screener.config import load_config
from screener.jobs import JobFailed, rt_probe
from screener.realtime.config import ShioajiKeys, load_daytrade_config, load_realtime_config

DAY = date(2026, 11, 18)


@pytest.fixture
def setup(config_dir: Path):
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    daytrade = load_daytrade_config(config_dir / "daytrade.yaml")
    contracts = {s.code: FakeContract(s.code, s.name, s.market.value.upper()) for s in config.watchlist.stocks}
    return config, realtime, daytrade, contracts


def _run(setup, client: FakeClient, tests=None):
    config, realtime, daytrade, _ = setup
    out = io.StringIO()
    path = rt_probe.run(config, realtime, daytrade, ShioajiKeys("k", "s"), tests=tests, day=DAY,
                        client_factory=lambda: client, out=out, now=datetime(2026, 11, 18, 18, 0),
                        sleep=lambda _: None)
    return path, out.getvalue()


def test_full_probe_writes_report(setup) -> None:
    _, realtime, _, contracts = setup
    client = FakeClient(contracts)

    path, text = _run(setup, client)

    report = json.loads((path / "report.json").read_text(encoding="utf-8"))
    assert report["T1"]["passed"] is True
    assert report["T3"]["found"] == len(contracts)
    assert [b["returned"] for b in report["T4"]["batches"]] == [50, 100, 200, 290]
    assert set(report["T5"]["one_day"]) == set(realtime.compare_codes)
    assert report["T6"][realtime.compare_codes[0]]["count"] == 2
    assert report["T7"]["subscribed"] == realtime.probe.subscribe_limit + 1
    assert client.subscribed == []                         # 全部取消
    assert client.logged_out
    assert all(item["bytes"] > 0 for item in report["T2"]["queries"])
    assert "## T7" in text and (path / "report.md").is_file()
    assert (path / f"T5_kbars_{realtime.compare_codes[0]}_{DAY}.csv").is_file()


def test_report_has_no_account_identity(setup) -> None:
    _, _, _, contracts = setup
    path, _ = _run(setup, FakeClient(contracts), tests=["T1"])

    t1 = json.loads((path / "report.json").read_text(encoding="utf-8"))["T1"]
    assert t1["accounts"] == [{"account_type": "Stock", "signed": False}]


def test_login_failure_stops_after_t1(setup) -> None:
    _, _, _, contracts = setup
    client = FakeClient(contracts, login_error="登入失敗：AuthError")

    with pytest.raises(JobFailed, match="T1 未通過"):
        _run(setup, client)

    assert client.calls == [("connect",)]                  # 後面的項目都沒有執行


def test_no_kbars_fails_t1(setup) -> None:
    _, _, _, contracts = setup
    client = FakeClient(contracts, kbars_data=lambda code, start, end: kbar_columns(end, []))

    with pytest.raises(JobFailed, match="歷史分 K 沒有資料"):
        _run(setup, client)


def test_kbars_compared_with_daily_k(setup, config_dir: Path) -> None:
    # 分 K 合起來和 screener.db 的日 K 比對（唯讀）
    from screener.data.db import SqlitePriceStore
    from screener.models import DailyBar

    config, realtime, _, contracts = setup
    code = realtime.compare_codes[0]
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_bars([DailyBar(code=code, date=DAY, open=100.0, high=101.0, low=100.0, close=101.0,
                                          volume=4)])
    path, _ = _run(setup, FakeClient(contracts), tests=["T5"])

    summary = json.loads((path / "report.json").read_text(encoding="utf-8"))["T5"]["one_day"][code]
    assert summary["daily_k"] == "開高低收相同；量 2／4 張（50.0%）"


def test_unknown_test_id(setup) -> None:
    _, _, _, contracts = setup
    with pytest.raises(JobFailed, match="未知的實測項目"):
        _run(setup, FakeClient(contracts), tests=["T9"])


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 11, 18, 18, 0), date(2026, 11, 18)),    # 平日收盤後：今天
    (datetime(2026, 11, 18, 10, 0), date(2026, 11, 17)),    # 盤中：前一個平日
    (datetime(2026, 11, 16, 8, 0), date(2026, 11, 13)),     # 週一開盤前：上週五
    (datetime(2026, 11, 21, 15, 0), date(2026, 11, 20)),    # 週六：週五
])
def test_default_probe_day(now: datetime, expected: date) -> None:
    assert rt_probe.default_probe_day(now) == expected
