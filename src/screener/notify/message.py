"""組成推播訊息（PROJECT_PLAN.md 第 11 節）。

Section 與 pack_sections 是通用的拆分方式，研究的定期報告（RESEARCH_PLAN.md 4.1）也使用。
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from screener.formatting import format_number
from screener.models import FactorSnapshot, Hit, ScanReport, Side, SnapshotRead, SnapshotStatus

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
    return pack_sections(_sections(report, industries), limit)


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


def format_for_terminal(messages: Sequence[str]) -> str:
    """--dry-run 印在終端的內容：多則時以「──── 第 1/2 則 ────」標示每一則的範圍，方便確認拆分結果。"""
    if len(messages) > 1:
        messages = [f"──── 第 {i}/{len(messages)} 則 ────\n{message}" for i, message in enumerate(messages, start=1)]
    return "\n\n".join(messages) + "\n"


# --- 區塊 -------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """訊息中的一個區塊（標題、一個族群、底部彙總）。拆成多則時，盡量整個區塊放在同一則。"""

    head: tuple[str, ...]
    items: tuple[tuple[str, ...], ...] = ()     # 整個區塊放不進一則時，可以在這些單位之間拆開
    tail: tuple[str, ...] = ()
    resume: tuple[str, ...] = ()                # 區塊從新的一則開始時，開頭補上的標題
    resume_inside: tuple[str, ...] = ()         # 區塊在中間拆開時，新的一則開頭補上的標題

    @property
    def lines(self) -> list[str]:
        return [*self.head, *(line for item in self.items for line in item), *self.tail]


def _sections(report: ScanReport, industries: Mapping[str, tuple[str, ...]]) -> list[Section]:
    snapshot = _usable(report.factor)
    sections = [Section(head=(_header(report), ""))]
    for side in Side:
        side_hits = tuple(hit for hit in report.hits if hit.side is side)
        side_line = f"━━ {side.label} ━━"
        for i, (group, hits) in enumerate(_group_hits(side_hits, industries)):
            group_line = f"▍{group}"
            sections.append(Section(
                head=(side_line, group_line) if i == 0 else (group_line,),
                items=tuple(tuple(_hit_lines(hit, snapshot)) for hit in hits),
                tail=("",),
                resume=() if i == 0 else (f"{side_line}（續）",),
                resume_inside=(f"{side_line}（續）", f"{group_line}（續）"),
            ))
    if not report.hits:
        sections.append(Section(head=("今日無符合標的", "")))
    sections.append(Section(head=(), items=tuple((line,) for line in _footer(report))))
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
    if (factor := _factor_footer(report)) is not None:
        lines.append(factor)
    return lines


def _usable(factor: SnapshotRead | None) -> FactorSnapshot | None:
    return factor.snapshot if factor is not None and factor.status is SnapshotStatus.OK else None


def _factor_footer(report: ScanReport) -> str | None:
    """營收年增的說明（D58）：有任何一檔顯示時才列；快照不能用時一律提醒，否則壞掉的快照沒有人會發現（R42）。"""
    factor = report.factor
    if factor is None:                          # 回放
        return None
    if factor.status is SnapshotStatus.OK and factor.snapshot is not None:
        snapshot = factor.snapshot
        if not any(hit.ctx.info.code in snapshot.values for hit in report.hits):
            return None
        return (f"營收年增：{snapshot.month:%Y-%m} 月營收，{snapshot.count:,} 檔上市櫃普通股排名"
                f"（快照 {snapshot.snapshot_date:%m/%d}）")
    if factor.status is SnapshotStatus.STALE and factor.snapshot is not None:
        return (f"⚠️ 因子快照 {factor.snapshot.snapshot_date:%m/%d} 已超過 {factor.max_age_days} 天沒有更新，"
                "未顯示營收年增")
    if factor.status is SnapshotStatus.MISSING:
        return "⚠️ 找不到因子快照，未顯示營收年增"
    return "⚠️ 因子快照格式錯誤，未顯示營收年增"


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


def _hit_lines(hit: Hit, snapshot: FactorSnapshot | None) -> list[str]:
    info = hit.ctx.info
    quote = f"{info.code} {info.name}  {format_number(hit.ctx.today['close'])}"
    if hit.change_pct is not None:
        quote += f"  {_change(hit.change_pct)}"
    fields = [quote, info.subcategory, hit.ctx.trend.label, "✅ 符合進場" if hit.entry else "",
              _revenue_growth(snapshot, info.code),
              f"⚠️ 價格為替代值（{hit.price_substitute.label}）" if hit.price_substitute is not None else ""]
    return [
        "｜".join(field for field in fields if field),
        f"  └ {'、'.join(signal.rule_name for signal in hit.signals)}",
    ]


def _revenue_growth(snapshot: FactorSnapshot | None, code: str) -> str:
    """例：「營收年增 +55%（前 18%）」；快照不能用或該檔沒有值時為空字串（D58）。"""
    if snapshot is None or code not in snapshot.values:
        return ""
    value, rank = snapshot.values[code]
    pct = round(value * 100)
    return f"營收年增 {f'{pct:+,d}%' if pct else '0%'}（前 {snapshot.percentile(rank)}%）"


def _change(pct: float) -> str:
    rounded = round(pct, 1)
    if rounded > 0:
        return f"▲{rounded:.1f}%"
    if rounded < 0:
        return f"▼{-rounded:.1f}%"
    return "0.0%"


# --- 拆成多則 ---------------------------------------------------------------

def pack_sections(sections: Sequence[Section], limit: int) -> list[str]:
    """把區塊依序放進每則不超過 limit 字（UTF-16）的訊息。

    一個區塊盡量放在同一則；放不下時從新的一則開始（開頭補上 resume）；單一區塊就超過上限時，
    在 items 之間拆開（新的一則開頭補上 resume_inside）；單一行就超過上限時直接切開。
    """
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
