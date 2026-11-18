"""Shioaji 的登入、查詢與訂閱（REALTIME_PLAN.md 第 3 節）。

只讀取行情：不呼叫任何委託、帳務或憑證的功能（REALTIME_PLAN.md 第 0、6 節）。
shioaji 在 connect() 時才匯入，即時系統的其他模組與測試不需要安裝 shioaji。

查詢（snapshots、kbars、ticks、usage）之間至少間隔 query_interval_sec（官方限制每 10 秒 50 次，超過時暫停一分鐘，
一天內多次超過可能暫停 IP 與帳號），失敗時重試 retry 次。訂閱不佔查詢次數，不經過這個間隔。
"""

import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from screener.realtime.config import ShioajiKeys, ShioajiSettings

logger = logging.getLogger(__name__)

TICK = "tick"
BIDASK = "bidask"
QUOTE_KINDS = (TICK, BIDASK)


class ShioajiRequestError(Exception):
    """Shioaji 的登入或查詢失敗（重試後仍失敗）。"""


@dataclass(frozen=True)
class AccountSummary:
    """登入回傳的帳戶，只保留類型與簽署狀態：身分證字號、帳號與姓名不寫進 log 與報告。"""

    account_type: str
    signed: bool


@dataclass(frozen=True)
class Usage:
    connections: int
    bytes: int
    limit_bytes: int
    remaining_bytes: int


