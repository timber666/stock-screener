"""HTTP 請求的共用流程：請求間隔、逾時與重試（PROJECT_PLAN.md 第 13 節）。FinMind、MIS、Discord 共用。"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

import requests

from screener.config import RequestSettings
from screener.data.base import DataSourceError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """暫時性錯誤（伺服器錯誤、回應不完整、被限流），重試可能會成功。

    retry_after：對方指定的等待秒數（例：Discord 的 HTTP 429），有值時取代重試退避的秒數。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HttpClient:
    """依設定控制請求間隔、逾時與重試。session、sleep、clock 可替換，方便測試。

    error：重試後仍失敗時拋出的例外型別。redact：不可出現在 log 與錯誤訊息中的字串（例：webhook token），
    連線錯誤的訊息會帶有網址，因此要先遮蔽。
    """

    def __init__(
        self,
        settings: RequestSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        error: type[Exception] = DataSourceError,
        redact: Sequence[str] = (),
    ) -> None:
        self._settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._error = error
        self._redact = tuple(secret for secret in redact if secret)
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
        return self._request("GET", url, read, label=label, params=params, headers=dict(headers or {}))

    def post_json(self, url: str, body: Any, read: Callable[[requests.Response], T], *, label: str) -> T:
        return self._request("POST", url, read, label=label, json=body)

    def _request(self, method: str, url: str, read: Callable[[requests.Response], T], *, label: str,
                 **kwargs: Any) -> T:
        """送出請求並以 read 解讀回應。

        連線失敗、逾時、回應傳到一半連線中斷（例：下載 10 MB 的 ISIN 清單時）與 read 拋出的 RetryableError
        會重試；read 拋出的其他錯誤直接往外拋。label 用於 log 與錯誤訊息，例："FinMind TaiwanStockPrice 2330"。
        """
        attempts = self._settings.retry + 1
        for attempt in range(1, attempts + 1):
            self._throttle()
            retry_after: float | None = None
            try:
                response = self._session.request(method, url, timeout=self._settings.timeout_sec, **kwargs)
                return read(response)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                error = f"連線失敗：{self._mask(str(exc))}"
            except RetryableError as exc:
                error, retry_after = str(exc), exc.retry_after

            if attempt < attempts:
                wait = self._settings.retry_backoff_sec * attempt if retry_after is None else retry_after
                logger.warning("%s 請求失敗（%s），%g 秒後重試（%d/%d）",
                               label, error, wait, attempt, self._settings.retry)
                self._sleep(wait)
        raise self._error(f"{label} 請求失敗，已重試 {self._settings.retry} 次：{error}")

    def _mask(self, text: str) -> str:
        for secret in self._redact:
            text = text.replace(secret, "***")
        return text

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self._last_request_at + self._settings.request_interval_sec - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()
