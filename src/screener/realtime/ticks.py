"""錄製檔中即時成交（Shioaji TickSTKv1 的 to_dict）的解析（REALTIME_PLAN.md 7.1 第 4 點）。

錄製檔只存原樣，解析在這裡做：欄位的意義在錄製日實測（T8）後確認，改這裡就好，不必重錄。
目前採用的欄位（Shioaji 1.7 的 TickSTKv1）：code、datetime（成交時間）、close（成交價，字串）、volume（張）、
simtrade（試撮）、intraday_odd（盤中零股，不計入）。
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from screener.realtime.bars import Trade
from screener.realtime.recorder import TICK

REQUIRED_FIELDS = ("code", "datetime", "close", "volume")


class TickFormatError(ValueError):
    """錄製的成交缺少需要的欄位（Shioaji 的格式和預期不同）。"""


def trade_from_tick(data: Mapping[str, Any]) -> Trade | None:
    """一筆即時成交轉成 Trade；盤中零股回傳 None。"""
    missing = [name for name in REQUIRED_FIELDS if name not in data]
    if missing:
        raise TickFormatError(f"成交缺少欄位 {', '.join(missing)}；實際的欄位：{', '.join(sorted(data))}")
    if data.get("intraday_odd"):
        return None
    return Trade(time=datetime.fromisoformat(str(data["datetime"])), price=float(data["close"]),
                 volume=int(data["volume"]), is_trial=bool(data.get("simtrade", False)))


def trades_by_code(records: Iterable[Mapping[str, Any]], codes: Iterable[str]) -> dict[str, list[Trade]]:
    """錄製檔中指定股票的成交（依收到的順序；排序與排除在 bars.session_trades）。"""
    wanted = {code: [] for code in codes}
    for record in records:
        if record.get("type") != TICK:
            continue
        data = record["data"]
        code = data.get("code")
        if code not in wanted:
            continue
        trade = trade_from_tick(data)
        if trade is not None:
            wanted[code].append(trade)
    return wanted
