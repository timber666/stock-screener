"""MIS 即時報價（PROJECT_PLAN.md 8.2）。

tests/fixtures/mis_20261030.json 是實際回應（2026-10-31 週六查詢，回傳 10/30 收盤後的資料），
依序為 2330 台積電、2308 台達電（跌停鎖死，沒有買單）、6488 環球晶（上櫃），以及不存在的代號 9999。
"""

import copy
import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
import requests

from conftest import FIXTURES_DIR, FakeResponse, FakeSession, FakeTime
from screener.config import MisSettings
from screener.data.base import DataSourceError
from screener.data.mis import API_URL, MisClient
from screener.models import Market, Quote, StockInfo

SETTINGS = MisSettings(timeout_sec=10, retry=2, retry_backoff_sec=3, request_interval_sec=3, batch_size=50,
                       missing_price_retry=1, missing_price_wait_sec=5)
REAL_BODY: dict[str, Any] = json.loads((FIXTURES_DIR / "mis_20261030.json").read_text(encoding="utf-8"))


def stock(code: str, market: Market = Market.TSE) -> StockInfo:
    return StockInfo(code=code, name=f"股{code}", market=market, group="半導體", subcategory="製造")


REAL_STOCKS = [stock("2330"), stock("2308"), stock("6488", Market.OTC), stock("9999")]


def record(code: str, **overrides: str) -> dict[str, Any]:
    """取自實際回應的一筆資料，可覆寫欄位。"""
    (found,) = [r for r in REAL_BODY["msgArray"] if r["c"] == code]
    return {**copy.deepcopy(found), **overrides}


def body(*records: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {**REAL_BODY, "msgArray": list(records)})


def make_client(session: FakeSession, timer: FakeTime | None = None, **settings: Any) -> MisClient:
    timer = timer or FakeTime()
    return MisClient(replace(SETTINGS, **settings), session=session, sleep=timer.sleep, clock=timer.clock)


# --- 請求與轉換 -------------------------------------------------------------

def test_parses_real_response() -> None:
    quotes = make_client(FakeSession(FakeResponse(200, REAL_BODY))).fetch_quotes(REAL_STOCKS)

    assert quotes["2330"] == Quote(code="2330", date=date(2026, 10, 30), open=2380.0, high=2380.0, low=2360.0,
                                   last=2370.0, prev_close=2415.0, volume=29562, best_bid=2365.0,
                                   best_ask=2370.0)
    assert quotes["6488"].last == 756.0 and quotes["6488"].volume == 8913
    assert quotes["2308"].best_bid is None          # 跌停鎖死時 b 為 "-"
    assert quotes["2308"].best_ask == 1335.0        # 賣價即跌停價
    assert "9999" not in quotes                     # 不存在的代號回傳 c 為空字串的資料


def test_sends_market_prefixed_codes_and_timeout() -> None:
    session = FakeSession(FakeResponse(200, REAL_BODY))

    make_client(session).fetch_quotes(REAL_STOCKS)

    (call,) = session.calls
    assert call["url"] == API_URL
    assert call["params"] == {"ex_ch": "tse_2330.tw|tse_2308.tw|otc_6488.tw|tse_9999.tw", "json": "1", "delay": "0"}
    assert call["timeout"] == SETTINGS.timeout_sec


def test_stocks_are_queried_in_batches_spaced_by_request_interval() -> None:
    timer = FakeTime()
    session = FakeSession(body(record("2330"), record("2308")), body(record("6488")))

    quotes = make_client(session, timer, batch_size=2).fetch_quotes(REAL_STOCKS[:3])

    assert [call["params"]["ex_ch"] for call in session.calls] == ["tse_2330.tw|tse_2308.tw", "otc_6488.tw"]
    assert timer.sleeps == [SETTINGS.request_interval_sec]
    assert set(quotes) == {"2330", "2308", "6488"}


def test_dash_means_no_value() -> None:
    no_trade = record("2330", o="-", h="-", l="-", z="-", v="-", b="-", a="-")

    quote = make_client(FakeSession(body(no_trade))).fetch_quotes([stock("2330")])["2330"]

    fields = (quote.open, quote.high, quote.low, quote.last, quote.volume, quote.best_bid, quote.best_ask)
    assert fields == (None,) * 7
    assert quote.prev_close == 2415.0


def test_records_for_codes_not_requested_are_ignored() -> None:
    quotes = make_client(FakeSession(body(record("2330"), record("6488")))).fetch_quotes([stock("2330")])

    assert set(quotes) == {"2330"}


# --- 錯誤與重試 -------------------------------------------------------------

@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("reset"), requests.Timeout("timed out"), FakeResponse(503, text="busy"),
     FakeResponse(200, text="<html>busy</html>")],
    ids=["connection-error", "timeout", "server-error", "not-json"],
)
def test_transient_failure_is_retried_with_backoff(failure: FakeResponse | Exception) -> None:
    timer = FakeTime()
    session = FakeSession(failure, FakeResponse(200, REAL_BODY))

    quotes = make_client(session, timer).fetch_quotes(REAL_STOCKS)

    assert len(quotes) == 3
    assert timer.sleeps == [SETTINGS.retry_backoff_sec]


def test_gives_up_after_configured_retries() -> None:
    session = FakeSession(*[requests.Timeout("timed out")] * 3)

    with pytest.raises(DataSourceError, match="MIS 2330～9999（4 檔） 請求失敗，已重試 2 次"):
        make_client(session).fetch_quotes(REAL_STOCKS)


def test_uri_too_long_suggests_smaller_batch_size() -> None:
    # 實測：一次查 290 檔時回 HTTP 414 與一段 HTML
    session = FakeSession(FakeResponse(414, text="<!DOCTYPE html>"))

    with pytest.raises(DataSourceError, match="調小 settings.yaml 的 mis.batch_size"):
        make_client(session).fetch_quotes(REAL_STOCKS)

    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "response",
    [FakeResponse(200, {**REAL_BODY, "rtcode": "9999", "rtmessage": "error"}),
     FakeResponse(200, {"rtcode": "0000"})],
    ids=["error-rtcode", "no-msgArray"],
)
def test_unexpected_body_is_rejected(response: FakeResponse) -> None:
    session = FakeSession(response)

    with pytest.raises(DataSourceError, match="MIS 回應"):
        make_client(session).fetch_quotes(REAL_STOCKS)

    assert len(session.calls) == 1


def test_malformed_record_is_rejected() -> None:
    with pytest.raises(DataSourceError, match="2330 的報價格式不符"):
        make_client(FakeSession(body(record("2330", z="abc")))).fetch_quotes([stock("2330")])
