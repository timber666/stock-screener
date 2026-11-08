"""證交所、櫃買中心回應的解析與請求（RESEARCH_PLAN.md 4.3）。測試資料是 2026-11-07 實測回應裁剪後的版本。"""

import copy
import json
from datetime import date
from typing import Any

import pytest

from conftest import FIXTURES_DIR, FakeResponse, FakeSession, FakeTime
from screener.config import RequestSettings
from screener.data.base import DataSourceError
from screener.models import ActionKind, CorporateAction, DividendResult, Market
from screener.research.market import tpex, twse
from screener.research.market.common import is_common_stock, number, roc_date, to_roc

MARKET_FIXTURES = FIXTURES_DIR / "market"
SETTINGS = RequestSettings(timeout_sec=10, retry=1, retry_backoff_sec=1, request_interval_sec=5)


def fixture(name: str) -> Any:
    return json.loads((MARKET_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --- 共用 -----------------------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    ("2330", True), ("9910", True), ("1101", True),
    ("0050", False), ("00400A", False), ("9103", False), ("910322", False), ("2881A", False), ("01001T", False),
    ("23300", False), ("２３３０", False),
])
def test_common_stock_rule(code: str, expected: bool) -> None:
    assert is_common_stock(code) is expected


@pytest.mark.parametrize("text, expected", [("1,234.50", 1234.5), ("-0.53 ", -0.53), ("+ 14.00", 14.0),
                                            ("--", None), ("----", None), ("", None), ("0.00", 0.0)])
def test_number(text: str, expected: float | None) -> None:
    assert number(text) == expected


@pytest.mark.parametrize("text, expected", [("115/10/01", date(2026, 10, 1)), ("115年11月06日", date(2026, 11, 6)),
                                            ("1040130", date(2015, 1, 30)), ("104/1/5", date(2015, 1, 5))])
def test_roc_date(text: str, expected: date) -> None:
    assert roc_date(text) == expected
    assert to_roc(date(2026, 10, 1)) == "115/10/01"


# --- 證交所 ---------------------------------------------------------------------

def test_twse_daily_parses_common_stocks_and_return_index() -> None:
    daily = twse.parse_daily(fixture("twse_daily_20261106"), date(2026, 11, 6))

    assert daily is not None and daily.market is Market.TSE
    assert daily.return_index == 91806.06
    quotes = {q.code: q for q in daily.quotes}
    assert sorted(quotes) == ["1101", "1109", "1538", "2317", "2330", "2414", "5906"]     # 不含 ETF、存託憑證
    tsmc = quotes["2330"]
    # 與 screener.db（FinMind）相同：開高低收 2020 / 2195 / 2020 / 2135、110,715 張；參考價即前一日收盤 2240
    assert (tsmc.open, tsmc.high, tsmc.low, tsmc.close) == (2020.0, 2195.0, 2020.0, 2135.0)
    assert (tsmc.change, tsmc.flag, tsmc.reference) == (-105.0, None, 2240.0)
    assert (tsmc.volume, tsmc.value, tsmc.trades) == (110_714_587, 231_893_102_005, 675_265)
    assert quotes["1109"].change == 0.0         # 平盤：<p> </p>、價差 0.00
    assert quotes["1109"].reference == quotes["1109"].close


def test_twse_not_compared_day_has_no_reference() -> None:
    # 標 X（不比價）的日子漲跌價差為 0.00，推不出參考價：2414 精技 2026-11-06 官方參考價 48.04、收盤 47.25
    quote = {q.code: q for q in twse.parse_daily(fixture("twse_daily_20261106"), date(2026, 11, 6)).quotes}["2414"]

    assert (quote.close, quote.change, quote.flag, quote.reference) == (47.25, None, "X", None)


def test_twse_untraded_stock_has_no_prices() -> None:
    quote = {q.code: q for q in twse.parse_daily(fixture("twse_daily_20261106"), date(2026, 11, 6)).quotes}["1538"]

    assert (quote.open, quote.high, quote.low, quote.close, quote.change) == (None, None, None, None, None)
    assert not quote.traded
    assert quote.volume == 386                  # 只有零股成交


def test_twse_holiday_is_none() -> None:
    assert twse.parse_daily(fixture("twse_daily_20261009"), date(2026, 10, 9)) is None


def test_twse_daily_rejects_wrong_date_and_errors() -> None:
    body = fixture("twse_daily_20261106")
    with pytest.raises(DataSourceError, match="回應日期"):
        twse.parse_daily(body, date(2026, 11, 5))
    with pytest.raises(DataSourceError, match="查詢失敗"):
        twse.parse_daily({"stat": "查詢日期大於今日"}, date(2026, 11, 6))


