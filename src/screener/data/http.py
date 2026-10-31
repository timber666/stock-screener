"""HTTP 請求的共用流程：請求間隔、逾時與重試（PROJECT_PLAN.md 第 13 節）。"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import TypeVar

import requests

from screener.config import RequestSettings
from screener.data.base import DataSourceError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """暫時性錯誤（伺服器錯誤、回應不完整），重試可能會成功。"""


class HttpClient:
    """依設定控制請求間隔、逾時與重試。session、sleep、clock 可替換，方便測試。"""

    def __init__(
        self,
        settings: RequestSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def get(
        self,
        url: str,
        params: Mapping[str, str],
        read: Callable[[requests.Response], T],
        *,
        label: str,
        headers: Mapping[str, str] | None = None,
    ) -> T:
        """送出 GET 並以 read 解讀回應。

        連線失敗、逾時與 read 拋出的 RetryableError 會重試；read 拋出的其他錯誤直接往外拋。
        label 用於 log 與錯誤訊息，例："FinMind TaiwanStockPrice 2330"。
        """
        attempts = self._settings.retry + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self._session.get(
                    url, params=params, headers=dict(headers or {}), timeout=self._settings.timeout_sec
                )
                return read(response)
            except (requests.ConnectionError, requests.Timeout) as exc:
                error = f"連線失敗：{exc}"
            except RetryableError as exc:
                error = str(exc)

            if attempt < attempts:
                wait = self._settings.retry_backoff_sec * attempt
                logger.warning("%s 請求失敗（%s），%g 秒後重試（%d/%d）",
                               label, error, wait, attempt, self._settings.retry)
                self._sleep(wait)
        raise DataSourceError(f"{label} 請求失敗，已重試 {self._settings.retry} 次：{error}")

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._last_request_at + self._settings.request_interval_sec - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()
