"""rt-record：訂閱的分配與錄製流程（以假的連線與時鐘測試，REALTIME_PLAN.md 7.1 第 4 點，L16）。"""

import io
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from fake_shioaji import FakeClient, FakeContract, FakeTick
from screener.config import load_config
from screener.jobs import JobFailed, rt_record
from screener.jobs.rt_record import Subscription, plan_subscriptions
from screener.realtime.config import ShioajiKeys, load_realtime_config
from screener.realtime.recorder import read_records, recording_path


def test_plan_puts_compare_codes_first_with_bidask() -> None:
    plan = plan_subscriptions(["A", "B", "C", "D", "E"], ["C"], max_subscriptions=3, connections=2)

    assert plan.connections == [[Subscription("C", "tick"), Subscription("C", "bidask"), Subscription("A", "tick")],
                                [Subscription("B", "tick"), Subscription("D", "tick"), Subscription("E", "tick")]]
    assert plan.left_out == []


def test_plan_lists_codes_that_do_not_fit() -> None:
    plan = plan_subscriptions(["A", "B", "C", "D"], ["A", "Z"], max_subscriptions=3, connections=1)

    assert plan.connections == [[Subscription("A", "tick"), Subscription("A", "bidask"), Subscription("B", "tick")]]
    assert plan.left_out == ["C", "D"]


def test_plan_rejects_compare_codes_over_the_limit() -> None:
    with pytest.raises(JobFailed, match="對照股"):
        plan_subscriptions(["A", "B"], ["A", "B"], max_subscriptions=3, connections=1)


def test_watchlist_with_default_settings_fits_one_connection_partially(config_dir: Path) -> None:
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    codes = [s.code for s in config.watchlist.stocks]

    one = plan_subscriptions(codes, realtime.compare_codes, max_subscriptions=200, connections=1)
    two = plan_subscriptions(codes, realtime.compare_codes, max_subscriptions=200, connections=2)

    assert len(one.connections[0]) == 200 and len(one.left_out) == len(codes) - 197   # 3 檔對照股佔 6 個
    assert two.left_out == [] and sum(len(c) for c in two.connections) == len(codes) + 3


class FakeTime:
    """sleep 讓時鐘前進；在指定的時刻觸發假的推送。"""

    def __init__(self, start: datetime, pushes: dict[datetime, list]) -> None:
        self.now = start
        self.pushes = sorted(pushes.items())

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        while self.pushes and self.pushes[0][0] <= self.now:
            for push in self.pushes.pop(0)[1]:
                push()


def test_record_until_end_time(config_dir: Path) -> None:
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    contracts = {s.code: FakeContract(s.code, s.name) for s in config.watchlist.stocks}
    client = FakeClient(contracts)
    start = datetime(2026, 11, 19, 8, 50)
    clock = FakeTime(start, {
        start + timedelta(seconds=1): [
            lambda: client.callbacks["tick"](FakeTick({"code": "4977", "close": "187.0", "simtrade": True})),
            lambda: client.callbacks["event"](0, 0, "host x.x.x.x", "Session up")],
        start + timedelta(seconds=2): [lambda: client.callbacks["bidask"](FakeTick({"code": "4977"})),
                                       lambda: client.callbacks["session_down"]()],
    })
    out = io.StringIO()

    path = rt_record.run(config, realtime, ShioajiKeys("k", "s"), codes=["4977", "1303", "2330"],
                         until=time(8, 51), client_factory=lambda: client, now=clock, sleep=clock.sleep, out=out)

    assert path == recording_path(realtime.record.tick_dir, start.date())
    records = list(read_records(path))
    types = [r["type"] for r in records]
    assert types.count("subscribe") == 5                   # 4977、1303 的成交與五檔，2330 的成交
    assert {"tick", "bidask", "event", "session_down", "usage"} <= set(types)
    assert types[-1] == "meta" and records[-1]["data"]["reason"] == "結束時間"
    tick = next(r for r in records if r["type"] == "tick")
    assert tick["data"] == {"code": "4977", "close": "187.0", "simtrade": True}
    assert client.subscribed == [] and client.logged_out
    assert "錄製結束（結束時間）" in out.getvalue()


def test_record_refuses_after_end_time(config_dir: Path) -> None:
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")

    with pytest.raises(JobFailed, match="--until"):
        rt_record.run(config, realtime, ShioajiKeys("k", "s"), client_factory=lambda: None,
                      now=lambda: datetime(2026, 11, 19, 14, 0))


def test_record_rejects_codes_outside_watchlist(config_dir: Path) -> None:
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")

    with pytest.raises(JobFailed, match="不在觀察清單"):
        rt_record.run(config, realtime, ShioajiKeys("k", "s"), codes=["0000"], client_factory=lambda: None,
                      now=lambda: datetime(2026, 11, 19, 8, 0))