def test_twse_daily_rejects_missing_field() -> None:
    body = copy.deepcopy(fixture("twse_daily_20261106"))
    table = next(t for t in body["tables"] if "每日收盤行情" in (t.get("title") or ""))
    table["fields"][table["fields"].index("收盤價")] = "收盤"

    with pytest.raises(DataSourceError, match="收盤價"):
        twse.parse_daily(body, date(2026, 11, 6))


def test_twse_daily_rejects_unknown_sign() -> None:
    body = copy.deepcopy(fixture("twse_daily_20261106"))
    table = next(t for t in body["tables"] if "每日收盤行情" in (t.get("title") or ""))
    row = next(r for r in table["data"] if r[0] == "2330")
    row[9] = "<p>?</p>"

    with pytest.raises(DataSourceError, match="2330"):
        twse.parse_daily(body, date(2026, 11, 6))


def test_twse_return_index_alternative_name() -> None:
    # 2019-04-29 的每日行情把「發行量加權股價報酬指數」寫成「加權股價報酬指數」
    body = copy.deepcopy(fixture("twse_daily_20261106"))
    table = next(t for t in body["tables"] if (t.get("title") or "").startswith("報酬指數"))
    for row in table["data"]:
        row[0] = row[0].replace("發行量加權", "加權")

    assert twse.parse_daily(body, date(2026, 11, 6)).return_index == 91806.06


def test_twse_missing_return_index_is_none() -> None:
    body = copy.deepcopy(fixture("twse_daily_20261106"))
    body["tables"] = [t for t in body["tables"] if not (t.get("title") or "").startswith("報酬指數")]

    assert twse.parse_daily(body, date(2026, 11, 6)).return_index is None


def test_twse_events() -> None:
    results = twse.parse_dividend_results(fixture("twse_exright"), "TWT49U")
    assert results == [DividendResult("9927", date(2026, 10, 1), 70.10, 65.10),
                       DividendResult("2414", date(2026, 11, 6), 48.30, 48.04)]     # 不含 ETF

    actions = twse.parse_capital_reductions(fixture("twse_capital_reduction"), "TWTAUU")
    assert actions == [CorporateAction("1414", date(2026, 1, 12), ActionKind.CAPITAL_REDUCTION, 17.45, 18.27),
                       CorporateAction("2380", date(2026, 6, 29), ActionKind.CAPITAL_REDUCTION, 6.60, 23.86)]

    index = twse.parse_return_index(fixture("twse_return_index"), "MFI94U")
    assert len(index) == 19
    assert index[date(2026, 10, 1)] == 111811.08


def test_twse_events_without_data_are_empty() -> None:
    body = {"stat": "很抱歉，沒有符合條件的資料!"}
    assert twse.parse_dividend_results(body, "TWT49U") == []
    assert twse.parse_capital_reductions(body, "TWTAUU") == []
    assert twse.parse_return_index(body, "MFI94U") == {}


def test_twse_requests() -> None:
    time = FakeTime()
    session = FakeSession(*[FakeResponse(body={"stat": "OK"}) for _ in range(4)])
    client = twse.TwseClient(SETTINGS, session=session, sleep=time.sleep, clock=time.clock)

    client.daily(date(2026, 11, 6))
    client.dividend_results(date(2026, 10, 1), date(2026, 10, 31))
    client.capital_reductions(date(2026, 1, 1), date(2026, 12, 31))
    client.return_index(date(2026, 10, 1))

    assert [(c["url"], c["params"]) for c in session.calls] == [
        ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
         {"date": "20261106", "type": "ALLBUT0999", "response": "json"}),
        ("https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
         {"startDate": "20261001", "endDate": "20261031", "response": "json"}),
        ("https://www.twse.com.tw/rwd/zh/reducation/TWTAUU",
         {"startDate": "20260101", "endDate": "20261231", "response": "json"}),
        ("https://www.twse.com.tw/rwd/zh/TAIEX/MFI94U", {"date": "20261001", "response": "json"}),
    ]
    assert time.sleeps == [5, 5, 5]             # 請求間隔


def test_non_json_response_is_not_retried() -> None:
    # 被暫時限制連線時通常回傳 HTML，馬上重試只會延長限制
    time = FakeTime()
    session = FakeSession(FakeResponse(text="<html>blocked</html>"))
    client = twse.TwseClient(SETTINGS, session=session, sleep=time.sleep, clock=time.clock)

    with pytest.raises(DataSourceError, match="不是 JSON"):
        client.daily(date(2026, 11, 6))
    assert len(session.calls) == 1


