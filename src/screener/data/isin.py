"""證交所的本國上市、上櫃證券 ISIN 清單：上市日／上櫃日（PROJECT_PLAN.md 14.2、D38）。

FinMind 的日 K 包含部分股票上櫃前的興櫃交易，興櫃沒有漲跌幅限制、開盤價也不正確；回測以這裡的
上市日／上櫃日排除之前的訊號。已確認（2026-11-01）：
- 上市與上櫃各一頁 HTML，Big5（cp950）編碼，約 10 MB 與 3 MB；只列目前掛牌的證券。
- 每列為「代號　名稱」（全形空白分隔）、ISIN、上市日（YYYY/MM/DD）、市場別、產業別、CFICode、備註。
- 日期是在目前市場掛牌的日子：由上櫃轉上市的股票，記的是轉上市的日子。
"""

import re
import time
from collections.abc import Callable
from datetime import datetime

import requests

from screener.config import RequestSettings
from screener.data.base import DataSourceError
from screener.http import HttpClient, RetryableError
from screener.models import Listing, Market

URLS = {
    Market.TSE: "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
    Market.OTC: "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
}
ENCODING = "cp950"
NO_PRICE_LIMIT_DAYS = 5             # 掛牌後前五個交易日沒有漲跌幅限制

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_SECURITY = re.compile(r"^(\w+)　")
_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")


class IsinClient:
    """session、sleep、clock 可替換，方便測試。"""

    def __init__(
        self,
        settings: RequestSettings,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def fetch_listings(self) -> dict[str, Listing]:
        """所有上市、上櫃證券的掛牌日，key 為代號（含權證等，呼叫端自行篩選）。"""
        listings: dict[str, Listing] = {}
        for market, url in URLS.items():
            html = self._http.get(url, {}, _read_html, label=f"證交所 ISIN 清單（{market.value}）")
            listings.update(parse_listings(html, market))
        if not listings:
            raise DataSourceError("證交所 ISIN 清單沒有任何資料，格式可能已變動")
        return listings


def _read_html(response: requests.Response) -> str:
    status = response.status_code
    if status >= 500:
        raise RetryableError(f"HTTP {status}")
    if status != 200:
        raise DataSourceError(f"證交所 ISIN 清單拒絕請求（HTTP {status}）")
    return response.content.decode(ENCODING, errors="replace")


def parse_listings(html: str, market: Market) -> dict[str, Listing]:
    listings = {}
    for row in _ROW.findall(html):
        cells = [_TAG.sub("", cell).strip() for cell in _CELL.findall(row)]
        if len(cells) < 3 or not (match := _SECURITY.match(cells[0])) or not _DATE.match(cells[2]):
            continue                            # 分類標題列（例：「股票」）或表頭
        code = match.group(1)
        listings[code] = Listing(code, market, datetime.strptime(cells[2], "%Y/%m/%d").date())
    return listings
