"""全市場資料的品質檢查（RESEARCH_PLAN.md 4.3「品質報告」）。

每個檢查回傳終端摘要的文字與明細表；明細由 jobs/market_check.py 寫成 CSV。資料以 DataFrame 傳入，
日期為 YYYY-MM-DD 字串，方便以 SQL 讀出後直接使用、也方便測試。
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
import pandas as pd

from screener.analysis.market import PRICE_LIMIT, tick_size
from screener.research.market.models import SHARES_PER_LOT

FIRST_DAYS = 5                      # 轉上市、新掛牌後檢查漲跌幅的交易日數
GAP_DAYS = 5                        # 連續幾個交易日不在行情表中才列為中斷
SUSPICIOUS_NAMES = ("-DR", "ETF", "ETN", "債", "受益", "特別股", "-創")   # 以代號分類為普通股、名稱卻像特殊證券；
                                                                          # 「-創」為創新板，列出供確認
FIRST, TRANSFER, RESUMED, NO_TRADE, UNEXPLAINED = "第一次出現", "轉換市場的第一天", "停止交易後恢復", "前一天沒有成交", "無法解釋"


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)   # CSV 檔名（不含副檔名）→ 明細


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "-"


def _ticks(a: float, b: float) -> float:
    """a 與 b 差幾個升降單位（以 b 的升降單位計）。"""
    return abs(a - b) / float(tick_size(Decimal(repr(b))))


# --- 1. 概況 -------------------------------------------------------------------

def overview(quotes: pd.DataFrame, trading_days: Sequence[str], calendar: Sequence[str], closed_days: Sequence[str],
             securities: pd.DataFrame) -> Section:
    section = Section("概況")
    days = list(trading_days)
    if not days:
        section.lines.append("market.db 沒有任何行情")
        return section
    section.lines.append(f"交易日 {len(days):,} 天（{days[0]}～{days[-1]}）；非交易日 {len(closed_days):,} 天"
                         f"（週一至五的假日、颱風假）")
    per_day = quotes.groupby(["date", "market"]).size().unstack(fill_value=0)
    for market, label in (("tse", "上市"), ("otc", "上櫃")):
        counts = per_day.get(market, pd.Series(dtype=int))
        if counts.empty:
            continue
        section.lines.append(f"{label}：每天 {counts.min():,}～{counts.max():,} 檔（中位數 {int(counts.median()):,}），"
                             f"最後一天 {counts.iloc[-1]:,} 檔")
    delisted = int(securities["delisted_on"].notna().sum()) if len(securities) else 0
    section.lines.append(f"證券 {len(securities):,} 檔（其中有 FinMind 下市紀錄 {delisted:,} 檔）")
    in_range = [d for d in calendar if days[0] <= d <= days[-1]]
    missing = sorted(set(in_range) - set(days))
    extra = sorted(set(days) - set(calendar)) if calendar else []
    if calendar:
        section.lines.append(f"FinMind 交易日曆 {len(in_range):,} 天：沒有行情的 {len(missing)} 天、"
                             f"有行情卻不在日曆上的 {len(extra)} 天")
    else:
        section.lines.append("market.db 沒有交易日曆（market-update 執行後才有），未比對")
    if missing or extra:
        section.tables["calendar_mismatch"] = pd.DataFrame(
            [{"date": d, "problem": "日曆有、沒有行情"} for d in missing]
            + [{"date": d, "problem": "有行情、日曆沒有"} for d in extra])
    return section


# --- 2. 與 screener.db 比對 -------------------------------------------------------

def compare_watchlist(quotes: pd.DataFrame, watchlist: pd.DataFrame, listings: pd.DataFrame,
                      trading_days: Sequence[str]) -> Section:
    """watchlist：screener.db 的日 K（code、date、open、high、low、close、volume 張）；listings：上市櫃日。

    只比 market.db 已抓取的交易日。上市的開高低收與成交量要完全相同；上櫃的開高低收要相同，成交量另列差異（R10）。
    """
    section = Section("與 screener.db（觀察清單，FinMind）比對")
    watchlist = watchlist[watchlist["date"].isin(set(trading_days))]
    traded = quotes[quotes["close"].notna()]
    merged = watchlist.merge(traded, on=["code", "date"], how="left", suffixes=("", "_m"), indicator=True)
    found = merged[merged["_merge"] == "both"].copy()
    found["lots"] = (found["volume_m"].astype(np.int64) + SHARES_PER_LOT // 2) // SHARES_PER_LOT
    price_equal = np.ones(len(found), dtype=bool)
    for column in ("open", "high", "low", "close"):
        price_equal &= np.isclose(found[column], found[f"{column}_m"], rtol=0, atol=1e-9)
    found["price_equal"] = price_equal
    found["volume_equal"] = found["lots"] == found["volume"]
    rows = []
    for market, label in (("tse", "上市"), ("otc", "上櫃")):
        subset = found[found["market"] == market]
        if subset.empty:
            continue
        n, price_ok, volume_ok = len(subset), int(subset["price_equal"].sum()), int(subset["volume_equal"].sum())
        section.lines.append(f"{label}：比對 {n:,} 筆，開高低收相同 {price_ok:,}（{_pct(price_ok, n)}），"
                             f"成交量相同 {volume_ok:,}（{_pct(volume_ok, n)}）")
        if market == "otc":
            diff = (subset["lots"] / subset["volume"].where(subset["volume"] > 0) - 1) * 100
            diff = diff.dropna()
            if len(diff):
                q = diff.quantile([0, 0.05, 0.5, 0.95, 1])
                section.lines.append(f"  上櫃成交量（櫃買中心 ÷ FinMind − 1）：最小 {q[0]:+.2f}%、5% {q[0.05]:+.2f}%、"
                                     f"中位數 {q[0.5]:+.2f}%、95% {q[0.95]:+.2f}%、最大 {q[1]:+.2f}%（R10，照櫃買中心存）")
        bad = subset[~subset["price_equal"] | (~subset["volume_equal"] if market == "tse" else False)]
        rows += [{"code": r.code, "date": r.date, "market": market, "problem": "開高低收不同" if not r.price_equal
                  else "成交量不同", "screener": f"{r.open}/{r.high}/{r.low}/{r.close} {r.volume}張",
                  "market_db": f"{r.open_m}/{r.high_m}/{r.low_m}/{r.close_m} {r.lots}張"} for r in bad.itertuples()]

    missing = merged[merged["_merge"] == "left_only"][["code", "date"]].merge(listings, on="code", how="left")
    before_listing = missing["listed_on"].notna() & (missing["date"] < missing["listed_on"])
    section.lines.append(f"screener.db 有、market.db 沒有成交的 {len(missing):,} 筆：掛牌前（興櫃，D38）"
                         f"{int(before_listing.sum()):,} 筆、其他 {int((~before_listing).sum()):,} 筆")
    rows += [{"code": r.code, "date": r.date, "market": "", "problem": "market.db 沒有成交", "screener": "",
              "market_db": ""} for r in missing[~before_listing].itertuples()]

    span = watchlist.groupby("code")["date"].agg(["min", "max"])
    extra = traded[traded["code"].isin(span.index)].join(span, on="code")
    extra = extra[(extra["date"] >= extra["min"]) & (extra["date"] <= extra["max"])]
    extra = extra.merge(watchlist[["code", "date"]], on=["code", "date"], how="left", indicator=True)
    extra = extra[extra["_merge"] == "left_only"]
    section.lines.append(f"market.db 有成交、screener.db 沒有的 {len(extra):,} 筆")
    rows += [{"code": r.code, "date": r.date, "market": r.market, "problem": "screener.db 沒有", "screener": "",
              "market_db": f"{r.open}/{r.high}/{r.low}/{r.close}"} for r in extra.itertuples()]
    if rows:
        section.tables["watchlist_mismatch"] = pd.DataFrame(rows)
    return section


# --- 3. 除權息與減資 -------------------------------------------------------------

def _locate(events: pd.DataFrame, traded: pd.DataFrame) -> pd.DataFrame:
    """把事件日順延到該檔之後第一個有成交的日子（與還原的計算相同），加上當天的行情。"""
    if events.empty:
        return events.assign(trade_date=pd.Series(dtype=object))
    left = events.assign(_on=pd.to_datetime(events["date"])).sort_values("_on")
    right = traded.assign(_on=pd.to_datetime(traded["date"]))[
        ["code", "_on", "date", "market", "close", "change", "flag", "prev_close", "comparable"]].rename(
        columns={"date": "trade_date", "market": "quote_market"}).sort_values("_on")
    located = pd.merge_asof(left, right, on="_on", by="code", direction="forward")
    return located.drop(columns="_on")


def _within(events: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    """market.db 行情期間內的事件。"""
    return events[(events["date"] >= quotes["date"].min()) & (events["date"] <= quotes["date"].max())]


def _not_in(frame: pd.DataFrame, keys: set[tuple[str, str]]) -> pd.Series:
    """(code, date) 不在 keys 中的列。以 Series 篩選：空的 list 會被 pandas 當成選欄位。"""
    return pd.Series([key not in keys for key in zip(frame["code"], frame["date"])], index=frame.index, dtype=bool)


def check_events(quotes: pd.DataFrame, events: pd.DataFrame) -> Section:
    """events：官方事件（code、date、kind、market、before_price、after_price、source）。

    逐筆檢查：行情當天有沒有標示（X、除息等），推得出參考價時與官方差幾檔（事件日沒有成交、前一天沒有成交或
    停止交易時，參考價經過調整，不能比）；反過來，有標示卻沒有官方事件的日子，分成「前一天沒有成交」
    「第一次出現」「轉換市場的第一天」「停止交易後恢復」與無法解釋。停止交易前後價格的檢查在 check_anomalies。
    """
    section = Section("除權息、減資與面額變更")
    events = _within(events, quotes)
    q = quotes.sort_values(["code", "date"]).copy()
    q["prev_row_close"] = q.groupby("code")["close"].shift(1)
    q["prev_row_date"] = q.groupby("code")["date"].shift(1)
    q["prev_row_market"] = q.groupby("code")["market"].shift(1)
    days = sorted(q["date"].unique())
    position = {d: i for i, d in enumerate(days)}
    pos = q["date"].map(position)
    q["consecutive"] = pos - pos.groupby(q["code"]).shift(1) == 1          # 前一列就是前一個交易日
    q["comparable"] = q["prev_row_close"].notna() & q["consecutive"]
    traded = q[q["close"].notna()].copy()
    traded["prev_close"] = traded.groupby("code")["close"].shift(1)
    traded["reference"] = traded["close"] - traded["change"]

    located = _locate(events, traded)
    located = located[located["trade_date"].notna()]
    rows = []
    summary: dict[tuple[str, str], dict[str, int]] = {}
    for r in located.itertuples():
        key = (r.quote_market, r.kind)          # 面額變更（FinMind）沒有市場，以當天行情的市場分類
        counts = summary.setdefault(key, {"total": 0, "flag": 0, "same": 0, "tick": 0, "differ": 0, "none": 0,
                                          "reset": 0})
        counts["total"] += 1
        if isinstance(r.flag, str):
            counts["flag"] += 1
            continue
        if np.isnan(r.change):
            counts["none"] += 1
            continue
        first_day = days[bisect_left(days, r.date)] if bisect_left(days, r.date) < len(days) else None
        if r.trade_date != first_day or not r.comparable:
            counts["reset"] += 1                # 事件日沒有成交、前一天沒有成交或停止交易：參考價經過調整
            continue
        reference = r.close - r.change
        ticks = _ticks(reference, r.after_price)
        bucket = "same" if ticks < 1e-6 else ("tick" if ticks <= 1 + 1e-6 else "differ")
        counts[bucket] += 1
        if bucket == "differ":
            rows.append({"code": r.code, "date": r.date, "trade_date": r.trade_date, "kind": r.kind,
                         "source": r.source, "before": r.before_price, "official": r.after_price,
                         "derived": round(reference, 4), "ticks": round(ticks, 1)})
    labels = {"dividend": "除權息", "capital_reduction": "減資", "split": "面額變更"}
    for (market, kind), c in sorted(summary.items()):
        section.lines.append(
            f"{'上市' if market == 'tse' else '上櫃'}{labels.get(kind, kind)} {c['total']:,} 筆：行情有標示 {c['flag']:,}；"
            f"沒有標示、推出的參考價與官方相同 {c['same']:,}、差一檔以內 {c['tick']:,}、差更多 {c['differ']:,}；"
            f"事件日或前一天沒有成交而不能比 {c['reset']:,}；推不出 {c['none']:,}")
    section.lines.append(f"期間內的官方事件 {len(events):,} 筆中，之後沒有成交而無法比對的 {len(events) - len(located):,} 筆")
    if rows:
        section.tables["event_reference_mismatch"] = pd.DataFrame(rows)

    official_days = set(zip(located["code"], located["trade_date"]))
    flagged = traded[traded["flag"].notna()]
    unexplained = flagged[_not_in(flagged, official_days)]
    reasons = np.select([unexplained["prev_row_date"].isna(), unexplained["prev_row_market"] != unexplained["market"],
                         ~unexplained["consecutive"], unexplained["prev_row_close"].isna()],
                        [FIRST, TRANSFER, RESUMED, NO_TRADE], default=UNEXPLAINED)
    unexplained = unexplained.assign(reason=reasons,
                                     move_pct=(unexplained["close"] / unexplained["prev_close"] - 1) * 100)
    counts = unexplained["reason"].value_counts()
    section.lines.append(f"行情有標示 {len(flagged):,} 天：有官方事件 {len(flagged) - len(unexplained):,}、"
                         + "、".join(f"{reason} {counts.get(reason, 0):,}"
                                     for reason in (NO_TRADE, FIRST, TRANSFER, RESUMED, UNEXPLAINED))
                         + "（沒有官方事件的日子不還原）")
    listed = unexplained[unexplained["reason"].isin([TRANSFER, RESUMED, UNEXPLAINED])]
    if len(listed):
        section.tables["flag_without_event"] = listed[["code", "date", "market", "flag", "reason", "prev_row_date",
                                                       "close", "prev_close", "move_pct"]].round({"move_pct": 2})

    differs = traded[traded["change"].notna() & traded["prev_close"].notna()]
    differs = differs[~np.isclose(differs["reference"], differs["prev_close"], rtol=0, atol=1e-6)
                      & (differs["reference"] > 0)]
    differs = differs[_not_in(differs, official_days)]
    derived = differs[differs["comparable"]]
    reset = differs[~differs["comparable"]]
    section.lines.append(f"沒有官方事件、推出的參考價卻不等於前一日收盤：前一個交易日有成交的 {len(derived):,} 天"
                         f"（以推出的參考價還原）；前一個交易日沒有成交的 {len(reset):,} 天（上櫃 "
                         f"{int((reset['market'] == 'otc').sum()):,}，櫃買中心調整了參考價，不是事件，不還原）")
    if len(derived):
        section.tables["derived_events"] = derived[["code", "date", "market", "prev_close", "reference", "close"]]
    return section


def compare_screener_events(events: pd.DataFrame, screener_events: pd.DataFrame, watchlist: pd.DataFrame,
                            quotes: pd.DataFrame) -> Section:
    """market.db 的官方事件與 screener.db（FinMind）的事件逐筆比對，只比兩邊都有日 K 的期間。"""
    section = Section("與 screener.db 的除權息、減資比對（觀察清單）")
    events, screener_events = _within(events, quotes), _within(screener_events, quotes)
    span = watchlist.groupby("code")["date"].agg(["min", "max"])

    def within(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame[frame["code"].isin(span.index)].join(span, on="code")
        return frame[(frame["date"] > frame["min"]) & (frame["date"] <= frame["max"])]

    ours, theirs = within(events), within(screener_events)
    merged = ours.merge(theirs, on=["code", "date", "kind"], how="outer", suffixes=("", "_s"), indicator=True)
    both = merged[merged["_merge"] == "both"]
    factor = (both["after_price"] / both["before_price"]).to_numpy(dtype=float)
    factor_s = (both["after_price_s"] / both["before_price_s"]).to_numpy(dtype=float)
    same = np.isclose(factor, factor_s, rtol=1e-4, atol=0)
    only_ours, only_theirs = merged[merged["_merge"] == "left_only"], merged[merged["_merge"] == "right_only"]
    section.lines.append(f"兩邊都有 {len(both):,} 筆，調整因子相同 {int(same.sum()):,}、不同 {int((~same).sum()):,}；"
                         f"只有 market.db {len(only_ours):,} 筆、只有 screener.db {len(only_theirs):,} 筆")
    rows = [{"code": r.code, "date": r.date, "kind": r.kind, "problem": problem,
             "market_db": f"{r.before_price} → {r.after_price}" if problem != "只有 screener.db" else "",
             "screener_db": f"{r.before_price_s} → {r.after_price_s}" if problem != "只有 market.db" else ""}
            for frame, problem in ((both[~same], "因子不同"), (only_ours, "只有 market.db"),
                                   (only_theirs, "只有 screener.db"))
            for r in frame.itertuples()]
    if rows:
        section.tables["screener_event_mismatch"] = pd.DataFrame(rows)
    return section


# --- 4. 報酬指數 -----------------------------------------------------------------

def check_return_index(daily: dict[str, float], monthly: dict[str, float], trading_days: Sequence[str]) -> Section:
    section = Section("發行量加權股價報酬指數")
    tse_days = [d for d in trading_days]
    missing = [d for d in tse_days if d not in daily]
    both = [d for d in daily if d in monthly]
    differ = [d for d in both if abs(daily[d] - monthly[d]) > 1e-6]
    only_monthly = [d for d in monthly if d not in daily and tse_days and tse_days[0] <= d <= tse_days[-1]]
    section.lines.append(f"每日值 {len(daily):,} 天，與月表（MFI94U）比對 {len(both):,} 天：相同 {len(both) - len(differ):,}、"
                         f"不同 {len(differ):,}；交易日沒有每日值 {len(missing):,} 天；月表有、每日沒有 {len(only_monthly):,} 天")
    rows = ([{"date": d, "problem": "與月表不同", "daily": daily[d], "monthly": monthly[d]} for d in differ]
            + [{"date": d, "problem": "沒有每日值", "daily": None, "monthly": monthly.get(d)} for d in missing]
            + [{"date": d, "problem": "只有月表", "daily": None, "monthly": monthly[d]} for d in only_monthly])
    if rows:
        section.tables["return_index_mismatch"] = pd.DataFrame(rows)
    return section


# --- 5. 異常值與中斷 ---------------------------------------------------------------

def check_anomalies(quotes: pd.DataFrame, trading_days: Sequence[str], events: pd.DataFrame) -> Section:
    """開高低收與成交量的異常；代號中斷（停止交易）又出現時，前後收盤相差超過 10% 卻沒有官方事件的，
    可能漏了減資等事件（價格沒有還原）。上櫃恢復交易的日子行情不會標示，所以以前後收盤比對。"""
    section = Section("異常值與中斷")
    traded = quotes[quotes["close"].notna()]
    open_outside = (traded["open"] > traded["high"]) | (traded["open"] < traded["low"])
    close_outside = (traded["close"] > traded["high"]) | (traded["close"] < traded["low"])
    zero_volume = traded["volume"] == 0
    partial = quotes[quotes["close"].isna() & quotes[["open", "high", "low"]].notna().any(axis=1)]
    section.lines.append(f"有成交 {len(traded):,} 筆：開盤不在高低之間 {int(open_outside.sum()):,}、收盤不在高低之間 "
                         f"{int(close_outside.sum()):,}、成交量為 0 {int(zero_volume.sum()):,}；沒有收盤卻有其他價格 {len(partial):,}")
    bad = pd.concat([traded[open_outside].assign(problem="開盤不在高低之間"),
                     traded[close_outside].assign(problem="收盤不在高低之間"),
                     traded[zero_volume].assign(problem="成交量為 0"), partial.assign(problem="只有部分價格")])
    if len(bad):
        section.tables["price_anomalies"] = bad[["code", "date", "market", "problem", "open", "high", "low", "close",
                                                 "volume"]]

    position = {d: i for i, d in enumerate(trading_days)}
    q = quotes[["code", "date"]].sort_values(["code", "date"]).copy()
    q["pos"] = q["date"].map(position)
    q["prev_pos"] = q.groupby("code")["pos"].shift(1)
    q["prev_date"] = q.groupby("code")["date"].shift(1)
    gaps = q[(q["pos"] - q["prev_pos"] - 1) >= GAP_DAYS]
    by_code = {code: group for code, group in traded[["code", "date", "close"]].groupby("code")}
    event_days = events.groupby("code")["date"].apply(list).to_dict() if len(events) else {}
    rows = []
    for r in gaps.itertuples():
        group = by_code.get(r.code)
        before = group[group["date"] <= r.prev_date].tail(1) if group is not None else None
        after = group[group["date"] >= r.date].head(1) if group is not None else None
        if before is None or before.empty or after.empty:
            continue
        resumed = after["date"].iloc[0]
        move = (after["close"].iloc[0] / before["close"].iloc[0] - 1) * 100
        rows.append({"code": r.code, "last_seen": r.prev_date, "back_on": r.date, "resumed": resumed,
                     "missing_days": int(r.pos - r.prev_pos - 1), "close_before": before["close"].iloc[0],
                     "close_after": after["close"].iloc[0], "move_pct": round(move, 2),
                     "official_event": any(r.prev_date < d <= resumed for d in event_days.get(r.code, ()))})
    table = pd.DataFrame(rows, columns=["code", "last_seen", "back_on", "resumed", "missing_days", "close_before",
                                        "close_after", "move_pct", "official_event"])
    big = table[table["move_pct"].abs() > float(PRICE_LIMIT) * 100 + 0.1]
    missing = big[~big["official_event"].astype(bool)]
    section.lines.append(f"代號連續 {GAP_DAYS} 個交易日以上不在行情表中又出現（停止交易，例：減資、面額變更）：{len(gaps):,} 次、"
                         f"{gaps['code'].nunique():,} 檔；有官方事件 {int(table['official_event'].sum()):,} 次；前後收盤相差超過 10% "
                         f"{len(big):,} 次，其中沒有官方事件 {len(missing):,} 次（可能漏了減資等事件，價格沒有還原）"
                         + ("：" + "、".join(f"{r.code} {r.last_seen}～{r.resumed} {r.move_pct:+.1f}%"
                                            for r in missing.itertuples()) if len(missing) else ""))
    if len(table):
        section.tables["gaps"] = table
    return section


# --- 6. 下市 ---------------------------------------------------------------------

def check_delistings(securities: pd.DataFrame, delistings: pd.DataFrame, trading_days: Sequence[str]) -> Section:
    """最後一個交易日與 FinMind 下市日是否一致：最後一個交易日要在下市日之前，下市後不再有行情。

    2026-11 實測：下市日（終止上市日）通常在最後一個交易日之後 5～9 個交易日（先停止買賣）；停止交易一段時間
    才下市的，相差可達 150 個交易日以上（例：1333 恩得利 2020-04-06 最後一次交易，2020-11-17 下市）。
    """
    section = Section("下市")
    days = list(trading_days)
    if not days:
        return section
    position = {d: i for i, d in enumerate(days)}
    rows = []
    with_date = securities[securities["delisted_on"].notna()]
    for r in with_date.itertuples():
        if r.delisted_on > days[-1]:
            problem, distance = "下市日在資料最後一天之後", None
        else:
            distance = int(np.searchsorted(days, r.delisted_on)) - position[r.last_date]   # 非交易日取之後第一個交易日
            problem = "" if r.last_date < r.delisted_on else "下市日之後仍有行情"
        rows.append({"code": r.code, "name": r.name, "last_date": r.last_date, "delisted_on": r.delisted_on,
                     "trading_days_between": distance, "problem": problem})
    recent = days[max(0, len(days) - 1 - GAP_DAYS)]
    gone = securities[securities["delisted_on"].isna() & (securities["last_date"] < recent)]
    rows += [{"code": r.code, "name": r.name, "last_date": r.last_date, "delisted_on": None,
              "trading_days_between": None, "problem": "不再出現，但沒有下市紀錄"} for r in gone.itertuples()]
    in_range = delistings[(delistings["date"] >= days[0]) & (delistings["date"] <= days[-1])]
    never = in_range[~in_range["code"].isin(securities["code"])]
    rows += [{"code": r.code, "name": r.name, "last_date": None, "delisted_on": r.date,
              "trading_days_between": None, "problem": "期間內下市，行情中沒有"} for r in never.itertuples()]
    table = pd.DataFrame(rows, columns=["code", "name", "last_date", "delisted_on", "trading_days_between", "problem"])
    normal = table[(table["problem"] == "") & table["trading_days_between"].notna()]
    between = normal["trading_days_between"].astype(int)
    after = int((table["problem"] == "下市日之後仍有行情").sum())
    section.lines.append(
        f"有下市紀錄 {len(with_date):,} 檔：最後一個交易日在下市日之前 {len(normal):,} 檔（相差 10 個交易日以內 "
        f"{int((between <= 10).sum()):,}、超過 10 個交易日 {int((between > 10).sum()):,}，為停止交易一段時間後才下市），"
        f"下市日之後仍有行情 {after:,} 檔；不再出現卻沒有下市紀錄 {len(gone):,} 檔"
        + (f"（{'、'.join(f'{r.code} {r.name} {r.last_date}' for r in gone.itertuples())}）" if len(gone) else "")
        + f"；期間內下市、行情中卻沒有的 {len(never):,} 檔")
    if len(table):
        section.tables["delistings"] = table
    return section


# --- 7. 轉上市與新掛牌 -------------------------------------------------------------

def check_listings(quotes: pd.DataFrame, first_day: str) -> Section:
    """轉換市場（多為上櫃轉上市）與新掛牌後前五個交易日的最大漲跌幅（相對前一個收盤）。

    新掛牌前五日沒有漲跌幅限制（D38）；轉上市照接續計算（不重新排除），超過 10% 的列出供檢查。
    """
    section = Section("轉上市與新掛牌")
    q = quotes.sort_values(["code", "date"]).copy()
    q["prev_market"] = q.groupby("code")["market"].shift(1)
    switches = q[q["prev_market"].notna() & (q["market"] != q["prev_market"])][["code", "date", "prev_market",
                                                                               "market"]]
    firsts = q.groupby("code").head(1)
    new = firsts[firsts["date"] > first_day][["code", "date", "market"]]
    traded = q[q["close"].notna()].copy()
    traded["prev_close"] = traded.groupby("code")["close"].shift(1)
    traded["move"] = np.maximum(traded["high"] / traded["prev_close"] - 1, 1 - traded["low"] / traded["prev_close"])
    by_code = {code: group for code, group in traded.groupby("code")}
    rows = []
    for kind, events in (("轉換市場", switches), ("新掛牌", new)):
        for r in events.itertuples():
            group = by_code.get(r.code)
            if group is None:
                continue
            after = group[group["date"] >= r.date].head(FIRST_DAYS)
            moves = after["move"].dropna()
            rows.append({"code": r.code, "date": r.date, "kind": kind,
                         "from": getattr(r, "prev_market", ""), "to": r.market,
                         "max_move_pct": round(float(moves.max()) * 100, 2) if len(moves) else None,
                         "days_over_limit": int((moves > float(PRICE_LIMIT) + 1e-3).sum())})
    table = pd.DataFrame(rows, columns=["code", "date", "kind", "from", "to", "max_move_pct", "days_over_limit"])
    switched = table[table["kind"] == "轉換市場"]
    over = switched[switched["days_over_limit"] > 0]
    to_tse = int(((switched["from"] == "otc") & (switched["to"] == "tse")).sum())
    section.lines.append(f"轉換市場 {len(switched):,} 次（上櫃轉上市 {to_tse:,}），"
                         f"轉換後前 {FIRST_DAYS} 個交易日漲跌幅超過 10% 的 {len(over):,} 次"
                         + (f"：{'、'.join(over['code'].head(20))}" if len(over) else "（轉上市照接續計算，不重新排除）"))
    listed = table[table["kind"] == "新掛牌"]
    section.lines.append(f"新掛牌 {len(listed):,} 檔，前 {FIRST_DAYS} 個交易日有超過 10% 的 "
                         f"{int((listed['days_over_limit'] > 0).sum()):,} 檔（沒有漲跌幅限制，不產生訊號，D38）")
    if len(table):
        section.tables["listings"] = table
    return section


# --- 8. 證券分類 -----------------------------------------------------------------

def check_classification(securities: pd.DataFrame) -> Section:
    section = Section("證券分類（代號 4 碼數字，排除 00、91 開頭）")
    counts = securities["market"].value_counts()
    section.lines.append(f"普通股 {len(securities):,} 檔：上市 {counts.get('tse', 0):,}、上櫃 {counts.get('otc', 0):,}")
    odd = securities[securities["name"].apply(lambda name: any(token in name for token in SUSPICIOUS_NAMES))]
    listed = "、".join(f"{r.code} {r.name}" for r in odd.head(20).itertuples())
    section.lines.append(f"名稱像特殊證券（含「{'」「'.join(SUSPICIOUS_NAMES)}」）的 {len(odd):,} 檔"
                         + (f"：{listed}" if len(odd) else ""))
    if len(odd):
        section.tables["suspicious_names"] = odd[["code", "name", "market", "first_date", "last_date"]]
    return section
