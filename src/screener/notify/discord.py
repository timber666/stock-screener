"""Discord webhook 推播，實作 Notifier（PROJECT_PLAN.md 第 11 節）。

- POST JSON `{"content": ...}` 到 webhook 網址，成功回 HTTP 204。單則訊息上限 2000 字。
- 被限流時回 HTTP 429，`retry_after` 為要等待的秒數。
- webhook 網址本身就是憑證（含 token），不可寫入 log 或錯誤訊息。
"""

import time
from collections.abc import Callable
from typing import Any

import requests

from screener.config import DiscordSettings
from screener.http import HttpClient, RetryableError
from screener.notify.base import NotifyError

MESSAGE_LIMIT = 2000                # Discord 單則訊息的字數上限

_HTTP_RATE_LIMITED = 429
_HTTP_BAD_WEBHOOK = (401, 403, 404)


class DiscordWebhook:
    """session、sleep、clock 可替換，方便測試。env_name：網址在 .env 的名稱，用於錯誤提示。"""

    def __init__(
        self,
        settings: DiscordSettings,
        url: str,
        *,
        env_name: str = "DISCORD_WEBHOOK_URL",
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._env_name = env_name
        _, _, credentials = url.partition("/webhooks/")     # {id}/{token}
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock, error=NotifyError,
                                redact=(url, credentials))

    def send(self, content: str) -> None:
        body = {"content": content, "allowed_mentions": {"parse": []}}    # 不觸發 @everyone 等提及
        self._http.post_json(self._url, body, self._check_response, label="Discord webhook")

    def _check_response(self, response: requests.Response) -> None:
        """成功時不回傳內容。暫時性錯誤拋出 RetryableError，其餘錯誤拋出 NotifyError。"""
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == _HTTP_RATE_LIMITED:
            raise RetryableError("被 Discord 限流（HTTP 429）", retry_after=_retry_after(response))
        if status >= 500:
            raise RetryableError(f"HTTP {status}")
        hint = f"，請確認 .env 的 {self._env_name}" if status in _HTTP_BAD_WEBHOOK else ""
        raise NotifyError(f"Discord 拒絕訊息（HTTP {status}：{_error_message(response)}）{hint}")


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after(response: requests.Response) -> float | None:
    body = _json(response)
    value = body.get("retry_after") if isinstance(body, dict) else response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_message(response: requests.Response) -> str:
    body = _json(response)
    if isinstance(body, dict) and "message" in body:
        return str(body["message"])
    return response.text[:200]
