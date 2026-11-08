"""全市場資料的品質檢查（RESEARCH_PLAN.md 4.3「品質報告」）。"""

import io
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.test_backtest_job import CONFIG, FLAT, RISES
from research.test_market_backtest import RESEARCH, watchlist_config, write_market
from screener.jobs import market_check
from screener.research.market import check

COLUMNS = ["code", "date", "market", "name", "open", "high", "low", "close", "volume", "change", "flag"]
DAYS = ["2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06"]


def row(code: str, day: str, close: float | None, change: float | None = 0.0, flag: str | None = None,
        market: str = "tse", high: float | None = None, low: float | None = None) -> dict:
    return {"code": code, "date": day, "market": market, "name": f"股{code}", "open": close,
            "high": high if high is not None else close, "low": low if low is not None else close, "close": close,
            "volume": 1000 if close else 0, "change": np.nan if change is None else change, "flag": flag}


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def bar(code: str, day: str, close: float, volume: int, high: float | None = None) -> dict:
    """screener.db 的一根日 K（成交量為張）。"""
    return {"code": code, "date": day, "open": close, "high": high or close, "low": close, "close": close,
            "volume": volume}


def event(code: str, day: str, before: float, after: float, kind: str = "dividend", market: str = "tse") -> dict:
    return {"code": code, "date": day, "kind": kind, "market": market, "before_price": before, "after_price": after,
            "source": market}


def test_events_are_matched_with_flags_and_derived_references() -> None:
    quotes = frame([
        row("1111", DAYS[0], 100), row("1111", DAYS[1], 95, None, "X"),             # 標 X，有官方事件
        row("2222", DAYS[0], 50), row("2222", DAYS[1], 47.0, -1.0),                  # 推出 48，官方 48
        row("3333", DAYS[0], 50), row("3333", DAYS[1], 47.0, -1.0),                  # 推出 48，官方 48.05：差一檔
        row("4444", DAYS[0], 50), row("4444", DAYS[1], None, None), row("4444", DAYS[2], 51, None, "X"),  # 前一天沒有成交
        row("5555", DAYS[2], 30, None, "X"),                                         # 第一次出現
        row("6666", DAYS[0], 20), row("6666", DAYS[1], 21, None, "X"),               # 無法解釋
        row("7777", DAYS[0], 20), row("7777", DAYS[1], 18.5, -0.5),                  # 推出 19 ≠ 20，沒有官方事件
        row("9960", DAYS[0], 26.55, market="otc"), row("9960", DAYS[1], None, None, market="otc"),
        row("9960", DAYS[2], 26.25, -0.1, market="otc"),                             # 前一天沒有成交：參考價調整
        row("6438", DAYS[0], 88.8, market="otc"), row("6438", DAYS[1], 87, None, "X"),   # 轉上市的第一天
        row("8101", DAYS[0], 1.9), row("8101", DAYS[3], 10.45, None, "X"),           # 停止交易後恢復，+450%
    ])
    events = pd.DataFrame([event("1111", DAYS[1], 100, 96), event("2222", DAYS[1], 50, 48),
                           event("3333", DAYS[1], 50, 48.05), event("8888", "2026-01-30", 10, 9)])   # 期間外不比

    section = check.check_events(quotes, events)

    text = "\n".join(section.lines)
    assert "上市除權息 3 筆：行情有標示 1；沒有標示、推出的參考價與官方相同 1、差一檔以內 1、差更多 0" in text
    assert "期間內的官方事件 3 筆" in text
    flagged = section.tables["flag_without_event"].set_index("code")["reason"]
    assert flagged.to_dict() == {"6438": "轉換市場的第一天", "6666": "無法解釋", "8101": "停止交易後恢復"}
    assert "有官方事件 1、前一天沒有成交 1、第一次出現 1、轉換市場的第一天 1、停止交易後恢復 1、無法解釋 1" in text
    assert list(section.tables["flag_without_event"].set_index("code")["move_pct"].round(1)[["8101"]]) == [450.0]
    assert list(section.tables["derived_events"]["code"]) == ["7777"]                # 有官方事件的日子不另外計入
    assert "前一個交易日有成交的 1 天（以推出的參考價還原）；前一個交易日沒有成交的 1 天（上櫃 1" in text


def test_price_jump_across_a_halt_without_event_is_listed() -> None:
    days = [f"2026-01-{d:02d}" for d in range(5, 31)]
    quotes = frame([row("8101", days[0], 1.9), row("8101", days[10], 10.45, None, "X"),     # 停止交易 9 天後 +450%
                    row("1414", days[0], 17.45), row("1414", days[10], 18.0, 0.0),          # 有官方減資
                    *[row("2330", d, 100) for d in days[:11]]])
    events = pd.DataFrame([event("1414", days[8], 17.45, 18.27, kind="capital_reduction")])

    section = check.check_anomalies(quotes, days[:11], events)

    gaps = section.tables["gaps"].set_index("code")
    assert gaps.loc["8101", "missing_days"] == 9 and not gaps.loc["8101", "official_event"]
    assert gaps.loc["1414", "official_event"]
    assert "前後收盤相差超過 10% 1 次，其中沒有官方事件 1 次" in section.lines[1]
    assert "8101 2026-01-05～2026-01-15 +450.0%" in section.lines[1]


