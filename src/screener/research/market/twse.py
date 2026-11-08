"""證交所（上市）的每日行情、報酬指數、除權息與減資（RESEARCH_PLAN.md 4.3）。

已實測（2026-11-07）：
- MI_INDEX（type=ALLBUT0999）一天一次，`tables` 中標題含「每日收盤行情」的是個股行情，標題以
  「報酬指數(臺灣證券交易所)」開頭的表有「發行量加權股價報酬指數」。2015 年的回應格式相同。
- 非交易日（假日、颱風假、沒有交易的週六）`stat` 為「很抱歉，沒有符合條件的資料!」。
- 漲跌(+/-) 是 HTML：`<p style= color:red>+</p>`、`<p style= color:green>-</p>`、`<p> </p>`（平盤或沒有成交）、
  `<p>X</p>`（不比價：除權息、減資恢復買賣等）。標 X 的日子漲跌價差為 0.00，推不出參考價
  （例：2414 精技 2026-11-06 官方參考價 48.04、收盤 47.25）。
- 成交股數含零股、盤後定價、鉅額交易：與 FinMind 的日成交量相同（2026-11-06 重疊的 172 檔）。
"""

import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import requests

from screener.config import RequestSettings
from screener.data.base import DataSourceError
from screener.http import HttpClient
from screener.models import ActionKind, CorporateAction, DividendResult, Market
from screener.research.market.common import (cell_number, count, field_positions, is_common_stock, json_reader, number,
                                             positive_pair, roc_date, rows_of)
from screener.research.market.models import DailyQuotes, MarketQuote, SharesOutstanding, Valuation

BASE_URL = "https://www.twse.com.tw/rwd/zh"
NO_DATA = "沒有符合條件的資料"
QUOTE_TITLE = "每日收盤行情"
INDEX_TITLE = "報酬指數(臺灣證券交易所)"
RETURN_INDEX = "發行量加權股價報酬指數"
RETURN_INDEX_NAMES = (RETURN_INDEX, "加權股價報酬指數")     # 2019-04-29 的每日行情寫成後者
NOT_COMPARED = "X"                  # 不比價

_TAG = re.compile(r"<[^>]+>")
_QUOTE_FIELDS = ("證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
                 "漲跌(+/-)", "漲跌價差")