class ShioajiClient:
    """一個 Shioaji 連線。sleep、clock 可以注入，測試時不必真的等待。"""

    def __init__(self, settings: ShioajiSettings, keys: ShioajiKeys, *,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._keys = keys
        self._sleep = sleep
        self._clock = clock
        self._last_query: float | None = None
        self._sj: Any = None
        self._api: Any = None

    # --- 連線 -----------------------------------------------------------------

    def connect(self) -> list[AccountSummary]:
        """登入（simulation 依設定）並等待合約下載完成。回傳帳戶的類型與簽署狀態。

        登入時 Shioaji 會自己下載合約（快取在使用者目錄的 .shioaji），不能再呼叫 fetch_contracts：
        兩者同時進行時 fetch_contracts 會失敗（exclusive access lost）。
        """
        log_path = self._settings.log_path.resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("SJ_LOG_PATH", str(log_path))     # 匯入前設定，否則 shioaji.log 會寫在目前的目錄
        import shioaji

        self._sj = shioaji
        self._api = shioaji.Shioaji(simulation=self._settings.simulation)
        # 登入前就換掉預設的事件處理：預設會把事件印到終端，內容含身分證字號與對外 IP
        self._api.set_event_callback(_log_event)
        try:
            accounts = self._api.login(self._keys.api_key, self._keys.secret_key,
                                       subscribe_trade=self._settings.subscribe_trade,
                                       receive_window=self._settings.receive_window_ms)
        except Exception as exc:
            raise ShioajiRequestError(f"登入失敗：{type(exc).__name__}: {exc}") from exc
        self._wait_contracts()
        return [AccountSummary(account_type=str(_enum_name(account.account_type)), signed=bool(account.signed))
                for account in accounts]

    def _wait_contracts(self) -> None:
        deadline = self._clock() + self._settings.contracts_timeout_ms / 1000
        while self.contract_count() == 0:
            if self._clock() >= deadline:
                raise ShioajiRequestError(f"登入後 {self._settings.contracts_timeout_ms / 1000:.0f} 秒內沒有拿到股票合約")
            self._sleep(_CONTRACTS_POLL_SEC)

    def logout(self) -> None:
        if self._api is None:
            return
        try:
            self._api.logout()
        except Exception as exc:                # 登出失敗不影響已取得的結果
            logger.warning("Shioaji 登出失敗：%s: %s", type(exc).__name__, exc)

    # --- 查詢 -----------------------------------------------------------------

    def usage(self) -> Usage:
        result = self._query("usage", lambda: self._api.usage(timeout=self._settings.timeout_ms))
        return Usage(connections=int(result.connections), bytes=int(result.bytes),
                     limit_bytes=int(result.limit_bytes), remaining_bytes=int(result.remaining_bytes))

    def stock(self, code: str) -> Any | None:
        """股票的合約；查不到時為 None。需先 fetch_contracts。"""
        return self._api.Contracts.Stocks.get(code)

    def contract_count(self) -> int:
        return sum(1 for group in self._api.Contracts.Stocks for _ in group)

    def snapshots(self, contracts: Sequence[Any]) -> list[dict[str, Any]]:
        result = self._query("snapshots", lambda: self._api.snapshots(list(contracts),
                                                                      timeout=self._settings.timeout_ms))
        return [_as_dict(item) for item in result]

    def kbars(self, contract: Any, start: date, end: date) -> dict[str, list[Any]]:
        """1 分 K（每次不超過 30 天）。"""
        result = self._query("kbars", lambda: self._api.kbars(contract, start=start.isoformat(), end=end.isoformat(),
                                                              timeout=self._settings.timeout_ms))
        return _as_dict(result)

    def ticks(self, contract: Any, day: date) -> dict[str, list[Any]]:
        """一個交易日的全部逐筆成交。"""
        result = self._query("ticks", lambda: self._api.ticks(contract, date=day.isoformat(),
                                                              timeout=self._settings.timeout_ms))
        return _as_dict(result)

    # --- 訂閱 -----------------------------------------------------------------

    def subscribe(self, contract: Any, kind: str) -> None:
        self._api.subscribe(contract, quote_type=_quote_type(kind), version=self._sj.QuoteVersion.v1)

    def unsubscribe(self, contract: Any, kind: str) -> None:
        self._api.unsubscribe(contract, quote_type=_quote_type(kind), version=self._sj.QuoteVersion.v1)

    def on_tick(self, callback: Callable[[Any], None]) -> None:
        self._api.set_on_tick_stk_v1_callback(callback)

    def on_bidask(self, callback: Callable[[Any], None]) -> None:
        self._api.set_on_bidask_stk_v1_callback(callback)

    def on_event(self, callback: Callable[[int, int, str, str], None]) -> None:
        """連線事件（斷線、重連、訂閱的回應等）：resp_code、event_code、info、event。info 已遮蔽個人資料。"""
        self._api.set_event_callback(lambda code, event_code, info, event: callback(
            code, event_code, mask_private(info), event))

    def on_session_down(self, callback: Callable[[], None]) -> None:
        self._api.set_session_down_callback(callback)

    # --- 內部 -----------------------------------------------------------------

    def _query(self, what: str, call: Callable[[], Any]) -> Any:
        if self._api is None:
            raise ShioajiRequestError("尚未登入")
        attempts = self._settings.retry + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                return call()
            except Exception as exc:
                if attempt == attempts:
                    raise ShioajiRequestError(f"{what} 失敗（{attempts} 次）：{type(exc).__name__}: {exc}") from exc
                wait = attempt * self._settings.retry_backoff_sec
                logger.warning("%s 失敗（第 %d 次），%.0f 秒後重試：%s: %s", what, attempt, wait, type(exc).__name__, exc)
                self._sleep(wait)
        raise AssertionError("unreachable")

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_query is not None:
            wait = self._settings.query_interval_sec - (now - self._last_query)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_query = now


_CONTRACTS_POLL_SEC = 0.5
_NATIONAL_ID = re.compile(r"\b[A-Z][12]\d{8}\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def mask_private(text: str) -> str:
    """遮蔽事件訊息中的身分證字號與 IP（Shioaji 的連線資訊含使用者的身分證字號與對外 IP）。"""
    return _IPV4.sub("x.x.x.x", _NATIONAL_ID.sub("***", str(text)))


def _log_event(code: int, event_code: int, info: str, event: str) -> None:
    logger.info("Shioaji 事件 %s/%s：%s（%s）", code, event_code, event, mask_private(info))


def _quote_type(kind: str) -> str:
    if kind not in QUOTE_KINDS:
        raise ValueError(f"未知的訂閱類型：{kind}")
    return "tick" if kind == TICK else "bidask"


def _as_dict(value: Any) -> dict[str, Any]:
    """Shioaji 的資料物件（MappingMixin）轉成 dict。"""
    if isinstance(value, dict):
        return value
    return dict(value.dict())


def _enum_name(value: Any) -> Any:
    return getattr(value, "name", None) or getattr(value, "value", None) or value