def test_server_error_is_retried() -> None:
    time = FakeTime()
    session = FakeSession(FakeResponse(status_code=503, text="busy"), FakeResponse(body={"stat": "OK"}))
    client = twse.TwseClient(SETTINGS, session=session, sleep=time.sleep, clock=time.clock)

    assert client.daily(date(2026, 11, 6)) == {"stat": "OK"}
    assert len(session.calls) == 2


# --- 櫃買中心 -------------------------------------------------------------------

def test_tpex_daily_parses_common_stocks() -> None:
    daily = tpex.parse_daily(fixture("tpex_daily_20261019"), date(2026, 10, 19))

    assert daily is not None and daily.market is Market.OTC and daily.return_index is None
    quotes = {q.code: q for q in daily.quotes}
    assert sorted(quotes) == ["1240", "1294", "1570", "1593"]       # 不含 ETF
    up = quotes["1294"]
    assert (up.close, up.change, up.reference, up.flag) == (49.5, 0.15, 49.35, None)
    assert (up.next_limit_up, up.next_limit_down) == (54.4, 44.55)
    assert quotes["1240"].change == -0.10
    assert quotes["1570"].change == 0.0
    untraded = quotes["1593"]
    assert (untraded.close, untraded.change, untraded.volume) == (None, None, 0)


def test_tpex_event_text_is_a_flag() -> None:
    # 除權息日的漲跌欄位是文字，推不出參考價（2026-10-19 的 32 筆與除權息表一一對應）
    body = copy.deepcopy(fixture("tpex_daily_20261019"))
    row = next(r for r in body["tables"][0]["data"] if r[0] == "1294")
    row[3] = "除權息"

    quote = {q.code: q for q in tpex.parse_daily(body, date(2026, 10, 19)).quotes}["1294"]

    assert (quote.close, quote.change, quote.flag, quote.reference) == (49.5, None, "除權息", None)


def test_tpex_old_format_without_bid_ask_volume() -> None:
    # 2015 年的回應少了最後買量、賣量兩欄，欄位以名稱對應
    daily = tpex.parse_daily(fixture("tpex_daily_20150105"), date(2015, 1, 5))

    quotes = {q.code: q for q in daily.quotes}
    assert sorted(quotes) == ["1256", "3093"]
    assert (quotes["1256"].close, quotes["1256"].change, quotes["1256"].next_limit_up) == (216.0, 14.0, 231.0)


def test_tpex_holiday_is_none() -> None:
    assert tpex.parse_daily(fixture("tpex_daily_20261009"), date(2026, 10, 9)) is None


def test_tpex_daily_rejects_other_date() -> None:
    with pytest.raises(DataSourceError, match="回應日期"):
        tpex.parse_daily(fixture("tpex_daily_20261019"), date(2026, 10, 20))
    with pytest.raises(DataSourceError, match="查詢失敗"):
        tpex.parse_daily({"stat": "error"}, date(2026, 10, 19))


def test_tpex_events() -> None:
    results = tpex.parse_dividend_results(fixture("tpex_exright"), "exDailyQ")
    assert results == [DividendResult("1784", date(2026, 10, 1), 61.20, 60.32),
                       DividendResult("6523", date(2026, 10, 1), 91.00, 89.00),
                       DividendResult("6171", date(2026, 10, 2), 27.35, 25.35)]

    actions = tpex.parse_capital_reductions(fixture("tpex_capital_reduction"), "revivt")
    assert actions == [CorporateAction("4131", date(2015, 1, 30), ActionKind.CAPITAL_REDUCTION, 9.95, 12.56),
                       CorporateAction("8047", date(2026, 10, 27), ActionKind.CAPITAL_REDUCTION, 44.40, 53.45)]


def test_tpex_requests() -> None:
    time = FakeTime()
    session = FakeSession(*[FakeResponse(body={"stat": "ok"}) for _ in range(3)])
    client = tpex.TpexClient(SETTINGS, session=session, sleep=time.sleep, clock=time.clock)

    client.daily(date(2026, 10, 19))
    client.dividend_results(date(2026, 10, 1), date(2026, 10, 31))
    client.capital_reductions(date(2026, 1, 1), date(2026, 12, 31))

    assert [(c["url"], c["params"]) for c in session.calls] == [
        ("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
         {"date": "2026/10/19", "type": "EW", "response": "json"}),
        ("https://www.tpex.org.tw/web/stock/exright/dailyquo/exDailyQ_result.php",
         {"l": "zh-tw", "d": "115/10/01", "ed": "115/10/31", "o": "json"}),
        ("https://www.tpex.org.tw/www/zh-tw/bulletin/revivt",
         {"startDate": "2026/01/01", "endDate": "2026/12/31", "response": "json"}),
    ]
