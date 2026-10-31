import logging
from datetime import date
from typing import Any

import pytest
import requests

from conftest import FakeResponse, FakeSession, FakeTime
from screener.config import FinMindSettings
from screener.data.base import DataSourceError
from screener.data.finmind import API_URL, FinMindClient
from screener.models import DailyBar

SETTINGS = FinMindSettings(timeout_sec=30, retry=2, retry_backoff_sec=5, request_interval_sec=1)
START, END = date(2026, 10, 29), date(2026, 10, 30)


def row(day: str = "2026-10-30", stock_id: str = "2330", **overrides: Any) -> dict[str, Any]:
    """FinMind TaiwanStockPrice 的一筆資料（欄位與數值取自實際回應）。"""
    data = {
        "date": day, "stock_id": stock_id, "Trading_Volume": 36615931, "Trading_money": 86833096690,
        "open": 2380.0, "max": 2380.0, "min": 2360.0, "close": 2370.0, "spread": -45.0,
        "Trading_turnover": 331330,
    }
    data.update(overrides)
    return data


def ok(*rows: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"msg": "success", "status": 200, "data": list(rows)})


def make_client(session: FakeSession, timer: FakeTime | None = None,
                token: str | None = "test-token") -> FinMindClient:
    timer = timer or FakeTime()
    return FinMindClient(SETTINGS, token, session=session, sleep=timer.sleep, clock=timer.clock)


# --- 請求與轉換 -------------------------------------------------------------

def test_converts_rows_to_daily_bars() -> None:
    client = make_client(FakeSession(ok(row("2026-10-29", close=2415.0), row("2026-10-30"))))

    bars = client.fetch_daily_prices("2330", START, END)

    assert bars[1] == DailyBar(code="2330", date=date(2026, 10, 30), open=2380.0, high=2380.0,
                               low=2360.0, close=2370.0, volume=36616)
    assert [b.date for b in bars] == [START, END]


@pytest.mark.parametrize(("shares", "lots"),
                         [(0, 0), (499, 0), (500, 1), (1499, 1), (1500, 2), (36615931, 36616)])
def test_volume_is_converted_from_shares_to_lots_rounding_half_up(shares: int, lots: int) -> None:
    client = make_client(FakeSession(ok(row(Trading_Volume=shares))))

    assert client.fetch_daily_prices("2330", START, END)[0].volume == lots


def test_sends_dataset_dates_token_and_timeout() -> None:
    session = FakeSession(ok())

    make_client(session).fetch_daily_prices("2330", START, END)

    (call,) = session.calls
    assert call["url"] == API_URL
    assert call["params"] == {"dataset": "TaiwanStockPrice", "data_id": "2330",
                              "start_date": "2026-10-29", "end_date": "2026-10-30"}
    assert call["headers"] == {"Authorization": "Bearer test-token"}
    assert call["timeout"] == SETTINGS.timeout_sec


def test_without_token_sends_no_authorization_header() -> None:
    session = FakeSession(ok())

    make_client(session, token=None).fetch_daily_prices("2330", START, END)

    assert session.calls[0]["headers"] == {}


def test_no_rows_is_not_an_error() -> None:
    assert make_client(FakeSession(ok())).fetch_daily_prices("9999", START, END) == []


def test_requests_are_spaced_by_request_interval() -> None:
    timer = FakeTime()
    client = make_client(FakeSession(ok(), ok()), timer)

    client.fetch_daily_prices("2330", START, END)
    client.fetch_daily_prices("2454", START, END)

    assert timer.sleeps == [SETTINGS.request_interval_sec]


# --- 重試 -------------------------------------------------------------------

@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("reset"), requests.Timeout("timed out"), FakeResponse(503, text="busy"),
     FakeResponse(200, text="<html>gateway</html>")],
    ids=["connection-error", "timeout", "server-error", "not-json"],
)
def test_transient_failure_is_retried_with_backoff(failure: FakeResponse | Exception) -> None:
    timer = FakeTime()
    session = FakeSession(failure, ok(row()))

    bars = make_client(session, timer).fetch_daily_prices("2330", START, END)

    assert len(bars) == 1
    assert len(session.calls) == 2
    assert timer.sleeps == [SETTINGS.retry_backoff_sec]


def test_gives_up_after_configured_retries() -> None:
    timer = FakeTime()
    session = FakeSession(*[FakeResponse(500, text="error")] * 3)

    with pytest.raises(DataSourceError, match="已重試 2 次：HTTP 500"):
        make_client(session, timer).fetch_daily_prices("2330", START, END)

    assert len(session.calls) == SETTINGS.retry + 1
    assert timer.sleeps == [5, 10]          # 第 n 次重試前等待 n × retry_backoff_sec


def test_rate_limit_is_not_retried() -> None:
    body = {"msg": "Requests reach the upper limit. https://finmindtrade.com/", "status": 402}
    session = FakeSession(FakeResponse(402, body))

    with pytest.raises(DataSourceError, match="每小時請求上限"):
        make_client(session).fetch_daily_prices("2330", START, END)

    assert len(session.calls) == 1


def test_rejected_request_is_not_retried() -> None:
    session = FakeSession(FakeResponse(400, {"msg": "Token is illegal.", "status": 400}))

    with pytest.raises(DataSourceError, match="Token is illegal"):
        make_client(session).fetch_daily_prices("2330", START, END)

    assert len(session.calls) == 1


# --- 資料驗證 ---------------------------------------------------------------

def test_row_with_missing_field_is_rejected() -> None:
    bad = row()
    del bad["max"]

    with pytest.raises(DataSourceError, match="格式不符"):
        make_client(FakeSession(ok(bad))).fetch_daily_prices("2330", START, END)


def test_row_for_another_stock_is_rejected() -> None:
    with pytest.raises(DataSourceError, match="stock_id 為 2454"):
        make_client(FakeSession(ok(row(stock_id="2454")))).fetch_daily_prices("2330", START, END)


def test_unexpected_body_is_rejected() -> None:
    session = FakeSession(FakeResponse(200, {"msg": "success", "status": 200}))

    with pytest.raises(DataSourceError, match="回應格式不符"):
        make_client(session).fetch_daily_prices("2330", START, END)


@pytest.mark.parametrize("shares", [0, 21], ids=["suspended", "odd-lot-only"])
def test_row_without_board_lot_trade_is_skipped(shares: int, caplog: pytest.LogCaptureFixture) -> None:
    # 實際回應：停牌或只有零股成交的日子，價格全為 0
    no_trade = row("2026-10-29", Trading_Volume=shares, open=0.0, max=0.0, min=0.0, close=0.0)
    client = make_client(FakeSession(ok(no_trade, row("2026-10-30"))))

    with caplog.at_level(logging.INFO):
        bars = client.fetch_daily_prices("2330", START, END)

    assert [b.date for b in bars] == [END]
    assert "沒有整股成交" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_row_with_some_non_positive_price_is_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client(FakeSession(ok(row("2026-10-29", open=0.0), row("2026-10-30"))))

    with caplog.at_level(logging.WARNING):
        bars = client.fetch_daily_prices("2330", START, END)

    assert [b.date for b in bars] == [END]
    assert "價格不是正數" in caplog.text
