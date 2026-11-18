"""ShioajiClient 的查詢間隔、重試與個人資料的遮蔽（以假的 API 物件測試，不匯入 shioaji）。"""

from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_CONFIG_DIR
from screener.realtime.config import ShioajiKeys, load_realtime_config
from screener.realtime.shioaji_source import ShioajiClient, ShioajiRequestError, mask_private


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds

    def clock(self) -> float:
        return self.now


class FlakyApi:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def snapshots(self, contracts: list[Any], timeout: int) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("timeout")
        return [{"code": "2330"}]


def _client(api: Any, clock: FakeClock) -> ShioajiClient:
    settings = load_realtime_config(REPO_CONFIG_DIR / "realtime.yaml").shioaji
    client = ShioajiClient(settings, ShioajiKeys("k", "s"), sleep=clock.sleep, clock=clock.clock)
    client._api = api                           # 跳過 connect：測試不登入
    return client


def test_queries_keep_the_minimum_interval() -> None:
    clock = FakeClock()
    client = _client(FlakyApi(failures=0), clock)

    client.snapshots([])
    clock.now += 0.1
    client.snapshots([])

    assert clock.sleeps == [0.2]                # query_interval_sec 0.3 − 已經過的 0.1


def test_failed_query_is_retried_with_backoff() -> None:
    clock = FakeClock()
    api = FlakyApi(failures=2)

    assert _client(api, clock).snapshots([]) == [{"code": "2330"}]
    assert api.calls == 3
    assert [s for s in clock.sleeps if s >= 1] == [5, 10]   # retry_backoff_sec × n


def test_query_fails_after_retries() -> None:
    with pytest.raises(ShioajiRequestError, match="snapshots 失敗（3 次）"):
        _client(FlakyApi(failures=5), FakeClock()).snapshots([])


def test_query_before_login() -> None:
    settings = load_realtime_config(REPO_CONFIG_DIR / "realtime.yaml").shioaji
    with pytest.raises(ShioajiRequestError, match="尚未登入"):
        ShioajiClient(settings, ShioajiKeys("k", "s")).usage()


def test_connect_needs_shioaji(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 測試環境一律不能匯入 shioaji（conftest），不會真的登入
    monkeypatch.chdir(tmp_path)                         # log_path 是相對路徑，目錄建在暫存目錄
    monkeypatch.setenv("SJ_LOG_PATH", str(tmp_path / "shioaji.log"))
    settings = load_realtime_config(REPO_CONFIG_DIR / "realtime.yaml").shioaji
    client = ShioajiClient(settings, ShioajiKeys("k", "s"))
    with pytest.raises(ImportError):
        client.connect()


def test_mask_private() -> None:
    text = "#P2P/v:x/PYAPI/A123456789/1118/1/2/10.20.30.40/> host '192.168.1.2:80'"

    assert mask_private(text) == "#P2P/v:x/PYAPI/***/1118/1/2/x.x.x.x/> host 'x.x.x.x:80'"
    assert mask_private("Subscribe or Unsubscribe ok") == "Subscribe or Unsubscribe ok"
