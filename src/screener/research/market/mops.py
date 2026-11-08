"""公開資訊觀測站的月營收與當時的產業別（RESEARCH_PLAN.md 4.4、R20）。

已實測（2026-11-08）：
- 網址 `mopsov.twse.com.tw/nas/t21/{sii,otc}/t21sc03_{民國年}_{月}_{0,1}.html`：最後一碼 0 為國內公司、1 為外國公司
  （名稱帶 -KY），兩份都要抓。每月、每個市場各兩頁，一次涵蓋全市場。
- Big5 編碼的 HTML，依「產業別：XX」分段（上市 33 類、上櫃 28 類，每個月一份，沒有後見之明），單位千元。
- 資料列為 `<tr align=right>`，「去年同月增減」的標籤是大寫 `<Td>`，解析要不分大小寫。
- 舊月份的頁面顯示的是公司更正後的數字（已知限制）。
"""

import html
import re
import time
from collections.abc import Callable
from datetime import date

import requests

from screener.config import RequestSettings
from screener.data.base import DataSourceError
from screener.http import HttpClient, RetryableError
from screener.models import Market
from screener.research.market.common import ROC_OFFSET, field_positions, is_common_stock, number
from screener.research.market.models import MonthlyRevenue

BASE_URL = "https://mopsov.twse.com.tw/nas/t21"
ENCODING = "cp950"
_MARKET_PATH = {Market.TSE: "sii", Market.OTC: "otc"}
_HEADER = "公司<br>代號"
_SEGMENT = re.compile(r"產業別[：:]\s*([^<\s]+)|<tr\s+align=right[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_FIELDS = ("公司代號", "公司名稱", "當月營收", "上月營收", "去年當月營收", "當月累計營收", "去年累計營收")


class MopsClient:
    """回傳原始回應（位元組），由 parse_revenue 解析；原始回應另存壓縮檔。session、sleep、clock 可替換，方便測試。"""

    def __init__(self, settings: RequestSettings, *, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self._http = HttpClient(settings, session=session, sleep=sleep, clock=clock)

    def revenue_page(self, market: Market, month: date, *, foreign: bool) -> bytes:
        label = f"公開資訊觀測站月營收 {market.value} {month:%Y-%m}{'（外國公司）' if foreign else ''}"
        url = (f"{BASE_URL}/{_MARKET_PATH[market]}/t21sc03_{month.year - ROC_OFFSET}_{month.month}"
               f"_{1 if foreign else 0}.html")
        return self._http.get(url, {}, _read_bytes(label), label=label)


def _read_bytes(label: str) -> Callable[[requests.Response], bytes]:
    def read(response: requests.Response) -> bytes:
        status = response.status_code
        if status >= 500:
            raise RetryableError(f"HTTP {status}")
        if status != 200:
            raise DataSourceError(f"{label} 拒絕請求（HTTP {status}）")
        return response.content

    return read


def _cells(row: str) -> list[str]:
    return [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in _CELL.findall(row)]


def _header_positions(text: str, where: str) -> dict[str, int]:
    at = text.find(_HEADER)
    if at < 0:
        raise DataSourceError(f"{where} 找不到欄位列，格式可能已變動或沒有資料：{text[:200]!r}")
    row = text[text.rfind("<tr", 0, at):text.find("</tr>", at)]
    fields = [re.sub(r"<br>|\s", "", cell, flags=re.IGNORECASE) for cell in _CELL.findall(row)]
    return field_positions(fields, _FIELDS, where)


def _amount(cell: str) -> int | None:
    value = number(cell)
    return None if value is None else int(value)


def parse_revenue(content: bytes, month: date, market: Market, where: str) -> list[MonthlyRevenue]:
    """普通股的月營收與所屬的產業別；頁面沒有任何資料列時報錯（只查詢已經公布的月份）。"""
    text = content.decode(ENCODING, errors="replace")
    at = _header_positions(text, where)
    industry, rows = "", []
    for match in _SEGMENT.finditer(text):
        if match.group(1):
            industry = match.group(1).split("（")[0]     # 金融保險業的名稱後面接著括號說明
            continue
        cells = _cells(match.group(2))
        if len(cells) <= max(at.values()) or not is_common_stock(cells[at["公司代號"]]):
            continue                            # 合計列、存託憑證
        try:
            rows.append(MonthlyRevenue(
                code=cells[at["公司代號"]], month=month, market=market, name=cells[at["公司名稱"]], industry=industry,
                revenue=_amount(cells[at["當月營收"]]), previous=_amount(cells[at["上月營收"]]),
                last_year=_amount(cells[at["去年當月營收"]]), cumulative=_amount(cells[at["當月累計營收"]]),
                cumulative_last_year=_amount(cells[at["去年累計營收"]])))
        except ValueError as exc:
            raise DataSourceError(f"{where} {cells[at['公司代號']]} 的資料列格式不符（{exc}）：{cells!r}") from exc
    if not rows:
        raise DataSourceError(f"{where} 沒有任何普通股的資料列，格式可能已變動")
    return rows


def as_text(content: bytes) -> str:
    """原始回應存檔用：解碼後的 HTML。"""
    return content.decode(ENCODING, errors="replace")
