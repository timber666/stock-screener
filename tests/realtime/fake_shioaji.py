"""取代 ShioajiClient 的假連線：不連網、不匯入 shioaji。介面同 screener.realtime.shioaji_source.ShioajiClient。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from screener.realtime.shioaji_source import AccountSummary, ShioajiRequestError, Usage

_EPOCH = datetime(1970, 1, 1)


def to_ts(moment: datetime) -> int:
    """台灣時間 → Shioaji 的 ts（把本地時間當成 UTC 編碼的奈秒）。"""
    return int((moment - _EPOCH) / timedelta(microseconds=1)) * 1000


@dataclass
class FakeContract:
    code: str
    name: str
    exchange: str = "TSE"
    day_trade: str = "Yes"

    def dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "exchange": self.exchange, "day_trade": self.day_trade,
                "reference": 100.0, "limit_up": 110.0, "limit_down": 90.0, "trading_suspended": False,
                "category": "24"}


@dataclass
class FakeTick:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def kbar_columns(day: date, closes: list[float], *, start: time = time(9, 1)) -> dict[str, list[Any]]:
    """以結束時間標示的 1 分 K（同 Shioaji），每根量 1 張。"""
    labels = [datetime.combine(day, start) + timedelta(minutes=i) for i in range(len(closes))]
    return {"ts": [to_ts(label) for label in labels], "Open": closes, "High": closes, "Low": closes,
            "Close": closes, "Volume": [1] * len(closes), "Amount": [c * 1000 for c in closes]}


@dataclass
class FakeClient:
    contracts: dict[str, FakeContract]
    login_error: str | None = None
    accounts: list[AccountSummary] = field(default_factory=lambda: [AccountSummary("Stock", False)])
    kbars_data: Callable[[str, date, date], dict[str, list[Any]]] | None = None
    ticks_data: Callable[[str, date], dict[str, list[Any]]] | None = None
    usage_bytes: int = 0
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    subscribed: list[tuple[str, str]] = field(default_factory=list)
    callbacks: dict[str, Callable[..., None]] = field(default_factory=dict)
    logged_out: bool = False

    def connect(self) -> list[AccountSummary]:
        self.calls.append(("connect",))
        if self.login_error:
            raise ShioajiRequestError(self.login_error)
        return self.accounts

    def logout(self) -> None:
        self.logged_out = True

    def usage(self) -> Usage:
        self.usage_bytes += 700
        return Usage(connections=1, bytes=self.usage_bytes, limit_bytes=500 * 1024 * 1024,
                     remaining_bytes=500 * 1024 * 1024 - self.usage_bytes)

    def stock(self, code: str) -> FakeContract | None:
        return self.contracts.get(code)

    def contract_count(self) -> int:
        return len(self.contracts)

    def snapshots(self, contracts: list[FakeContract]) -> list[dict[str, Any]]:
        self.calls.append(("snapshots", len(contracts)))
        self.usage_bytes += 100 * len(contracts)
        return [{"code": c.code, "close": 100.0, "total_volume": 10, "ts": to_ts(datetime(2026, 11, 18, 14, 30))}
                for c in contracts]

    def kbars(self, contract: FakeContract, start: date, end: date) -> dict[str, list[Any]]:
        self.calls.append(("kbars", contract.code, start, end))
        self.usage_bytes += 10_000
        if self.kbars_data is None:
            return kbar_columns(end, [100.0, 101.0])
        return self.kbars_data(contract.code, start, end)

    def ticks(self, contract: FakeContract, day: date) -> dict[str, list[Any]]:
        self.calls.append(("ticks", contract.code, day))
        if self.ticks_data is not None:
            return self.ticks_data(contract.code, day)
        moments = [datetime.combine(day, time(9, 0, 1)), datetime.combine(day, time(13, 30))]
        return {"ts": [to_ts(m) for m in moments], "close": [100.0, 101.0], "volume": [5, 7],
                "bid_price": [99.5, 100.5], "bid_volume": [1, 1], "ask_price": [100.0, 101.0], "ask_volume": [1, 1],
                "tick_type": [1, 2]}

    def subscribe(self, contract: FakeContract, kind: str) -> None:
        self.subscribed.append((contract.code, kind))

    def unsubscribe(self, contract: FakeContract, kind: str) -> None:
        self.subscribed.remove((contract.code, kind))

    def on_tick(self, callback: Callable[[Any], None]) -> None:
        self.callbacks["tick"] = callback

    def on_bidask(self, callback: Callable[[Any], None]) -> None:
        self.callbacks["bidask"] = callback

    def on_event(self, callback: Callable[[int, int, str, str], None]) -> None:
        self.callbacks["event"] = callback

    def on_session_down(self, callback: Callable[[], None]) -> None:
        self.callbacks["session_down"] = callback
