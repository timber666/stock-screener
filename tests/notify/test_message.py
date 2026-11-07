"""推播訊息格式（PROJECT_PLAN.md 第 11 節）。"""

from datetime import date
from pathlib import Path

from conftest import make_bars, make_context
from screener.models import Hit, Market, PriceSubstitute, ScanReport, Side, Signal, StockInfo, Trend
from screener.notify.message import format_failure, format_messages, format_report, text_length

INDUSTRIES = {"PCB": ("銅箔基板", "PCB製造"), "記憶體": (), "半導體": ("製造", "封測")}
DAY = date(2026, 10, 30)


def hit(code: str, group: str, subcategory: str | None, *rule_names: str, close: float = 123.5,
        change: float | None = 3.2, trend: Trend = Trend.BULL, side: Side = Side.LONG,
        substitute: PriceSubstitute | None = None, entry: bool = False) -> Hit:
    info = StockInfo(code=code, name=f"股{code}", market=Market.TSE, group=group, subcategory=subcategory)
    ctx = make_context(make_bars([100.0, close]), trend=trend, info=info)
    signals = tuple(Signal("id", name, "detail", side) for name in rule_names)
    return Hit(ctx=ctx, side=side, signals=signals, change_pct=change, price_substitute=substitute, entry=entry)


def report(*hits: Hit, not_evaluated: tuple[str, ...] = (), is_replay: bool = True,
           mismatch: tuple[str, ...] = ()) -> ScanReport:
    return ScanReport(day=DAY, is_replay=is_replay, observed=45, hits=hits, not_evaluated=not_evaluated,
                      scan_time=None if is_replay else "13:10", prev_close_mismatch=mismatch)


def test_entry_is_marked_on_the_stock_line_only_when_it_matches() -> None:
    text = format_report(report(
        hit("3037", "PCB", "PCB製造", "多頭突破五日線紅K", entry=True),
        hit("1000", "PCB", "PCB製造", "三線多排"),
    ), INDUSTRIES)

    assert "3037 股3037  123.5  ▲3.2%｜PCB製造｜多頭｜✅ 符合進場\n  └ 多頭突破五日線紅K" in text
    assert "1000 股1000  123.5  ▲3.2%｜PCB製造｜多頭\n  └ 三線多排" in text


def test_entry_mark_comes_before_the_substitute_warning() -> None:
    text = format_report(report(
        hit("3037", "PCB", "PCB製造", "多頭突破五日線紅K", entry=True, substitute=PriceSubstitute.BEST_BID),
    ), INDUSTRIES)

    assert "｜多頭｜✅ 符合進場｜⚠️ 價格為替代值（最佳買價）" in text


def test_hits_are_grouped_by_industry_and_sorted_upstream_first() -> None:
    text = format_report(report(
        hit("2330", "半導體", "製造", "三線多排", trend=Trend.UNKNOWN),
        hit("3037", "PCB", "PCB製造", "多頭突破五日線紅K"),
        hit("2383", "PCB", "銅箔基板", "多頭突破五日線紅K", "三線多排"),
        hit("2408", "記憶體", None, "盤整長紅爆量過前高", close=50.0, change=-1.25, trend=Trend.RANGE),
        hit("1000", "PCB", "PCB製造", "三線多排", change=0.04),
    ), INDUSTRIES)

    assert text == "\n".join([
        "【回放】2026-10-30（以收盤 K 棒計算）",
        "",
        "━━ 多方 ━━",
        "▍PCB",
        "2383 股2383  123.5  ▲3.2%｜銅箔基板｜多頭",
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
        hit("2330", "半導體", "製造", "三線多排", substitute=PriceSubstitute.BEST_BID),
        hit("2308", "半導體", "製造", "空頭跌破五日線黑K", close=1335.0, change=-9.8, trend=Trend.BEAR,
            side=Side.SHORT, substitute=PriceSubstitute.BEST_ASK),
        is_replay=False), INDUSTRIES).splitlines()

    assert "2330 股2330  123.5  ▲3.2%｜製造｜多頭｜⚠️ 價格為替代值（最佳買價）" in text
    assert "2308 股2308  1335  ▼9.8%｜製造｜空頭｜⚠️ 價格為替代值（最佳賣價）" in text


# --- 拆成多則（Discord 單則上限）----------------------------------------------

def pcb(code: str, side: Side = Side.LONG) -> Hit:
    return hit(code, "PCB", "銅箔基板", "三線多排", side=side)


def mem(code: str, side: Side = Side.LONG) -> Hit:
    return hit(code, "記憶體", None, "三線多排", side=side)


