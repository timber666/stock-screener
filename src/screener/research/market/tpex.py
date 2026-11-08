"""櫃買中心（上櫃）的每日行情、除權息與減資（RESEARCH_PLAN.md 4.3）。

已實測（2026-11-07）：
- 每日行情 `www/zh-tw/afterTrading/otc`（type=EW）的 `stat` 為 `ok`、`date` 為查詢日；非交易日同樣是 `ok`，
  但行情表沒有資料列。不要用 `daily_close_quotes/stk_quote_result.php`：它忽略日期，一律回傳最新一天。
- 欄位名稱有多餘的空白與 `<br>`，2015 年的回應少了最後買量、賣量兩欄，因此以名稱對應。
- 漲跌為帶正負號的數字字串（例 `+ 14.00`、`-0.53 `、`0.00`）；沒有成交為 `---`；除權息日為「除息」等文字，
  推不出參考價（2026-10-19 的 32 筆與除權息表一一對應）。行情另有「次日漲停價、跌停價」。
- 成交股數比 FinMind 少 0.2%～1.2%，口徑不同，照櫃買中心的數字存（R10）。
- 減資恢復交易參考價 `www/zh-tw/bulletin/revivt`：日期區間可以跨年，2015-01～2026-11 共 248 筆；
  恢復買賣日期為 `1040130` 形式。
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
from screener.research.market.common import (count, field_positions, is_common_stock, json_reader, number,
                                             positive_pair, roc_date, rows_of, to_roc)
from screener.research.market.models import DailyQuotes, MarketQuote

BASE_URL = "https://www.tpex.org.tw"
QUOTE_TITLE = "上櫃股票每日收盤行情"

_CHANGE = re.compile(r"[+-]?\s*[0-9][0-9,]*(\.[0-9]+)?")
_QUOTE_FIELDS = ("代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "成交股數", "成交金額(元)", "成交筆數",
                 "次日漲停價", "次日跌停價")


class TpexClient:
    """回傳原始回應（JSON），由 parse_* 解析；原始回應另存壓縮檔。session、sleep、clock 可替換，方便測試。"""

    def __init__(self, settings: RequestSettings, *, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def daily(self, day: date) -> Any:
        return self._get("www/zh-tw/afterTrading/otc", {"date": f"{day:%Y/%m/%d}", "type": "EW", "response": "json"},
                         f"櫃買中心每日行情 {day}")

    def dividend_results(self, start: date, end: date) -> Any:
        return self._get("web/stock/exright/dailyquo/exDailyQ_result.php",
                         {"l": "zh-tw", "d": to_roc(start), "ed": to_roc(end), "o": "json"},
                         f"櫃買中心除權息 {start}～{end}")

    def capital_reductions(self, start: date, end: date) -> Any:
        return self._get("www/zh-tw/bulletin/revivt",
                         {"startDate": f"{start:%Y/%m/%d}", "endDate": f"{end:%Y/%m/%d}", "response": "json"},
                         f"櫃買中心減資 {start}～{end}")

    def _get(self, path: str, params: Mapping[str, str], label: str) -> Any:
        return self._http.get(f"{BASE_URL}/{path}", params, json_reader(label), label=label)


def first_table(body: Any, where: str) -> dict[str, Any]:
    """stat 為 ok 的回應的第一張表；其他情況報錯。"""
    if not isinstance(body, dict) or str(body.get("stat")).lower() != "ok":
        stat = body.get("stat") if isinstance(body, dict) else None
        raise DataSourceError(f"{where} 查詢失敗：{stat if stat is not None else str(body)[:200]}")
    tables = body.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        raise DataSourceError(f"{where} 的回應沒有資料表：{str(body)[:200]}")
    return tables[0]


def parse_daily(body: Any, day: date) -> DailyQuotes | None:
    """普通股的行情；非交易日（行情表沒有資料列）為 None。"""
    where = f"櫃買中心每日行情 {day}"
    table = first_table(body, where)
    if body.get("date") != f"{day:%Y%m%d}":
        raise DataSourceError(f"{where} 的回應日期為 {body.get('date')}")
    rows = rows_of(table, where)
    if not rows:
        return None
    if QUOTE_TITLE not in str(table.get("title")):
        raise DataSourceError(f"{where} 的資料表不是「{QUOTE_TITLE}」：{table.get('title')}")
    at = field_positions(table.get("fields") or [], _QUOTE_FIELDS, where)
    quotes = []
    for row in rows:
        code = row[at["代號"]].strip()
        if not is_common_stock(code):
            continue
        try:
            quotes.append(_quote(row, at, day))
        except (ValueError, IndexError) as exc:
            raise DataSourceError(f"{where} {code} 的資料列格式不符（{exc}）：{row!r}") from exc
    return DailyQuotes(day=day, market=Market.OTC, quotes=tuple(quotes))


def _quote(row: list[str], at: Mapping[str, int], day: date) -> MarketQuote:
    close = number(row[at["收盤"]])
    text = row[at["漲跌"]].strip()
    change, flag = None, None
    if close is not None:
        if _CHANGE.fullmatch(text):
            change = number(text)
        elif text and not set(text) <= {"-"}:
            flag = text                     # 除息、除權等，推不出參考價
    return MarketQuote(
        code=row[at["代號"]].strip(), date=day, market=Market.OTC, name=row[at["名稱"]].strip(),
        open=number(row[at["開盤"]]), high=number(row[at["最高"]]), low=number(row[at["最低"]]), close=close,
        volume=count(row[at["成交股數"]]), value=count(row[at["成交金額(元)"]]), trades=count(row[at["成交筆數"]]),
        change=change, flag=flag,
        next_limit_up=number(row[at["次日漲停價"]]), next_limit_down=number(row[at["次日跌停價"]]),
    )


def parse_dividend_results(body: Any, where: str) -> list[DividendResult]:
    """除權息計算結果：除權息前收盤價與參考價（官方，D37）。只取普通股。"""
    table = first_table(body, where)
    rows = rows_of(table, where)
    if not rows:
        return []
    at = field_positions(table.get("fields") or [], ("除權息日期", "代號", "除權息前收盤價", "除權息參考價"), where)
    results = []
    for row in rows:
        code = row[at["代號"]].strip()
        if not is_common_stock(code):
            continue
        day = roc_date(row[at["除權息日期"]])
        before, after = number(row[at["除權息前收盤價"]]), number(row[at["除權息參考價"]])
        if positive_pair(before, after, f"{where} {code} {day}"):
            results.append(DividendResult(code, day, before, after))
    return results


def parse_capital_reductions(body: Any, where: str) -> list[CorporateAction]:
    """減資恢復交易參考價：因子 = 減資恢復買賣開始日參考價格 ÷ 最後交易日之收盤價格。只取普通股。"""
    table = first_table(body, where)
    rows = rows_of(table, where)
    if not rows:
        return []
    at = field_positions(table.get("fields") or [],
                         ("恢復買賣日期", "股票代號", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格"), where)
    actions = []
    for row in rows:
        code = row[at["股票代號"]].strip()
        if not is_common_stock(code):
            continue
        day = roc_date(row[at["恢復買賣日期"]])
        before, after = number(row[at["最後交易日之收盤價格"]]), number(row[at["減資恢復買賣開始日參考價格"]])
        if positive_pair(before, after, f"{where} {code} {day}"):
            actions.append(CorporateAction(code, day, ActionKind.CAPITAL_REDUCTION, before, after))
    return actions
