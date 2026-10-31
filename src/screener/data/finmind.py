"""FinMind 台股日成交資訊（TaiwanStockPrice），實作 DailyPriceSource（PROJECT_PLAN.md 8.1）。

已實測確認的 API 行為：
- 成交量 Trading_Volume 單位為股，與證交所「個股日成交資訊」相同（含一般、零股、盤後定價、鉅額交易）。
- 沒有整股成交的日子通常沒有資料列，但也可能是一筆價格全為 0 的資料列（停牌，或只有零股成交）；
  查無資料時回傳空陣列，不是錯誤。
- 超過每小時請求上限時回 HTTP 402；token 錯誤等參數問題回 HTTP 400，訊息在 `msg`。
"""

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import requests

from screener.config import FinMindSettings
from screener.data.base import DataSourceError
from screener.data.http import HttpClient, RetryableError
from screener.models import DailyBar

logger = logging.getLogger(__name__)

API_URL = "https://api.finmindtrade.com/api/v4/data"
DATASET = "TaiwanStockPrice"
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
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def fetch_daily_prices(self, code: str, start: date, end: date) -> list[DailyBar]:
        params = {
            "dataset": DATASET,
            "data_id": code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        rows = self._http.get(API_URL, params, _read_data, label=f"FinMind {DATASET} {code}",
                              headers=self._headers)
        bars = [_parse_row(code, row) for row in rows]
        return [bar for bar in bars if bar is not None]


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


def _parse_row(code: str, row: Any) -> DailyBar | None:
    """轉成 DailyBar（成交量由股換算成張，四捨五入）。沒有整股成交或價格不是正數的資料列略過。"""
    try:
        if row["stock_id"] != code:
            raise ValueError(f"stock_id 為 {row['stock_id']}")
        bar = DailyBar(
            code=code,
            date=date.fromisoformat(row["date"]),
            open=float(row["open"]),
            high=float(row["max"]),
            low=float(row["min"]),
            close=float(row["close"]),
            volume=(int(row["Trading_Volume"]) + SHARES_PER_LOT // 2) // SHARES_PER_LOT,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(f"FinMind {code} 的資料列格式不符（{exc}）：{row!r}") from exc

    prices = (bar.open, bar.high, bar.low, bar.close)
    if all(price == 0 for price in prices):
        # 停牌或只有零股成交的日子，FinMind 可能回傳價格全為 0 的資料列：與沒有資料列一樣視為沒有交易
        logger.info("FinMind %s %s 沒有整股成交（價格為 0），略過", code, bar.date)
        return None
    if min(prices) <= 0:
        logger.warning("FinMind %s %s 的價格不是正數，略過：%r", code, bar.date, row)
        return None
    return bar
