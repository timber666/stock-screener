"""組成推播訊息（PROJECT_PLAN.md 第 11 節）。超過 Discord 2000 字時的拆分於 M5 隨推播實作。"""

from collections.abc import Mapping

from screener.formatting import format_number
from screener.models import Hit, ScanReport, Side


def format_report(report: ScanReport, industries: Mapping[str, tuple[str, ...]]) -> str:
    """先列多方再列空方，各自依族群分組；族群依 industries 的順序，族群內依子分類順序、再依代號排列。

    沒有觸發的方向不列出；兩個方向都沒有觸發時，顯示「今日無符合標的」。底部彙總昨收不一致與未評估的股票。
    """
    lines = [_header(report), ""]
    for side in Side:
        side_hits = tuple(hit for hit in report.hits if hit.side is side)
        if not side_hits:
            continue
        lines.append(f"━━ {side.label} ━━")
        for group, hits in _group_hits(side_hits, industries):
            lines.append(f"▍{group}")
            for hit in hits:
                lines += _hit_lines(hit)
            lines.append("")
    if not report.hits:
        lines += ["今日無符合標的", ""]

    counts = "、".join(f"{side.label} {sum(hit.side is side for hit in report.hits)}" for side in Side)
    triggered = len({hit.ctx.info.code for hit in report.hits})
    lines.append(f"觀察 {report.observed} 檔｜觸發 {triggered} 檔（{counts}）")
    if report.prev_close_mismatch:
        lines.append(f"⚠️ 昨收不一致（除權息或資料未更新）：{'、'.join(report.prev_close_mismatch)}")
    if report.not_evaluated:
        reason = "當日沒有日 K（無成交或資料未更新）" if report.is_replay else "今日無成交或無報價"
        lines.append(f"⚠️ {len(report.not_evaluated)} 檔{reason}，未評估：{'、'.join(report.not_evaluated)}")
    return "\n".join(lines)


def _header(report: ScanReport) -> str:
    if report.is_replay:
        return f"【回放】{report.day}（以收盤 K 棒計算）"
    return f"【盤中選股】{report.day} {report.scan_time}（暫定 K 棒，收盤前可能變動）"


def _group_hits(hits: tuple[Hit, ...], industries: Mapping[str, tuple[str, ...]]) -> list[tuple[str, list[Hit]]]:
    groups = list(industries)

    def sort_key(hit: Hit) -> tuple[int, int, str]:
        info = hit.ctx.info
        subcategories = industries[info.group]
        position = subcategories.index(info.subcategory) if info.subcategory else -1
        return groups.index(info.group), position, info.code

    ordered = sorted(hits, key=sort_key)
    return [(group, members) for group in groups
            if (members := [hit for hit in ordered if hit.ctx.info.group == group])]


def _hit_lines(hit: Hit) -> list[str]:
    info = hit.ctx.info
    quote = f"{info.code} {info.name}  {format_number(hit.ctx.today['close'])}"
    if hit.change_pct is not None:
        quote += f"  {_change(hit.change_pct)}"
    fields = [quote, info.subcategory, hit.ctx.trend.label, " ".join(f"#{t}" for t in info.themes),
              f"⚠️ 價格為替代值（{hit.price_substitute.label}）" if hit.price_substitute is not None else ""]
    return [
        "｜".join(field for field in fields if field),
        f"  └ {'、'.join(signal.rule_name for signal in hit.signals)}",
    ]


def _change(pct: float) -> str:
    rounded = round(pct, 1)
    if rounded > 0:
        return f"▲{rounded:.1f}%"
    if rounded < 0:
        return f"▼{-rounded:.1f}%"
    return "0.0%"
