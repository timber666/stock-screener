"""全市場資料用到的 FinMind 資料集：交易日曆、下市清單（RESEARCH_PLAN.md 4.3）。面額變更沿用 fetch_splits。

都是不分股票、一次涵蓋全市場的資料集，免費方案可用（2026-11-07 實測）：
- TaiwanStockTradingDate：2015-01-05 起 2,885 個交易日，含 2016～2018 年 8 個週六的補班交易日。
- TaiwanStockDelisting：726 筆（含 ETF、存託憑證等），欄位 date、stock_id、stock_name。
減資 TaiwanStockCapitalReductionReferencePrice 不指定代號時需要贊助會員，改用證交所、櫃買中心的資料。
"""

from datetime import date

from screener.data.base import DataSourceError
from screener.data.finmind import FinMindClient
from screener.research.market.common import is_common_stock
from screener.research.market.models import Delisting

TRADING_DATE_DATASET = "TaiwanStockTradingDate"
DELISTING_DATASET = "TaiwanStockDelisting"
EARLIEST = date(1990, 1, 1)         # 下市清單的查詢起點：涵蓋全部紀錄


class MarketFinMindClient(FinMindClient):
    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        try:
            return sorted(date.fromisoformat(row["date"]) for row in self._get(TRADING_DATE_DATASET, None, start, end))
        except (KeyError, TypeError, ValueError) as exc:
            raise DataSourceError(f"FinMind {TRADING_DATE_DATASET} 的資料格式不符（{exc}）") from exc

    def fetch_delistings(self, end: date) -> list[Delisting]:
        """普通股的下市紀錄；同一個代號有多筆時取最晚的一筆。"""
        latest: dict[str, Delisting] = {}
        try:
            for row in self._get(DELISTING_DATASET, None, EARLIEST, end):
                code = str(row["stock_id"])
                if not is_common_stock(code):
                    continue
                item = Delisting(code, date.fromisoformat(row["date"]), str(row["stock_name"]).strip())
                if code not in latest or item.date > latest[code].date:
                    latest[code] = item
        except (KeyError, TypeError, ValueError) as exc:
            raise DataSourceError(f"FinMind {DELISTING_DATASET} 的資料格式不符（{exc}）") from exc
        return sorted(latest.values(), key=lambda d: d.code)
