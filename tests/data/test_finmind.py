import logging
from datetime import date, timedelta
from typing import Any

import pytest
import requests

from conftest import FakeResponse, FakeSession, FakeTime
from screener.config import FinMindSettings
from screener.data.base import DataSourceError
from screener.data.finmind import API_URL, FinMindClient
from screener.models import ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult

SETTINGS = FinMindSettings(timeout_sec=30, retry=2, retry_backoff_sec=5, request_interval_sec=1)
LOOKAHEAD_DAYS = 30                 # research.yaml 的 events.dividend_lookahead_days
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
    return FinMindClient(SETTINGS, token, dividend_lookahead_days=LOOKAHEAD_DAYS, session=session,
                         sleep=timer.sleep, clock=timer.clock)


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


# --- 除權息、減資、面額變更（PROJECT_PLAN.md 14.2）----------------------------

def dividend_row(stock_id: str = "2884", **overrides: Any) -> dict[str, Any]:
    """TaiwanStockDividend 的一筆資料（欄位與數值取自 2884 玉山金 2024 年的實際回應，省略無關欄位）。

    date 是權利分派基準日，比除權息交易日晚。
    """
    data = {
        "date": "2024-08-02", "stock_id": stock_id, "year": "112年",
        "CashExDividendTradingDate": "2024-07-25", "StockExDividendTradingDate": "2024-07-25",
        "CashEarningsDistribution": 1.2, "CashStatutorySurplus": 0.0,
        "StockEarningsDistribution": 0.20033197, "StockStatutorySurplus": 0.0,
    }
    data.update(overrides)
    return data


JUL = (date(2024, 7, 1), date(2024, 7, 31))


def test_dividend_query_looks_ahead_because_api_filters_on_record_date() -> None:
    session = FakeSession(ok())

    make_client(session).fetch_dividends("2884", *JUL)

    params = session.calls[0]["params"]
    assert params["dataset"] == "TaiwanStockDividend"
    assert (params["start_date"], params["end_date"]) == (
        "2024-07-01", (JUL[1] + timedelta(days=LOOKAHEAD_DAYS)).isoformat())


def test_dividends_need_lookahead_days() -> None:
    # 只有 events 會查股利，放寬天數在 research.yaml；每日的 update 建立的用戶端沒有這個設定
    client = FinMindClient(SETTINGS, "test-token", session=FakeSession())

    with pytest.raises(ValueError, match="dividend_lookahead_days"):
        client.fetch_dividends("2884", *JUL)


def test_cash_and_stock_dividends_on_the_same_day_form_one_event() -> None:
    events = make_client(FakeSession(ok(dividend_row()))).fetch_dividends("2884", *JUL)

    assert events == [DividendEvent("2884", date(2024, 7, 25), 1.2, 0.20033197)]


def test_earnings_and_surplus_distributions_are_summed() -> None:
    # 2409 友達 2023 年的股利全部配在公積，只取盈餘那一欄會少算 100%
    row = dividend_row("2409", CashExDividendTradingDate="2024-07-10", StockExDividendTradingDate="",
                       CashEarningsDistribution=0.0, CashStatutorySurplus=0.8,
                       StockEarningsDistribution=0.1, StockStatutorySurplus=0.2)

    (event,) = make_client(FakeSession(ok(row))).fetch_dividends("2409", *JUL)

    assert (event.cash_dividend, event.stock_dividend) == (0.8, pytest.approx(0.3))
    assert event.date == date(2024, 7, 10)              # 股票股利的交易日空白時沿用現金股利的


def test_cash_and_stock_on_different_days_are_separate_events() -> None:
    row = dividend_row(CashExDividendTradingDate="2024-07-10", StockExDividendTradingDate="2024-07-24")

    events = make_client(FakeSession(ok(row))).fetch_dividends("2884", *JUL)

    assert events == [DividendEvent("2884", date(2024, 7, 10), 1.2, 0.0),
                      DividendEvent("2884", date(2024, 7, 24), 0.0, 0.20033197)]


def test_same_day_announcements_are_combined_and_out_of_range_days_dropped() -> None:
    rows = [dividend_row(StockEarningsDistribution=0.0), dividend_row(CashEarningsDistribution=0.0),
            dividend_row(CashExDividendTradingDate="2024-08-05", StockExDividendTradingDate="2024-08-05")]

    events = make_client(FakeSession(ok(*rows))).fetch_dividends("2884", *JUL)

    assert events == [DividendEvent("2884", date(2024, 7, 25), 1.2, 0.20033197)]


def test_dividend_without_trading_date_is_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    row = dividend_row(CashExDividendTradingDate="", StockExDividendTradingDate="")

    with caplog.at_level(logging.WARNING):
        assert make_client(FakeSession(ok(row))).fetch_dividends("2884", *JUL) == []

    assert "沒有除權息交易日" in caplog.text


def test_dividend_results_capital_reductions_and_splits() -> None:
    result = {"date": "2025-12-11", "stock_id": "2330", "before_price": 1505.0, "after_price": 1499.99,
              "stock_and_cache_dividend": 5.000011, "stock_or_cache_dividend": "息"}
    reduction = {"date": "2022-09-19", "stock_id": "2603", "ClosingPriceonTheLastTradingDay": 80.8,
                 "PostReductionReferencePrice": 187.0, "ReasonforCapitalReduction": "Cash refund"}
    split = {"date": "2025-08-25", "stock_id": "2327", "type": "面額變更", "before_price": 546.0,
             "after_price": 136.5}
    session = FakeSession(ok(result), ok(reduction), ok(split))
    client = make_client(session)
    year = (date(2021, 1, 1), date(2026, 10, 30))

    assert client.fetch_dividend_results("2330", *year) == [
        DividendResult("2330", date(2025, 12, 11), 1505.0, 1499.99)]
    assert client.fetch_capital_reductions("2603", *year) == [
        CorporateAction("2603", date(2022, 9, 19), ActionKind.CAPITAL_REDUCTION, 80.8, 187.0)]
    assert client.fetch_splits(*year) == [CorporateAction("2327", date(2025, 8, 25), ActionKind.SPLIT, 546.0, 136.5)]
    assert "data_id" not in session.calls[2]["params"]          # 面額變更不分股票，一次查全市場


def test_action_without_positive_prices_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    split = {"date": "2025-08-25", "stock_id": "2327", "type": "", "before_price": 0.0, "after_price": 136.5}

    with caplog.at_level(logging.WARNING):
        assert make_client(FakeSession(ok(split))).fetch_splits(date(2025, 1, 1), date(2025, 12, 31)) == []

    assert "前後價格不是正數" in caplog.text
