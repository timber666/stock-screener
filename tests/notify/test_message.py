"""推播訊息格式（PROJECT_PLAN.md 第 11 節）。"""

from datetime import date

from conftest import make_bars, make_context
from screener.models import Hit, Market, PriceSubstitute, ScanReport, Side, Signal, StockInfo, Trend
from screener.notify.message import format_report

INDUSTRIES = {"PCB": ("銅箔基板", "PCB製造"), "記憶體": (), "半導體": ("製造", "封測")}
DAY = date(2026, 10, 30)


def hit(code: str, group: str, subcategory: str | None, *rule_names: str, close: float = 123.5,
        change: float | None = 3.2, trend: Trend = Trend.BULL, themes: tuple[str, ...] = (),
        side: Side = Side.LONG, substitute: PriceSubstitute | None = None) -> Hit:
    info = StockInfo(code=code, name=f"股{code}", market=Market.TSE, group=group, subcategory=subcategory,
                     themes=themes)
    ctx = make_context(make_bars([100.0, close]), trend=trend, info=info)
    signals = tuple(Signal("id", name, "detail", side) for name in rule_names)
    return Hit(ctx=ctx, side=side, signals=signals, change_pct=change, price_substitute=substitute)


def report(*hits: Hit, not_evaluated: tuple[str, ...] = (), is_replay: bool = True,
           mismatch: tuple[str, ...] = ()) -> ScanReport:
    return ScanReport(day=DAY, is_replay=is_replay, observed=45, hits=hits, not_evaluated=not_evaluated,
                      scan_time=None if is_replay else "13:10", prev_close_mismatch=mismatch)


def test_hits_are_grouped_by_industry_and_sorted_upstream_first() -> None:
    text = format_report(report(
        hit("2330", "半導體", "製造", "三線多排", trend=Trend.UNKNOWN),
        hit("3037", "PCB", "PCB製造", "多頭突破五日線紅K"),
        hit("2383", "PCB", "銅箔基板", "多頭突破五日線紅K", "三線多排", themes=("AI伺服器", "CPO")),
        hit("2408", "記憶體", None, "盤整長紅爆量過前高", close=50.0, change=-1.25, trend=Trend.RANGE),
        hit("1000", "PCB", "PCB製造", "三線多排", change=0.04),
    ), INDUSTRIES)

    assert text == "\n".join([
        "【回放】2026-10-30（以收盤 K 棒計算）",
        "",
        "━━ 多方 ━━",
        "▍PCB",
        "2383 股2383  123.5  ▲3.2%｜銅箔基板｜多頭｜#AI伺服器 #CPO",
        "  └ 多頭突破五日線紅K、三線多排",
        "1000 股1000  123.5  0.0%｜PCB製造｜多頭",
        "  └ 三線多排",
        "3037 股3037  123.5  ▲3.2%｜PCB製造｜多頭",
        "  └ 多頭突破五日線紅K",
        "",
        "▍記憶體",
        "2408 股2408  50  ▼1.2%｜盤整",
        "  └ 盤整長紅爆量過前高",
        "",
        "▍半導體",
        "2330 股2330  123.5  ▲3.2%｜製造｜資料不足",
        "  └ 三線多排",
        "",
        "觀察 45 檔｜觸發 5 檔（多方 5、空方 0）",
    ])


def test_long_and_short_hits_are_listed_in_separate_sections() -> None:
    text = format_report(report(
        hit("2330", "半導體", "製造", "空頭跌破五日線黑K", close=2370.0, change=-1.86, trend=Trend.BEAR,
            side=Side.SHORT),
        hit("2383", "PCB", "銅箔基板", "三線多排"),
        hit("3037", "PCB", "PCB製造", "三線空排", trend=Trend.RANGE, side=Side.SHORT),
    ), INDUSTRIES)

    assert text.splitlines()[2:] == [
        "━━ 多方 ━━",
        "▍PCB",
        "2383 股2383  123.5  ▲3.2%｜銅箔基板｜多頭",
        "  └ 三線多排",
        "",
        "━━ 空方 ━━",
        "▍PCB",
        "3037 股3037  123.5  ▲3.2%｜PCB製造｜盤整",
        "  └ 三線空排",
        "",
        "▍半導體",
        "2330 股2330  2370  ▼1.9%｜製造｜空頭",
        "  └ 空頭跌破五日線黑K",
        "",
        "觀察 45 檔｜觸發 3 檔（多方 1、空方 2）",
    ]


def test_stock_in_both_sections_is_counted_once() -> None:
    both = report(hit("2330", "半導體", "製造", "甲"), hit("2330", "半導體", "製造", "乙", side=Side.SHORT))

    assert format_report(both, INDUSTRIES).endswith("觀察 45 檔｜觸發 1 檔（多方 1、空方 1）")


def test_no_hits_still_produces_a_message() -> None:
    assert format_report(report(), INDUSTRIES).splitlines()[2:] == [
        "今日無符合標的", "", "觀察 45 檔｜觸發 0 檔（多方 0、空方 0）",
    ]


def test_not_evaluated_stocks_are_listed_at_the_bottom() -> None:
    replay = format_report(report(not_evaluated=("1111", "2222")), INDUSTRIES)
    intraday = format_report(report(not_evaluated=("1111",), is_replay=False), INDUSTRIES)

    assert replay.endswith("⚠️ 2 檔當日沒有日 K（無成交或資料未更新），未評估：1111、2222")
    assert intraday.endswith("⚠️ 1 檔今日無成交或無報價，未評估：1111")


def test_intraday_header_marks_tentative_bars() -> None:
    text = format_report(report(is_replay=False), INDUSTRIES)

    assert text.startswith("【盤中選股】2026-10-30 13:10（暫定 K 棒，收盤前可能變動）")


def test_intraday_warnings_are_listed_at_the_bottom() -> None:
    text = format_report(report(hit("2330", "半導體", "製造", "三線多排"), is_replay=False,
                                mismatch=("2330", "3037"), not_evaluated=("1111",)), INDUSTRIES)

    assert text.splitlines()[-3:] == [
        "觀察 45 檔｜觸發 1 檔（多方 1、空方 0）",
        "⚠️ 昨收不一致（除權息或資料未更新）：2330、3037",
        "⚠️ 1 檔今日無成交或無報價，未評估：1111",
    ]


def test_substituted_price_is_marked_on_the_hit() -> None:
    text = format_report(report(
        hit("2330", "半導體", "製造", "三線多排", themes=("CPO",), substitute=PriceSubstitute.BEST_BID),
        hit("2308", "半導體", "製造", "空頭跌破五日線黑K", close=1335.0, change=-9.8, trend=Trend.BEAR,
            side=Side.SHORT, substitute=PriceSubstitute.BEST_ASK),
        is_replay=False), INDUSTRIES).splitlines()

    assert "2330 股2330  123.5  ▲3.2%｜製造｜多頭｜#CPO｜⚠️ 價格為替代值（最佳買價）" in text
    assert "2308 股2308  1335  ▼9.8%｜製造｜空頭｜⚠️ 價格為替代值（最佳賣價）" in text
