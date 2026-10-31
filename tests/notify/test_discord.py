"""Discord webhook（PROJECT_PLAN.md 第 11 節）。以假的 session 測試，不連網。"""

import logging

import pytest
import requests

from conftest import FakeResponse, FakeSession, FakeTime
from screener.config import DiscordSettings
from screener.notify.base import NotifyError
from screener.notify.discord import DiscordWebhook

SETTINGS = DiscordSettings(timeout_sec=10, retry=2, retry_backoff_sec=5, request_interval_sec=1)
URL = "https://discord.com/api/webhooks/123456/secret-token"
NO_CONTENT = FakeResponse(204, text="")


def make_webhook(session: FakeSession, timer: FakeTime | None = None) -> DiscordWebhook:
    timer = timer or FakeTime()
    return DiscordWebhook(SETTINGS, URL, session=session, sleep=timer.sleep, clock=timer.clock)


def test_posts_content_without_mentions() -> None:
    session = FakeSession(NO_CONTENT)

    make_webhook(session).send("【盤中選股】@everyone")

    (call,) = session.calls
    assert (call["method"], call["url"], call["timeout"]) == ("POST", URL, SETTINGS.timeout_sec)
    assert call["json"] == {"content": "【盤中選股】@everyone", "allowed_mentions": {"parse": []}}


def test_messages_are_spaced_by_request_interval() -> None:
    timer = FakeTime()
    webhook = make_webhook(FakeSession(NO_CONTENT, NO_CONTENT), timer)

    webhook.send("第 1 則")
    webhook.send("第 2 則")

    assert timer.sleeps == [SETTINGS.request_interval_sec]


def test_rate_limit_waits_for_retry_after() -> None:
    timer = FakeTime()
    limited = FakeResponse(429, {"message": "You are being rate limited.", "retry_after": 2.5, "global": False})
    session = FakeSession(limited, NO_CONTENT)

    make_webhook(session, timer).send("內容")

    assert len(session.calls) == 2
    assert timer.sleeps == [2.5]                    # 等 Discord 指定的秒數，而不是 retry_backoff_sec


def test_rate_limit_without_body_uses_retry_after_header() -> None:
    timer = FakeTime()
    session = FakeSession(FakeResponse(429, text="", headers={"Retry-After": "2"}), NO_CONTENT)

    make_webhook(session, timer).send("內容")

    assert timer.sleeps == [2.0]


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("reset"), requests.Timeout("timed out"), FakeResponse(502, text="bad gateway")],
    ids=["connection-error", "timeout", "server-error"],
)
def test_transient_failure_is_retried_with_backoff(failure: FakeResponse | Exception) -> None:
    timer = FakeTime()
    session = FakeSession(failure, NO_CONTENT)

    make_webhook(session, timer).send("內容")

    assert timer.sleeps == [SETTINGS.retry_backoff_sec]


def test_gives_up_after_configured_retries() -> None:
    session = FakeSession(*[FakeResponse(503, text="unavailable")] * 3)

    with pytest.raises(NotifyError, match="Discord webhook 請求失敗，已重試 2 次：HTTP 503"):
        make_webhook(session).send("內容")


def test_unknown_webhook_is_not_retried() -> None:
    session = FakeSession(FakeResponse(404, {"message": "Unknown Webhook", "code": 10015}))

    with pytest.raises(NotifyError, match="HTTP 404：Unknown Webhook）.*DISCORD_WEBHOOK_URL"):
        make_webhook(session).send("內容")

    assert len(session.calls) == 1


def test_webhook_token_never_appears_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    # requests 的連線錯誤訊息會帶有網址
    error = requests.ConnectionError(f"Max retries exceeded with url: /api/webhooks/123456/secret-token ({URL})")
    session = FakeSession(*[error] * 3)

    with caplog.at_level(logging.WARNING), pytest.raises(NotifyError) as exc_info:
        make_webhook(session).send("內容")

    assert "secret-token" not in caplog.text and "secret-token" not in str(exc_info.value)
    assert "/api/webhooks/***" in str(exc_info.value)
