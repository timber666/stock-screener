"""TWSE MIS 盤中即時報價，實作 QuoteSource（PROJECT_PLAN.md 8.2）。

非官方的公開端點，可能變動。已實測確認的行為（2026-10-31，週六）：
- 一次請求可查多檔：ex_ch 以 | 串接，上市為 tse_、上櫃為 otc_。150 檔可以，290 檔因網址過長回 HTTP 414。
- 非交易日回傳最後一個交易日收盤後的資料，`d` 為該日日期。
- 代號或市場前綴錯誤時，該檔回傳 `c` 為空字串的資料，無法對應到代號。
- 價格為 "2370.0000" 形式的字串，沒有值時為 "-"；`v` 為累積成交量（張），只含一般交易；
  `b`／`a` 為最佳五檔買／賣價，以 _ 分隔。跌停鎖死時 `b` 為 "-"，漲停鎖死時 `a` 為 "-"。

盤中實測（2026-11-02 13:10）：`z` 是最近一次更新的成交價，那次更新只有掛單變動、沒有成交時為 "-"
（290 檔中 265 檔）；最後一筆成交記在 `trade`（`z` 價格、`t` 時間、`v` 張數），因此現價依序取 `z`、
`trade.z`（D47）。`pz` 同樣為 "-"，不使用。

每檔的原始資料列（約 40 個欄位，含五檔價量、漲跌停價）保留在 Quote.raw，盤中掃描推播後存檔（D46）。
"""

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import requests

from screener.config import MisSettings
from screener.data.base import DataSourceError
from screener.http import HttpClient, RetryableError
from screener.models import Quote, StockInfo

logger = logging.getLogger(__name__)

API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

_RTCODE_OK = "0000"
_HTTP_URI_TOO_LONG = 414
_NO_VALUE = "-"


class MisClient:
    """依 batch_size 分批查詢，並依設定控制請求間隔、逾時與重試。session、sleep、clock 可替換，方便測試。"""

    def __init__(
        self,
        settings: MisSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._batch_size = settings.batch_size
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def fetch_quotes(self, stocks: Sequence[StockInfo]) -> dict[str, Quote]:
        wanted = {stock.code for stock in stocks}
        quotes: dict[str, Quote] = {}
        for start in range(0, len(stocks), self._batch_size):
            batch = stocks[start:start + self._batch_size]
            params = {"ex_ch": "|".join(_channel(stock) for stock in batch), "json": "1", "delay": "0"}
            label = f"MIS {batch[0].code}～{batch[-1].code}（{len(batch)} 檔）"
            for record in self._http.get(API_URL, params, _read_records, label=label):
                code = record.get("c")
                if code in wanted:      # 代號或市場錯誤的股票，c 為空字串
                    quotes[code] = _parse_record(record)
        return quotes


def _channel(stock: StockInfo) -> str:
    return f"{stock.market.value}_{stock.code}.tw"


def _read_records(response: requests.Response) -> list[Mapping[str, Any]]:
    """取出回應的 msgArray。暫時性錯誤拋出 RetryableError，其餘錯誤拋出 DataSourceError。"""
    status = response.status_code
    if status >= 500:
        raise RetryableError(f"HTTP {status}")
    if status == _HTTP_URI_TOO_LONG:
        raise DataSourceError("MIS 拒絕請求（HTTP 414：網址過長），請調小 settings.yaml 的 mis.batch_size")
    if status != 200:
        raise DataSourceError(f"MIS 拒絕請求（HTTP {status}：{response.text[:200]}）")
    try:
        body = response.json()
    except ValueError:
        raise RetryableError("回應不是 JSON") from None
    if not isinstance(body, dict) or body.get("rtcode") != _RTCODE_OK:
        raise DataSourceError(f"MIS 回應錯誤：{str(body)[:200]}")
    records = body.get("msgArray")
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise DataSourceError(f"MIS 回應格式不符：{str(body)[:200]}")
    return records


def _parse_record(record: Mapping[str, Any]) -> Quote:
    code = record["c"]
    try:
        quote = Quote(
            code=code,
            date=datetime.strptime(record["d"], "%Y%m%d").date(),
            open=_price(record["o"]),
            high=_price(record["h"]),
            low=_price(record["l"]),
            last=_last_price(record),
            prev_close=_price(record["y"]),
            volume=None if record["v"] in ("", _NO_VALUE) else int(record["v"]),
            best_bid=_first_level(record["b"]),
            best_ask=_first_level(record["a"]),
            raw=dict(record),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(f"MIS {code} 的報價格式不符（{exc}）：{dict(record)!r}") from exc
    if quote.last is None:
        logger.debug("MIS %s 沒有成交價（z 與 trade.z 都沒有值）：%r", code, dict(record))
    return quote


def _last_price(record: Mapping[str, Any]) -> float | None:
    """最新成交價：`z` 為 "-"（最近一次更新只有掛單變動）時改用最後一筆成交 `trade.z`（D47）。"""
    price = _price(record["z"])
    if price is None and isinstance(trade := record.get("trade"), Mapping):
        price = _price(trade.get("z", _NO_VALUE))
    return price


def _price(value: str) -> float | None:
    """ "2370.0000" → 2370.0；"-"、空字串或不是正數時為 None。"""
    if value in ("", _NO_VALUE):
        return None
    price = float(value)
    return price if price > 0 else None


def _first_level(levels: str) -> float | None:
    """最佳五檔的第一檔，例 "2365.0000_2360.0000_..._" → 2365.0；沒有掛單時為 "-"。"""
    return _price(levels.split("_")[0])
