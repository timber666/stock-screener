"""FinMind 台股資料，實作 DailyPriceSource（PROJECT_PLAN.md 8.1）與 EventSource（14.2）。

已實測確認的 API 行為：
- 成交量 Trading_Volume 單位為股，與證交所「個股日成交資訊」相同（含一般、零股、盤後定價、鉅額交易）。
- 沒有整股成交的日子通常沒有資料列，但也可能是一筆價格全為 0 的資料列（停牌，或只有零股成交）；
  查無資料時回傳空陣列，不是錯誤。
- 超過每小時請求上限時回 HTTP 402；token 錯誤等參數問題回 HTTP 400，訊息在 `msg`。
- 除權息、減資、面額變更的注意事項見 14.2，取自 Taiwan-Quant-Research 專案的實測紀錄。
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import requests

from screener.config import FinMindSettings
from screener.data.base import DataSourceError
from screener.http import HttpClient, RetryableError
from screener.models import ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult

logger = logging.getLogger(__name__)

API_URL = "https://api.finmindtrade.com/api/v4/data"
PRICE_DATASET = "TaiwanStockPrice"
DIVIDEND_DATASET = "TaiwanStockDividend"
DIVIDEND_RESULT_DATASET = "TaiwanStockDividendResult"
CAPITAL_REDUCTION_DATASET = "TaiwanStockCapitalReductionReferencePrice"
SPLIT_DATASET = "TaiwanStockSplitPrice"
SHARES_PER_LOT = 1000               # 1 張 = 1000 股

_HTTP_RATE_LIMITED = 402


class FinMindClient:
    """依設定控制請求間隔、逾時與重試。session、sleep、clock 可替換，方便測試。"""

    def __init__(
        self,
        settings: FinMindSettings,
        token: str | None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._dividend_lookahead = timedelta(days=settings.dividend_lookahead_days)
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def fetch_daily_prices(self, code: str, start: date, end: date) -> list[DailyBar]:
        rows = self._get(PRICE_DATASET, code, start, end)
        bars = [_parse_price(code, row) for row in rows]
        return [bar for bar in bars if bar is not None]

    def fetch_dividends(self, code: str, start: date, end: date) -> list[DividendEvent]:
        """除權息交易日落在 start～end 的事件；同一天的多筆公告合併成一筆。

        現金與股票股利可能在不同天除息、除權，各自歸到自己的交易日；其中一個日期空白時用另一個。

        TaiwanStockDividend 的日期篩選套在權利分派基準日，比除權息交易日晚 6～10 天（Taiwan-Quant-Research
        實測）。直接用 end 查詢會漏掉「除權息日在 end 之前、基準日在之後」的事件，所以迄日往後放寬
        dividend_lookahead_days 天（settings.yaml）再自行濾回。
        """
        rows = self._get(DIVIDEND_DATASET, code, start, end + self._dividend_lookahead)
        amounts: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])     # 交易日 → [現金, 股票]
        for row in rows:
            with _row_errors(code, row):
                _check_code(code, row)
                cash = _amount(row["CashEarningsDistribution"]) + _amount(row["CashStatutorySurplus"])
                stock = _amount(row["StockEarningsDistribution"]) + _amount(row["StockStatutorySurplus"])
                cash_day = _optional_date(row["CashExDividendTradingDate"])
                stock_day = _optional_date(row["StockExDividendTradingDate"])
            if (cash or stock) and cash_day is None and stock_day is None:
                logger.warning("FinMind %s 的股利沒有除權息交易日，略過：%r", code, row)
                continue
            for i, amount, day in ((0, cash, cash_day or stock_day), (1, stock, stock_day or cash_day)):
                if amount and start <= day <= end:
                    amounts[day][i] += amount
        return [DividendEvent(code, day, cash, stock) for day, (cash, stock) in sorted(amounts.items())]

    def fetch_dividend_results(self, code: str, start: date, end: date) -> list[DividendResult]:
        results = []
        for row in self._get(DIVIDEND_RESULT_DATASET, code, start, end):
            with _row_errors(code, row):
                _check_code(code, row)
                result = DividendResult(code, date.fromisoformat(row["date"]),
                                        float(row["before_price"]), float(row["after_price"]))
            if result.before_price > 0 and result.after_price > 0:
                results.append(result)
        return results

    def fetch_capital_reductions(self, code: str, start: date, end: date) -> list[CorporateAction]:
        actions = []
        for row in self._get(CAPITAL_REDUCTION_DATASET, code, start, end):
            with _row_errors(code, row):
                _check_code(code, row)
                action = CorporateAction(code, date.fromisoformat(row["date"]), ActionKind.CAPITAL_REDUCTION,
                                         float(row["ClosingPriceonTheLastTradingDay"]),
                                         float(row["PostReductionReferencePrice"]))
            actions.append(action)
        return _valid_actions(actions)

    def fetch_splits(self, start: date, end: date) -> list[CorporateAction]:
        actions = []
        for row in self._get(SPLIT_DATASET, None, start, end):
            with _row_errors("全市場", row):
                actions.append(CorporateAction(str(row["stock_id"]), date.fromisoformat(row["date"]),
                                               ActionKind.SPLIT, float(row["before_price"]),
                                               float(row["after_price"])))
        return _valid_actions(actions)

    def _get(self, dataset: str, code: str | None, start: date, end: date) -> list[Any]:
        params = {"dataset": dataset, "start_date": start.isoformat(), "end_date": end.isoformat()}
        if code is not None:
            params["data_id"] = code
        label = f"FinMind {dataset} {code or '全市場'}"
        return self._http.get(API_URL, params, _read_data, label=label, headers=self._headers)


def _read_data(response: requests.Response) -> list[Any]:
    """取出回應的 `data`。暫時性錯誤拋出 RetryableError，其餘錯誤拋出 DataSourceError。"""
    status = response.status_code
    if status >= 500:
        raise RetryableError(f"HTTP {status}")
    try:
        body = response.json()
    except ValueError:
        body = None
    message = body.get("msg") if isinstance(body, dict) else response.text[:200]

    if status == _HTTP_RATE_LIMITED:
        raise DataSourceError(f"已達 FinMind 每小時請求上限（HTTP 402：{message}），請一小時後再執行")
    if status != 200:
        raise DataSourceError(f"FinMind 拒絕請求（HTTP {status}：{message}）")
    if body is None:
        raise RetryableError("回應不是 JSON")
    if not isinstance(body, dict) or body.get("status") != 200 or not isinstance(body.get("data"), list):
        raise DataSourceError(f"FinMind 回應格式不符：{str(body)[:200]}")
    return body["data"]


def _parse_price(code: str, row: Any) -> DailyBar | None:
    """轉成 DailyBar（成交量由股換算成張，四捨五入）。沒有整股成交或價格不是正數的資料列略過。"""
    with _row_errors(code, row):
        _check_code(code, row)
        bar = DailyBar(
            code=code,
            date=date.fromisoformat(row["date"]),
            open=float(row["open"]),
            high=float(row["max"]),
            low=float(row["min"]),
            close=float(row["close"]),
            volume=(int(row["Trading_Volume"]) + SHARES_PER_LOT // 2) // SHARES_PER_LOT,
        )

    prices = (bar.open, bar.high, bar.low, bar.close)
    if all(price == 0 for price in prices):
        # 停牌或只有零股成交的日子，FinMind 可能回傳價格全為 0 的資料列：與沒有資料列一樣視為沒有交易
        logger.info("FinMind %s %s 沒有整股成交（價格為 0），略過", code, bar.date)
        return None
    if min(prices) <= 0:
        logger.warning("FinMind %s %s 的價格不是正數，略過：%r", code, bar.date, row)
        return None
    return bar


@contextmanager
def _row_errors(code: str, row: Any) -> Iterator[None]:
    """把資料列缺欄位、型別或格式錯誤轉成 DataSourceError。"""
    try:
        yield
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(f"FinMind {code} 的資料列格式不符（{exc}）：{row!r}") from exc


def _check_code(code: str, row: Mapping[str, Any]) -> None:
    if row["stock_id"] != code:
        raise ValueError(f"stock_id 為 {row['stock_id']}")


def _amount(value: Any) -> float:
    """股利金額；缺值或空白視為 0。"""
    return float(value) if value not in (None, "") else 0.0


def _optional_date(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def _valid_actions(actions: list[CorporateAction]) -> list[CorporateAction]:
    """事件前後價格要是正數才算得出調整因子，其餘記錄警告後略過。"""
    valid = [a for a in actions if a.before_price > 0 and a.after_price > 0]
    for action in actions:
        if action not in valid:
            logger.warning("FinMind %s %s 的%s前後價格不是正數，略過：%s → %s", action.code, action.date,
                           action.kind.value, action.before_price, action.after_price)
    return valid