def test_watchlist_comparison() -> None:
    quotes = frame([row("1111", DAYS[0], 100), row("1111", DAYS[1], 101),
                    row("2222", DAYS[0], 50, market="otc"), row("2222", DAYS[1], 51, market="otc")])
    quotes.loc[2, "volume"] = 990_000                                               # 上櫃少 1%（R10）
    watch = pd.DataFrame([bar("1111", DAYS[0], 100, 1), bar("1111", DAYS[1], 101, 1, high=102),
                          bar("2222", DAYS[0], 50, 1000), bar("3333", DAYS[0], 9, 1)])
    listings = pd.DataFrame([{"code": "3333", "listed_on": DAYS[1]}])                # 3333 02-02 為興櫃

    section = check.compare_watchlist(quotes, watch, listings, DAYS[:2])

    text = "\n".join(section.lines)
    assert "上市：比對 2 筆，開高低收相同 1（50.0%），成交量相同 2（100.0%）" in text
    assert "上櫃：比對 1 筆，開高低收相同 1（100.0%），成交量相同 0（0.0%）" in text and "中位數 -1.00%" in text
    assert "掛牌前（興櫃，D38）1 筆、其他 0 筆" in text
    assert "market.db 有成交、screener.db 沒有的 0 筆" in text      # 2222 02-03 在 screener.db 的日 K 期間之外，不比
    assert set(section.tables["watchlist_mismatch"]["problem"]) == {"開高低收不同"}


def test_transfers_and_new_listings() -> None:
    quotes = frame([row("1111", DAYS[0], 100, market="otc"), row("1111", DAYS[1], 115, high=115, market="tse"),
                    row("2222", DAYS[0], 50), row("2222", DAYS[1], 52),
                    row("3333", DAYS[1], 10), row("3333", DAYS[2], 14, high=14)])

    section = check.check_listings(quotes, DAYS[0])

    table = section.tables["listings"].set_index("code")
    assert (table.loc["1111", "kind"], table.loc["1111", "from"], table.loc["1111", "to"]) == ("轉換市場", "otc", "tse")
    assert table.loc["1111", "max_move_pct"] == 15.0 and table.loc["1111", "days_over_limit"] == 1
    assert table.loc["3333", "kind"] == "新掛牌" and table.loc["3333", "max_move_pct"] == 40.0
    assert "2222" not in table.index
    assert "轉換後前 5 個交易日漲跌幅超過 10% 的 1 次：1111" in section.lines[0]


def test_delistings_and_return_index() -> None:
    securities = pd.DataFrame([
        {"code": "1111", "name": "甲", "market": "tse", "first_date": DAYS[0], "last_date": DAYS[1],
         "delisted_on": DAYS[2]},
        {"code": "2222", "name": "乙", "market": "tse", "first_date": DAYS[0], "last_date": DAYS[0],
         "delisted_on": DAYS[4]},
        {"code": "3333", "name": "丙", "market": "tse", "first_date": DAYS[0], "last_date": DAYS[4],
         "delisted_on": None},
    ])
    delistings = pd.DataFrame([{"code": "1111", "date": DAYS[2], "name": "甲"},
                               {"code": "2222", "date": DAYS[4], "name": "乙"},
                               {"code": "9999", "date": DAYS[3], "name": "己"}])

    section = check.check_delistings(securities, delistings, DAYS)

    table = section.tables["delistings"].set_index("code")
    assert table["problem"].to_dict() == {"1111": "", "2222": "", "9999": "期間內下市，行情中沒有"}
    assert table.loc["2222", "trading_days_between"] == 4         # 停止交易一段時間後才下市
    assert "最後一個交易日在下市日之前 2 檔（相差 10 個交易日以內 2" in section.lines[0]

    index = check.check_return_index({DAYS[0]: 100.0, DAYS[1]: 101.0}, {DAYS[0]: 100.0, DAYS[1]: 101.5}, DAYS[:3])
    assert "相同 1、不同 1；交易日沒有每日值 1 天" in index.lines[0]


def test_market_check_job_prints_report_and_writes_details(tmp_path: Path) -> None:
    bars = {"1111": RISES, "2222": FLAT}
    write_market(tmp_path / "market.db", bars, {"1111": 2_000_000, "2222": 1_000_000})
    config = watchlist_config(tmp_path, bars)
    research = replace(RESEARCH, market=replace(RESEARCH.market, db_path=tmp_path / "market.db",
                                                check_dir=tmp_path / "check"))
    (tmp_path / "check").mkdir()
    (tmp_path / "check" / "stale.csv").write_text("old", encoding="utf-8")       # 上一次的明細會先清掉
    out = io.StringIO()

    market_check.run(config, research, out=out)

    text = out.getvalue()
    assert text.startswith(f"全市場資料品質報告（market.db：{RISES.index[0]}～{RISES.index[-1]}）")
    assert "上市：比對 46 筆，開高低收相同 46（100.0%），成交量相同 46（100.0%）" in text
    assert "■ 發行量加權股價報酬指數" in text and "■ 轉上市與新掛牌" in text
    assert not (tmp_path / "check" / "stale.csv").exists()
    assert CONFIG.settings.db_path != config.settings.db_path                    # 讀的是暫存的 screener.db
