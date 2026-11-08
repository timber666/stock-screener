"""證交所、櫃買中心回應的共用解析（RESEARCH_PLAN.md 4.3）。

兩邊的端點都不是正式 API，格式可能變動：欄位一律以名稱對應（去掉空白與 <br>），找不到時報錯，
不以位置猜測。數字有千分位逗號；沒有值時是 "--"、"---"、"----" 或空白。
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

import requests

from screener.data.base import DataSourceError
from screener.http import RetryableError

logger = logging.getLogger(__name__)

_COMMON_STOCK = re.compile(r"[0-9]{4}")
_FIELD_NOISE = re.compile(r"<br>|\s")
_ROC_SEPARATED = re.compile(r"(\d{2,3})[/年](\d{1,2})[/月](\d{1,2})日?")
_ROC_COMPACT = re.compile(r"(\d{2,3})(\d{2})(\d{2})")
ROC_OFFSET = 1911                   # 民國年 + 1911 = 西元年


def is_common_stock(code: str) -> bool:
    """上市、上櫃普通股：代號為 4 碼數字，排除 00 開頭（ETF）與 91 開頭（存託憑證）。

    以代號規則分類，少數特殊證券可能誤判，品質報告列出分類結果供檢查（RESEARCH_PLAN.md 4.3）。
    """
    return bool(_COMMON_STOCK.fullmatch(code)) and not code.startswith(("00", "91"))


def number(text: str) -> float | None:
    """'1,234.50' → 1234.5；沒有值（"--"、"----"、空白）時為 None。"""
    cleaned = text.replace(",", "").replace(" ", "").strip()
    if not cleaned or set(cleaned) == {"-"}:
        return None
    return float(cleaned)


def count(text: str) -> int:
    """成交股數、金額、筆數；沒有值時為 0。"""
    value = number(text)
    return 0 if value is None else int(value)


def roc_date(text: str) -> date:
    """民國日期：'115/10/01'、'115年10月01日'、'1151027'。"""
    text = text.strip()
    match = _ROC_SEPARATED.fullmatch(text) or _ROC_COMPACT.fullmatch(text)
    if not match:
        raise ValueError(f"不是民國日期：{text!r}")
    year, month, day = (int(part) for part in match.groups())
    return date(year + ROC_OFFSET, month, day)


def to_roc(day: date) -> str:
    """'115/10/01' 形式的民國日期（櫃買中心的查詢參數）。"""
    return f"{day.year - ROC_OFFSET}/{day:%m/%d}"


def field_positions(fields: Sequence[str], names: Sequence[str], where: str) -> dict[str, int]:
    """欄位名稱（去掉空白與 <br>）→ 位置；缺少任一個時報錯。"""
    cleaned = [_FIELD_NOISE.sub("", field) for field in fields]
    missing = [name for name in names if name not in cleaned]
    if missing:
        raise DataSourceError(f"{where} 的欄位缺少 {'、'.join(missing)}，格式可能已變動：{list(fields)}")
    return {name: cleaned.index(name) for name in names}


def json_reader(label: str) -> Callable[[requests.Response], Any]:
    """HttpClient 的回應解讀：伺服器錯誤重試；其他 HTTP 錯誤與不是 JSON 的回應直接失敗。

    回應不是 JSON 時不重試：被暫時限制連線時通常回傳 HTML 頁面，馬上重試只會延長限制。
    """

    def read(response: requests.Response) -> Any:
        status = response.status_code
        if status >= 500:
            raise RetryableError(f"HTTP {status}")
        if status != 200:
            raise DataSourceError(f"{label} 拒絕請求（HTTP {status}）")
        try:
            return response.json()
        except ValueError:
            raise DataSourceError(f"{label} 的回應不是 JSON（可能被暫時限制連線，請稍後再執行）："
                                  f"{response.text[:200]!r}") from None

    return read


def rows_of(table: Mapping[str, Any], where: str) -> list[list[str]]:
    data = table.get("data")
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(row, list) for row in data):
        raise DataSourceError(f"{where} 的資料格式不符：{str(data)[:200]}")
    return data


def positive_pair(before: float | None, after: float | None, where: str) -> bool:
    """事件前後價格都要是正數才算得出調整因子；不是時記錄警告。"""
    if before is not None and after is not None and before > 0 and after > 0:
        return True
    logger.warning("%s 的事件前後價格不是正數，略過：%s → %s", where, before, after)
    return False
