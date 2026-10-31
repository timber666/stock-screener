"""組成推播訊息（PROJECT_PLAN.md 第 11 節）。"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from screener.formatting import format_number
from screener.models import Hit, ScanReport, Side

_ELLIPSIS = "…"


def format_report(report: ScanReport, industries: Mapping[str, tuple[str, ...]]) -> str:
    """完整的訊息，不拆分。

    先列多方再列空方，各自依族群分組；族群依 industries 的順序，族群內依子分類順序、再依代號排列。
    沒有觸發的方向不列出；兩個方向都沒有觸發時，顯示「今日無符合標的」。底部彙總昨收不一致與未評估的股票。
    """
    return "\n".join(line for section in _sections(report, industries) for line in section.lines)


def format_messages(report: ScanReport, industries: Mapping[str, tuple[str, ...]], limit: int) -> list[str]:
    """與 format_report 內容相同，但拆成每則不超過 limit 字的多則訊息。

    依族群拆分：一個族群盡量放在同一則；單一族群就超過上限時，才在族群內逐檔拆開。
    從方向或族群的中間開始的一則，開頭補上「━━ 空方 ━━（續）」「▍PCB（續）」。
    """
    return _pack(_sections(report, industries), limit)


def format_failure(title: str, detail: str, log_file: Path | None, limit: int) -> str:
    """錯誤通知（PROJECT_PLAN.md 7.2）。detail 太長時截斷，讓整則不超過 limit 字。"""
    head = f"❌ {title}"
    foot = f"詳細內容請看 log：{log_file}" if log_file is not None else ""
    room = limit - text_length("\n".join(filter(None, [head, "", foot]))) - 1
    if text_length(detail) > room:
        detail = _take(detail, room - text_length(_ELLIPSIS)) + _ELLIPSIS
    return "\n".join(filter(None, [head, detail, foot]))


def text_length(text: str) -> int:
    """Discord 計算的字數（UTF-16 code unit）。"""
    return len(text.encode("utf-16-le")) // 2


# --- 區塊 -------------------------------------------------------------------

@dataclass(frozen=True)
class _Section:
    """訊息中的一個區塊（標題、一個族群、底部彙總）。拆成多則時，盡量整個區塊放在同一則。"""

    head: tuple[str, ...]
    items: tuple[tuple[str, ...], ...] = ()     # 整個區塊放不進一則時，可以在這些單位之間拆開
    tail: tuple[str, ...] = ()
    resume: tuple[str, ...] = ()                # 區塊從新的一則開始時，開頭補上的標題
    resume_inside: tuple[str, ...] = ()         # 區塊在中間拆開時，新的一則開頭補上的標題

    @property
    def lines(self) -> list[str]:
        return [*self.head, *(line for item in self.items for line in item), *self.tail]


def _sections(report: ScanReport, industries: Mapping[str, tuple[str, ...]]) -> list[_Section]:
    sections = [_Section(head=(_header(report), ""))]
    for side in Side:
        side_hits = tuple(hit for hit in report.hits if hit.side is side)
        side_line = f"━━ {side.label} ━━"
        for i, (group, hits) in enumerate(_group_hits(side_hits, industries)):
            group_line = f"▍{group}"
            sections.append(_Section(
                head=(side_line, group_line) if i == 0 else (group_line,),
                items=tuple(tuple(_hit_lines(hit)) for hit in hits),
                tail=("",),
                resume=() if i == 0 else (f"{side_line}（續）",),
                resume_inside=(f"{side_line}（續）", f"{group_line}（續）"),
            ))
    if not report.hits:
        sections.append(_Section(head=("今日無符合標的", "")))
    sections.append(_Section(head=(), items=tuple((line,) for line in _footer(report))))
    return sections


def _header(report: ScanReport) -> str:
    if report.is_replay:
        return f"【回放】{report.day}（以收盤 K 棒計算）"
    return f"【盤中選股】{report.day} {report.scan_time}（暫定 K 棒，收盤前可能變動）"


def _footer(report: ScanReport) -> list[str]:
    counts = "、".join(f"{side.label} {sum(hit.side is side for hit in report.hits)}" for side in Side)
    triggered = len({hit.ctx.info.code for hit in report.hits})
    lines = [f"觀察 {report.observed} 檔｜觸發 {triggered} 檔（{counts}）"]
    if report.prev_close_mismatch:
        lines.append(f"⚠️ 昨收不一致（除權息或資料未更新）：{'、'.join(report.prev_close_mismatch)}")
    if report.not_evaluated:
        reason = "當日沒有日 K（無成交或資料未更新）" if report.is_replay else "今日無成交或無報價"
        lines.append(f"⚠️ {len(report.not_evaluated)} 檔{reason}，未評估：{'、'.join(report.not_evaluated)}")
    return lines


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


# --- 拆成多則 ---------------------------------------------------------------

def _pack(sections: Sequence[_Section], limit: int) -> list[str]:
    messages: list[list[str]] = [[]]

    def fits(lines: Sequence[str]) -> bool:
        return text_length("\n".join([*messages[-1], *lines])) <= limit

    for section in sections:
        lines = section.lines
        if fits(lines):
            messages[-1] += lines
            continue
        if text_length("\n".join([*section.resume, *lines])) <= limit:
            messages.append([*section.resume, *lines])
            continue

        # 單一區塊就超過上限：在 items 之間拆開。標題至少帶著第一個單位，避免標題單獨留在上一則
        if not fits([*section.head, *(section.items[0] if section.items else ())]):
            messages.append(list(section.resume))
        messages[-1] += section.head
        for item in section.items:
            if not fits(item):
                messages.append(list(section.resume_inside))
            if fits(item):
                messages[-1] += item
                continue
            # 單一單位就超過上限（例：未評估的代號非常多）：直接切開
            for line in item:
                for piece in _pieces(line, limit - text_length("\n".join(section.resume_inside)) - 1):
                    if not fits([piece]):
                        messages.append(list(section.resume_inside))
                    messages[-1].append(piece)
        messages[-1] += section.tail
    return ["\n".join(_strip_blank(message)) for message in messages if _strip_blank(message)]


def _strip_blank(lines: list[str]) -> list[str]:
    """去掉結尾的空行。"""
    end = len(lines)
    while end and not lines[end - 1]:
        end -= 1
    return lines[:end]


def _pieces(text: str, width: int) -> Iterator[str]:
    if width < 2:                               # 一個字最多佔 2 個 UTF-16 code unit
        raise ValueError(f"width 太小：{width}")
    while text:
        piece = _take(text, width)
        yield piece
        text = text[len(piece):]


def _take(text: str, width: int) -> str:
    """text 開頭不超過 width 字（UTF-16）的部分。"""
    taken = 0
    for i, char in enumerate(text):
        taken += text_length(char)
        if taken > width:
            return text[:i]
    return text
