"""品質檢查：每檔每天的 1 分 K 合起來，和 screener.db 的日 K 比對（REALTIME_PLAN.md 7.2 第 4 點）。純函式：不碰資料庫。

- 合起來：開 = 第一根的開盤、高低 = 最高與最低、收 = 最後一根的收盤、量 = 合計。
- 開高低收以 normalize（PROJECT_PLAN.md 10.3）比較，要完全相同。
- 量：1 分 K 只含一般交易，日 K 含零股、盤後定價、鉅額交易（D21），比例應該略低於 1。
- 格式：K 棒都在交易時段內（09:00～收盤集合競價前一分鐘，以及 13:30 的收盤成交），有沒有 09:00 與 13:30 那根。
"""

from datetime import date, datetime, timedelta

import pandas as pd

from screener.analysis.numeric import normalize
from screener.realtime.config import Session

COMPARED = "compared"            # 有日 K、有 1 分 K：比對開高低收與量
NO_DATA = "no_data"              # 有日 K、查過但沒有 1 分 K
NOT_FETCHED = "not_fetched"      # 有日 K、還沒回補
NO_DAILY = "no_daily"            # 有 1 分 K、沒有日 K

PRICE_COLUMNS = ("open", "high", "low", "close")
BAR_COLUMNS = ("date", "minute", "open", "high", "low", "close", "volume")
DAILY_COLUMNS = ("date", "open", "high", "low", "close", "volume")

_RESULT_COLUMNS = ["date", "year", "status", "before_listing", "d_open", "d_high", "d_low", "d_close", "d_volume",
                   "b_open", "b_high", "b_low", "b_close", "b_volume", "bars", "first_minute", "last_minute",
                   "has_open_bar", "has_close_bar", "outside_session", "same_ohlc", "volume_ratio"]


def _hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def session_minutes(session: Session) -> tuple[str, str, str]:
    """(第一根、收盤集合競價前最後一根、收盤成交那根) 的開始時間 HH:MM，例：09:00、13:24、13:30。"""
    last_regular = datetime.combine(date.min, session.closing_call) - timedelta(minutes=1)
    return session.open.strftime("%H:%M"), _hhmm(last_regular), session.close.strftime("%H:%M")


def aggregate_days(bars: pd.DataFrame, session: Session) -> pd.DataFrame:
    """bars：一檔股票的 1 分 K（BAR_COLUMNS，依 date、minute 排序）。回傳以 date 為索引的每日合計。"""
    columns = ["open", "high", "low", "close", "volume", "bars", "first_minute", "last_minute", "has_open_bar",
               "has_close_bar", "outside_session"]
    if bars.empty:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="date"))
    first, last_regular, close = session_minutes(session)
    minute = bars["minute"]
    marked = bars.assign(is_open=minute == first, is_close=minute == close,
                         outside=(minute < first) | ((minute > last_regular) & (minute != close)))
    grouped = marked.groupby("date", sort=True)
    return pd.DataFrame({
        "open": grouped["open"].first(),
        "high": grouped["high"].max(),
        "low": grouped["low"].min(),
        "close": grouped["close"].last(),
        "volume": grouped["volume"].sum(),
        "bars": grouped.size(),
        "first_minute": grouped["minute"].first(),
        "last_minute": grouped["minute"].last(),
        "has_open_bar": grouped["is_open"].any(),
        "has_close_bar": grouped["is_close"].any(),
        "outside_session": grouped["outside"].sum(),
    })


def check_stock(daily: pd.DataFrame, fetched: dict[date, int], bars: pd.DataFrame, session: Session,
                listed_on: date | None = None) -> pd.DataFrame:
    """一檔股票每個股日一列：狀態、日 K 與 1 分 K 合起來的值、比對結果。

    daily：screener.db 的日 K（DAILY_COLUMNS，只含要檢查的範圍）；fetched：intraday.db 的 days（股日 → K 棒數）；
    bars：intraday.db 的 1 分 K（BAR_COLUMNS）。只列出有日 K 或有 1 分 K 的股日。
    """
    agg = aggregate_days(bars, session).to_dict("index")
    daily_by_day = daily.set_index("date").to_dict("index") if not daily.empty else {}
    fetched_text = {day.isoformat() for day in fetched}
    rows = []
    for day in sorted(daily_by_day.keys() | agg.keys()):
        has_daily = day in daily_by_day
        has_bars = day in agg
        if has_bars:
            status = COMPARED if has_daily else NO_DAILY
        else:
            status = NO_DATA if day in fetched_text else NOT_FETCHED
        row: dict[str, object] = {"date": day, "year": int(day[:4]), "status": status,
                                  "before_listing": bool(listed_on and date.fromisoformat(day) < listed_on)}
        if has_daily:
            d = daily_by_day[day]
            row.update(d_open=float(d["open"]), d_high=float(d["high"]), d_low=float(d["low"]),
                       d_close=float(d["close"]), d_volume=int(d["volume"]))
        if has_bars:
            b = agg[day]
            row.update(b_open=float(b["open"]), b_high=float(b["high"]), b_low=float(b["low"]),
                       b_close=float(b["close"]), b_volume=int(b["volume"]), bars=int(b["bars"]),
                       first_minute=b["first_minute"], last_minute=b["last_minute"],
                       has_open_bar=bool(b["has_open_bar"]), has_close_bar=bool(b["has_close_bar"]),
                       outside_session=int(b["outside_session"]))
        if status == COMPARED:
            row["same_ohlc"] = all(normalize(row[f"d_{name}"]) == normalize(row[f"b_{name}"])   # type: ignore[arg-type]
                                   for name in PRICE_COLUMNS)
            row["volume_ratio"] = (normalize(row["b_volume"] / row["d_volume"])                   # type: ignore[operator]
                                   if row["d_volume"] else None)
        rows.append(row)
    return pd.DataFrame(rows, columns=_RESULT_COLUMNS)