def lines_of(*hits: Hit) -> list[str]:
    """這幾檔在訊息中的行（每檔兩行）。"""
    return [line for h in hits for line in format_report(report(h), INDUSTRIES).splitlines()[4:6]]


TITLE = ["【回放】2026-10-30（以收盤 K 棒計算）", ""]


def test_short_report_is_a_single_message() -> None:
    r = report(pcb("1001"), mem("2001"))

    assert format_messages(r, INDUSTRIES, 2000) == [format_report(r, INDUSTRIES)]


def test_messages_are_split_between_groups() -> None:
    r = report(pcb("1001"), pcb("1002"), mem("2001"))
    first = "\n".join([*TITLE, "━━ 多方 ━━", "▍PCB", *lines_of(pcb("1001"), pcb("1002"))])

    messages = format_messages(r, INDUSTRIES, text_length(first) + 5)       # 放不下下一個族群

    assert messages == [first, "\n".join([
        "━━ 多方 ━━（續）", "▍記憶體", *lines_of(mem("2001")), "", "觀察 45 檔｜觸發 3 檔（多方 3、空方 0）",
    ])]


def test_new_side_starts_without_continuation_marker() -> None:
    r = report(pcb("1001"), pcb("1002"), pcb("3001", Side.SHORT))
    first = "\n".join([*TITLE, "━━ 多方 ━━", "▍PCB", *lines_of(pcb("1001"), pcb("1002"))])

    messages = format_messages(r, INDUSTRIES, text_length(first) + 5)

    assert messages[0] == first
    assert messages[1].startswith("━━ 空方 ━━\n▍PCB\n3001 ")


def test_group_larger_than_limit_is_split_between_stocks() -> None:
    r = report(pcb("1001"), pcb("1002"), pcb("1003"))
    first = "\n".join([*TITLE, "━━ 多方 ━━", "▍PCB", *lines_of(pcb("1001"))])

    messages = format_messages(r, INDUSTRIES, text_length(first) + 5)

    assert messages[0] == first
    assert messages[1] == "\n".join(["━━ 多方 ━━（續）", "▍PCB（續）", *lines_of(pcb("1002"))])
    assert messages[2].startswith("\n".join(["━━ 多方 ━━（續）", "▍PCB（續）", *lines_of(pcb("1003"))]))


def test_every_message_fits_and_nothing_is_lost() -> None:
    hits = [pcb(f"1{i:03d}") for i in range(20)] + [mem(f"2{i:03d}") for i in range(15)]
    hits += [pcb(f"3{i:03d}", Side.SHORT) for i in range(25)]
    r = report(*hits, not_evaluated=tuple(f"9{i:03d}" for i in range(50)), mismatch=("1001",))

    messages = format_messages(r, INDUSTRIES, 300)

    assert len(messages) > 3 and all(text_length(m) <= 300 for m in messages)
    received = [line for m in messages for line in m.splitlines() if line and not line.endswith("（續）")]
    assert received == [line for line in format_report(r, INDUSTRIES).splitlines() if line]


def test_line_longer_than_limit_is_cut() -> None:
    r = report(not_evaluated=tuple(f"{i:04d}" for i in range(100)))
    footer = format_report(r, INDUSTRIES).splitlines()[-1]

    messages = format_messages(r, INDUSTRIES, 120)

    assert all(text_length(m) <= 120 for m in messages)
    assert footer in "".join(m.replace("\n", "") for m in messages)     # 切開的片段依序接起來就是原本那一行


def test_text_length_counts_utf16_units_like_discord() -> None:
    assert text_length("台積電") == 3
    assert text_length("⚠️") == 2              # U+26A0 + U+FE0F
    assert text_length("😀") == 2              # BMP 之外的字元佔 2 個


# --- 錯誤通知 ---------------------------------------------------------------

def test_failure_message() -> None:
    text = format_failure("盤中選股執行失敗（2026-11-02 13:10）", "盤中報價抓取失敗：HTTP 503",
                          Path("logs/screener-2026-11-02.log"), 2000)

    assert text.splitlines() == [
        "❌ 盤中選股執行失敗（2026-11-02 13:10）",
        "盤中報價抓取失敗：HTTP 503",
        f"詳細內容請看 log：{Path('logs/screener-2026-11-02.log')}",
    ]


def test_long_failure_detail_is_truncated() -> None:
    text = format_failure("盤後更新執行失敗", "錯" * 500, Path("logs/x.log"), 100)

    lines = text.splitlines()
    assert text_length(text) <= 100
    assert lines[1].endswith("…") and lines[2] == f"詳細內容請看 log：{Path('logs/x.log')}"