class TwseClient:
    """回傳原始回應（JSON），由 parse_* 解析；原始回應另存壓縮檔。session、sleep、clock 可替換，方便測試。"""

    def __init__(self, settings: RequestSettings, *, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def daily(self, day: date) -> Any:
        return self._get("afterTrading/MI_INDEX", {"date": f"{day:%Y%m%d}", "type": "ALLBUT0999"},
                         f"證交所每日行情 {day}")

    def dividend_results(self, start: date, end: date) -> Any:
        return self._get("exRight/TWT49U", {"startDate": f"{start:%Y%m%d}", "endDate": f"{end:%Y%m%d}"},
                         f"證交所除權息 {start}～{end}")

    def capital_reductions(self, start: date, end: date) -> Any:
        return self._get("reducation/TWTAUU", {"startDate": f"{start:%Y%m%d}", "endDate": f"{end:%Y%m%d}"},
                         f"證交所減資 {start}～{end}")

    def return_index(self, month: date) -> Any:
        return self._get("TAIEX/MFI94U", {"date": f"{month:%Y%m}01"}, f"證交所報酬指數月表 {month:%Y-%m}")

    def valuation(self, day: date) -> Any:
        return self._get("afterTrading/BWIBBU_d", {"date": f"{day:%Y%m%d}", "selectType": "ALL"},
                         f"證交所本益比 {day}")

    def shares(self, day: date) -> Any:
        return self._get("fund/MI_QFIIS", {"date": f"{day:%Y%m%d}", "selectType": "ALLBUT0999"},
                         f"證交所發行股數 {day}")

    def _get(self, path: str, params: Mapping[str, str], label: str) -> Any:
        return self._http.get(f"{BASE_URL}/{path}", {**params, "response": "json"}, json_reader(label), label=label)


def has_data(body: Any, where: str) -> bool:
    """stat 為 OK 時為 True；查無資料（非交易日、期間內沒有事件）時為 False；其他情況報錯。"""
    if not isinstance(body, dict):
        raise DataSourceError(f"{where} 的回應格式不符：{str(body)[:200]}")
    stat = str(body.get("stat"))
    if stat == "OK":
        return True
    if NO_DATA in stat:
        return False
    raise DataSourceError(f"{where} 查詢失敗：{stat}")


def parse_daily(body: Any, day: date) -> DailyQuotes | None:
    """普通股的行情與報酬指數；非交易日為 None。"""
    where = f"證交所每日行情 {day}"
    if not has_data(body, where):
        return None
    if body.get("date") != f"{day:%Y%m%d}":
        raise DataSourceError(f"{where} 的回應日期為 {body.get('date')}")
    tables = [t for t in body.get("tables") or [] if isinstance(t, dict) and t.get("title")]
    quote_tables = [t for t in tables if QUOTE_TITLE in t["title"]]
    if len(quote_tables) != 1:
        raise DataSourceError(f"{where} 找不到唯一的「{QUOTE_TITLE}」表：{[t['title'] for t in tables]}")
    table = quote_tables[0]
    at = field_positions(table.get("fields") or [], _QUOTE_FIELDS, where)
    quotes = []
    for row in rows_of(table, where):
        code = row[at["證券代號"]].strip()
        if not is_common_stock(code):
            continue
        try:
            quotes.append(_quote(row, at, day))
        except (ValueError, IndexError) as exc:
            raise DataSourceError(f"{where} {code} 的資料列格式不符（{exc}）：{row!r}") from exc
    return DailyQuotes(day=day, market=Market.TSE, quotes=tuple(quotes), return_index=_return_index(tables))


def _quote(row: list[str], at: Mapping[str, int], day: date) -> MarketQuote:
    close = number(row[at["收盤價"]])
    sign = _TAG.sub("", row[at["漲跌(+/-)"]]).strip()
    magnitude = number(row[at["漲跌價差"]])
    flag = None
    if sign == NOT_COMPARED:
        change, flag = None, NOT_COMPARED
    elif close is None:
        change = None
    elif sign in ("+", "-"):
        change = magnitude if sign == "+" else -magnitude
    elif sign == "" and magnitude == 0:
        change = 0.0
    else:
        raise ValueError(f"無法解讀的漲跌：{row[at['漲跌(+/-)']]!r} {row[at['漲跌價差']]!r}")
    return MarketQuote(
        code=row[at["證券代號"]].strip(), date=day, market=Market.TSE, name=row[at["證券名稱"]].strip(),
        open=number(row[at["開盤價"]]), high=number(row[at["最高價"]]), low=number(row[at["最低價"]]), close=close,
        volume=count(row[at["成交股數"]]), value=count(row[at["成交金額"]]), trades=count(row[at["成交筆數"]]),
        change=change, flag=flag,
    )


def _return_index(tables: list[dict[str, Any]]) -> float | None:
    """「發行量加權股價報酬指數」的收盤指數；找不到時為 None（品質報告列出缺漏的日子）。"""
    for table in tables:
        if not table["title"].startswith(INDEX_TITLE):
            continue
        for row in table.get("data") or []:
            if row and row[0].strip() in RETURN_INDEX_NAMES:
                return number(row[1])
    return None


def parse_valuation(body: Any, day: date) -> list[Valuation]:
    """BWIBBU_d：本益比、殖利率、股價淨值比。2015 年的回應欄位較少，以名稱對應。虧損的本益比是 `-`。"""
    where = f"證交所本益比 {day}"
    if not has_data(body, where):
        raise DataSourceError(f"{where} 查無資料（交易日應該要有）")
    at = field_positions(body.get("fields") or [], ("證券代號", "本益比", "殖利率(%)", "股價淨值比"), where)
    return [Valuation(row[at["證券代號"]].strip(), day, Market.TSE, cell_number(row[at["本益比"]]),
                      cell_number(row[at["股價淨值比"]]), cell_number(row[at["殖利率(%)"]]))
            for row in rows_of(body, where) if is_common_stock(row[at["證券代號"]].strip())]


def parse_shares(body: Any, day: date) -> list[SharesOutstanding]:
    """MI_QFIIS 外資及陸資投資持股統計的發行股數。"""
    where = f"證交所發行股數 {day}"
    if not has_data(body, where):
        raise DataSourceError(f"{where} 查無資料（交易日應該要有）")
    at = field_positions(body.get("fields") or [], ("證券代號", "發行股數"), where)
    return [SharesOutstanding(row[at["證券代號"]].strip(), day, Market.TSE, count(str(row[at["發行股數"]] or "")))
            for row in rows_of(body, where) if is_common_stock(row[at["證券代號"]].strip())]


def parse_dividend_results(body: Any, where: str) -> list[DividendResult]:
    """TWT49U 除權除息計算結果表：除權息前收盤價與參考價（官方，D37）。只取普通股。"""
    if not has_data(body, where):
        return []
    at = field_positions(body.get("fields") or [], ("資料日期", "股票代號", "除權息前收盤價", "除權息參考價"), where)
    results = []
    for row in rows_of(body, where):
        code = row[at["股票代號"]].strip()
        if not is_common_stock(code):
            continue
        day = roc_date(row[at["資料日期"]])
        before, after = number(row[at["除權息前收盤價"]]), number(row[at["除權息參考價"]])
        if positive_pair(before, after, f"{where} {code} {day}"):
            results.append(DividendResult(code, day, before, after))
    return results


def parse_capital_reductions(body: Any, where: str) -> list[CorporateAction]:
    """TWTAUU 減資恢復買賣參考價格：因子 = 恢復買賣參考價 ÷ 停止買賣前收盤價格。只取普通股。"""
    if not has_data(body, where):
        return []
    at = field_positions(body.get("fields") or [], ("恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價"),
                         where)
    actions = []
    for row in rows_of(body, where):
        code = row[at["股票代號"]].strip()
        if not is_common_stock(code):
            continue
        day = roc_date(row[at["恢復買賣日期"]])
        before, after = number(row[at["停止買賣前收盤價格"]]), number(row[at["恢復買賣參考價"]])
        if positive_pair(before, after, f"{where} {code} {day}"):
            actions.append(CorporateAction(code, day, ActionKind.CAPITAL_REDUCTION, before, after))
    return actions


def parse_return_index(body: Any, where: str) -> dict[date, float]:
    """MFI94U 月表：日期 → 發行量加權股價報酬指數。"""
    if not has_data(body, where):
        return {}
    at = field_positions(body.get("fields") or [], ("日期", RETURN_INDEX), where)
    values = {}
    for row in rows_of(body, where):
        value = number(row[at[RETURN_INDEX]])
        if value is not None:
            values[roc_date(row[at["日期"]])] = value
    return values
