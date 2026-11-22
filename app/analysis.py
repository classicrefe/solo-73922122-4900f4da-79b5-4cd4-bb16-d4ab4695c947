"""桩号表解析与换版核查核心逻辑。

桩号表每行描述一个“段”，字段为：
    segment_no（段号）、start_mileage_m（工程里程起点，整数米）、
    end_mileage_m（工程里程终点，整数米，左闭右开）、
    start_chainage（起点桩号，整数米）

约定
----
* 工程里程与桩号均为整数米；
* 段区间为 [start_mileage_m, end_mileage_m)，段内每增加 1 米，桩号加 1；
  因此该段的桩号区间为
  [start_chainage, start_chainage + (end_mileage_m - start_mileage_m))；
* 桩号在全线唯一。若输入表存在桩号区间重叠，工具不会拒绝整张表，
  而是把受影响区间标为“重号”，便于养护员定位。

核查一个旧号 x 是否可迁移的完整条件
------------------------------------
1. 旧表中恰好有一个段覆盖 x（旧号在全线唯一）；
2. 该工程里程处恰好有一个旧段覆盖（否则同一位置出现多个旧号，无法判断迁移谁）；
3. 新表中恰好有一个段覆盖该工程里程（该工程位置有新号）；
4. 得到的新号 n 在新表中恰好出现一次（新号在全线唯一）。
四条都满足才判为可迁移，否则按首个不满足的条件给出缺号/重号/新表未覆盖。

长区间处理
----------
不逐米展开。做法是收集所有“分类可能发生变化”的边界（旧段桩号端点、
旧段里程端点映射到桩号、新段里程端点映射到桩号、新号区间重叠端点映射到桩号），
把查询区间切成若干半开子区间；每个子区间取一个样本点判定，
再按状态/段号/偏移合并相邻子区间。复杂度与段数和边界点数相关，与区间米数无关。
"""

from __future__ import annotations

import bisect
import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    segment_no: str
    start_mileage_m: int
    end_mileage_m: int
    start_chainage: int

    @property
    def span(self) -> int:
        return self.end_mileage_m - self.start_mileage_m

    @property
    def chainage_end(self) -> int:
        """桩号终点（不含）。"""
        return self.start_chainage + self.span

    def mileage_at(self, chainage: int) -> int:
        return self.start_mileage_m + (chainage - self.start_chainage)

    def chainage_at(self, mileage: int) -> int:
        return self.start_chainage + (mileage - self.start_mileage_m)


class IntervalIndex:
    """按区间起点排序的索引，用于查询覆盖某个坐标的所有区间。

    额外维护前缀最大终点，查询时可提前停止，避免每次扫描全表。
    """

    def __init__(self, segments: Iterable[Segment], start: str, end: str):
        self._segments = sorted(segments, key=lambda s: getattr(s, start))
        self._starts = [getattr(s, start) for s in self._segments]
        self._ends = [getattr(s, end) for s in self._segments]
        self._start_attr = start
        self._end_attr = end
        prefix: list[int] = []
        best: int | None = None
        for value in self._ends:
            best = value if best is None else max(best, value)
            prefix.append(best)
        self._prefix_max_end = prefix
        # 按终点排序，用于寻找“最近的前一个区间”
        self._by_end = sorted(self._segments, key=lambda s: getattr(s, end))
        self._ends_sorted = [getattr(s, end) for s in self._by_end]

    def __len__(self) -> int:
        return len(self._segments)

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)

    def covering(self, point: int, limit: int | None = None) -> list[Segment]:
        """返回所有满足 start <= point < end 的段（按起点排序）。"""
        i = bisect.bisect_right(self._starts, point) - 1
        found: list[Segment] = []
        while i >= 0:
            if self._prefix_max_end[i] <= point:
                break
            seg = self._segments[i]
            if getattr(seg, self._end_attr) > point:
                found.append(seg)
                if limit is not None and len(found) >= limit:
                    break
            i -= 1
        found.reverse()
        return found

    def overlapping(self, start: int, end: int) -> list[Segment]:
        """返回所有与 [start, end) 有交叠的段（按起点排序），无交叠则空。"""
        if end <= start:
            return []
        i = bisect.bisect_left(self._starts, end) - 1
        found: list[Segment] = []
        while i >= 0:
            if self._prefix_max_end[i] <= start:
                break
            seg = self._segments[i]
            if getattr(seg, self._end_attr) > start:
                found.append(seg)
            i -= 1
        found.reverse()
        return found

    def predecessor(self, point: int) -> Segment | None:
        """返回终点 <= point 中终点最大（最近）的段。"""
        i = bisect.bisect_right(self._ends_sorted, point) - 1
        return self._by_end[i] if i >= 0 else None

    def successor(self, point: int) -> Segment | None:
        """返回起点 > point 中起点最小的段。"""
        i = bisect.bisect_right(self._starts, point)
        return self._segments[i] if i < len(self._segments) else None


@dataclass
class SegmentTable:
    """一版桩号表。"""

    segments: tuple[Segment, ...]
    label: str = "桩号表"
    chainage_index: IntervalIndex = field(init=False, repr=False)
    mileage_index: IntervalIndex = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.chainage_index = IntervalIndex(self.segments, "start_chainage", "chainage_end")
        self.mileage_index = IntervalIndex(self.segments, "start_mileage_m", "end_mileage_m")

    def __len__(self) -> int:
        return len(self.segments)

    @staticmethod
    def build(segments: Iterable[Segment], label: str = "桩号表") -> SegmentTable:
        # 按桩号起点排序；同起点再按里程、段号排序，保证输出稳定。
        ordered = tuple(
            sorted(segments, key=lambda s: (s.start_chainage, s.start_mileage_m, s.segment_no))
        )
        return SegmentTable(segments=ordered, label=label)


# ---------------------------------------------------------------------------
# 输入校验 / 提示
# ---------------------------------------------------------------------------


class InputError(ValueError):
    """用户输入（文件、范围）不合法，可展示给用户。"""


def validate_table(table: SegmentTable) -> list[str]:
    """结构性错误；有错误则不能进入核查。重号不算结构错误，会在结果中分类。"""
    errors: list[str] = []
    label = table.label
    if not table.segments:
        errors.append(f"{label}：表中没有任何段。")
        return errors

    seen_no: dict[str, int] = {}
    for seg in table.segments:
        if not seg.segment_no.strip():
            errors.append(f"{label}：存在空段号。")
        else:
            seen_no[seg.segment_no] = seen_no.get(seg.segment_no, 0) + 1
        if seg.span <= 0:
            errors.append(
                f"{label}：段 {seg.segment_no or '(空段号)'} 的里程范围 "
                f"[{seg.start_mileage_m}, {seg.end_mileage_m}) 必须为非空区间。"
            )
    dup_no = sorted(no for no, cnt in seen_no.items() if cnt > 1)
    if dup_no:
        errors.append(f"{label}：段号重复：{'、'.join(dup_no)}。")
    return errors


def table_warnings(table: SegmentTable) -> list[str]:
    """不阻止核查、但值得提示的数据情况。"""
    warnings: list[str] = []
    label = table.label

    # 桩号区间重叠 → 重号
    ordered = sorted(table.segments, key=lambda s: s.start_chainage)
    for a, b in pairwise(ordered):
        if b.start_chainage < a.chainage_end:
            warnings.append(
                f"{label}：段 {a.segment_no} 与段 {b.segment_no} 的桩号区间重叠，"
                f"重叠部分会被判为重号。"
            )

    # 里程区间重叠 → 同一工程位置对应多个段
    by_mileage = sorted(table.segments, key=lambda s: s.start_mileage_m)
    for a, b in pairwise(by_mileage):
        if b.start_mileage_m < a.end_mileage_m:
            warnings.append(
                f"{label}：段 {a.segment_no} 与段 {b.segment_no} 的工程里程范围重叠，"
                f"重叠位置存在多个桩号，相关旧号无法自动迁移。"
            )

    # 里程不连续（仅提示，不影响核查）
    for a, b in pairwise(by_mileage):
        if a.end_mileage_m != b.start_mileage_m:
            warnings.append(
                f"{label}：段 {a.segment_no}（里程止 {a.end_mileage_m}）与段 "
                f"{b.segment_no}（里程起 {b.start_mileage_m}）不连续，中间里程不在本表覆盖内。"
            )
    return warnings


# ---------------------------------------------------------------------------
# 解析桩号表（CSV / XLSX）
# ---------------------------------------------------------------------------

FIELD_ALIASES = {
    "segment_no": {"segment_no", "段号", "段落号", "区间号", "编号"},
    "start_mileage_m": {
        "start_mileage_m", "start_mileage", "里程起点", "起点里程", "start_km",
        "里程起点m", "起点里程m", "起始里程", "起始里程m",
    },
    "end_mileage_m": {
        "end_mileage_m", "end_mileage", "里程终点", "终点里程", "end_km",
        "里程终点m", "终点里程m", "结束里程", "结束里程m",
    },
    "start_chainage": {
        "start_chainage", "start_chainage_m", "起点桩号", "起点桩号m",
        "start_stake", "开始桩号", "起始桩号",
    },
}
REQUIRED_FIELDS = tuple(FIELD_ALIASES.keys())
FIELD_ZH = {
    "segment_no": "段号",
    "start_mileage_m": "里程起点",
    "end_mileage_m": "里程终点",
    "start_chainage": "起点桩号",
}

_NUM_RE = re.compile(r"^[+-]?\d+(?:\.0+)?$")


def _normalise_header(name: object) -> str:
    cleaned = str(name or "").strip().replace("﻿", "").lower()
    return re.sub(r"[\s_\-（）()]+", "", cleaned)

_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in FIELD_ALIASES.items():
    for _alias in _aliases:
        _ALIAS_LOOKUP[_normalise_header(_alias)] = _canonical


def _map_header(raw_header: object) -> str | None:
    return _ALIAS_LOOKUP.get(_normalise_header(raw_header))


def _parse_int(value: object, field_name: str, row_no: int, filename: str) -> int:
    if isinstance(value, bool):
        raise InputError(f"{filename} 第 {row_no} 行：{field_name} 不是整数（{value!r}）。")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise InputError(f"{filename} 第 {row_no} 行：{field_name} 必须是整数米（{value!r}）。")
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        raise InputError(f"{filename} 第 {row_no} 行：{field_name} 为空。")
    if not _NUM_RE.match(text):
        raise InputError(f"{filename} 第 {row_no} 行：{field_name} 必须是整数米（{text!r}）。")
    return int(float(text))


def _segment_from_row(mapped: dict[str, object], row_no: int, filename: str) -> Segment:
    missing = [f for f in REQUIRED_FIELDS if f not in mapped or str(mapped[f]).strip() == ""]
    if missing:
        raise InputError(
            f"{filename} 第 {row_no} 行：缺少 {', '.join(FIELD_ZH[m] for m in missing)}。"
        )
    return Segment(
        segment_no=str(mapped["segment_no"]).strip(),
        start_mileage_m=_parse_int(mapped["start_mileage_m"], "里程起点", row_no, filename),
        end_mileage_m=_parse_int(mapped["end_mileage_m"], "里程终点", row_no, filename),
        start_chainage=_parse_int(mapped["start_chainage"], "起点桩号", row_no, filename),
    )


def _rows_to_table(rows: list[list[object]], filename: str, label: str) -> SegmentTable:
    header_idx: int | None = None
    for i, row in enumerate(rows):
        if any(str(cell).strip() for cell in row if cell is not None):
            header_idx = i
            break
    if header_idx is None:
        raise InputError(f"{filename}：文件为空。")

    header = rows[header_idx]
    col_map: dict[int, str] = {}
    for idx, cell in enumerate(header):
        canonical = _map_header(cell)
        if canonical is not None and canonical not in col_map.values():
            col_map[idx] = canonical
    missing_cols = [f for f in REQUIRED_FIELDS if f not in col_map.values()]
    if missing_cols:
        raise InputError(
            f"{filename}：表头缺少列 {', '.join(FIELD_ZH[m] for m in missing_cols)}；"
            f"支持的表头示例：段号, 里程起点, 里程终点, 起点桩号（也支持 segment_no 等英文名）。"
        )

    segments: list[Segment] = []
    for offset, row in enumerate(rows[header_idx + 1:]):
        row_no = header_idx + offset + 2  # 1 基、含表头
        if not any(str(cell).strip() for cell in row if cell is not None):
            continue
        mapped = {
            canonical: (row[idx] if idx < len(row) else None)
            for idx, canonical in col_map.items()
        }
        segments.append(_segment_from_row(mapped, row_no, filename))
    if not segments:
        raise InputError(f"{filename}：除表头外没有数据行。")
    return SegmentTable.build(segments, label=label)


def parse_csv_bytes(data: bytes, filename: str, label: str = "桩号表") -> SegmentTable:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise InputError(f"{filename}：无法识别文件编码，请使用 UTF-8 或 GB18030。") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    return _rows_to_table(rows, filename, label)


def parse_xlsx_bytes(data: bytes, filename: str, label: str = "桩号表") -> SegmentTable:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 部署环境已安装
        raise InputError("服务器缺少 openpyxl，无法解析 Excel 文件。") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise InputError(f"{filename}：无法读取 Excel 文件（{exc}）。") from exc
    try:
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _rows_to_table(rows, filename, label)


def parse_table_bytes(data: bytes, filename: str, label: str = "桩号表") -> SegmentTable:
    lower = (filename or "").lower()
    if lower.endswith(".xlsx"):
        return parse_xlsx_bytes(data, filename, label)
    if lower.endswith(".csv"):
        return parse_csv_bytes(data, filename, label)
    if data[:2] == b"PK":  # xlsx 是 zip 容器
        return parse_xlsx_bytes(data, filename, label)
    return parse_csv_bytes(data, filename, label)


# ---------------------------------------------------------------------------
# 核查
# ---------------------------------------------------------------------------

STATUS_MIGRATABLE = "migratable"
STATUS_MISSING_OLD = "missing_old"
STATUS_DUPLICATE_OLD = "duplicate_old"
STATUS_UNCOVERED_NEW = "uncovered_new"
STATUS_DUPLICATE_NEW = "duplicate_new"

STATUS_LABELS = {
    STATUS_MIGRATABLE: "可迁移",
    STATUS_MISSING_OLD: "缺号",
    STATUS_DUPLICATE_OLD: "重号（旧表）",
    STATUS_UNCOVERED_NEW: "新表未覆盖",
    STATUS_DUPLICATE_NEW: "重号（新表）",
}


@dataclass
class MigrationRow:
    status: str
    range_start_old: int
    range_end_old: int
    count: int
    new_start: int | None = None
    new_end: int | None = None
    offset: int | None = None
    segment_nos: tuple[str, ...] = ()
    detail: str = ""

    @property
    def migratable(self) -> bool:
        return self.status == STATUS_MIGRATABLE

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "range_start_old": self.range_start_old,
            "range_end_old": self.range_end_old,
            "count": self.count,
            "new_start": self.new_start,
            "new_end": self.new_end,
            "offset": self.offset,
            "segment_nos": "、".join(self.segment_nos),
            "detail": self.detail,
            "migratable": self.migratable,
        }


@dataclass
class AnalysisResult:
    old_table: SegmentTable
    new_table: SegmentTable
    query_start: int
    query_end: int
    rows: list[MigrationRow]
    notes: list[str] = field(default_factory=list)

    @property
    def migratable_rows(self) -> list[MigrationRow]:
        return [r for r in self.rows if r.migratable]

    @property
    def total_queried(self) -> int:
        return self.query_end - self.query_start + 1

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUS_LABELS}
        for row in self.rows:
            counts[row.status] += row.count
        return counts

    def summary_rows(self) -> list[dict[str, object]]:
        counts = self.counts()
        return [
            {
                "status": status,
                "label": STATUS_LABELS[status],
                "count": counts[status],
                "intervals": sum(1 for r in self.rows if r.status == status),
            }
            for status in STATUS_LABELS
            if counts[status] or status == STATUS_MIGRATABLE
        ]


def _merge_close_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相接/相交的闭区间。"""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _subtract_ranges(
    base: list[tuple[int, int]], cuts: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    cuts = _merge_close_ranges(cuts)
    result: list[tuple[int, int]] = []
    for start, end in base:
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end < cursor:
                continue
            if cut_start > end:
                break
            if cut_start > cursor:
                result.append((cursor, min(cut_start - 1, end)))
            cursor = max(cursor, cut_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            result.append((cursor, end))
    return result


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> tuple[int, int] | None:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return (lo, hi) if lo < hi else None


class RangeSet:
    """有序、互不相接的半开区间集合，支持快速求交集。

    `_intersect_ranges` 原先每次扫描整个重叠区间列表；在段数多、重叠多时
    会成为瓶颈。这里预存起点数组并用二分定位。
    """

    def __init__(self, ranges: Iterable[tuple[int, int]]):
        self._ranges = list(ranges)
        self._starts = [r[0] for r in self._ranges]

    def __bool__(self) -> bool:
        return bool(self._ranges)

    def intersect(self, low: int, high: int) -> list[tuple[int, int]]:
        if low >= high or not self._ranges:
            return []
        result: list[tuple[int, int]] = []
        index = max(0, bisect.bisect_right(self._starts, low) - 1)
        while index < len(self._ranges):
            start, end = self._ranges[index]
            if start >= high:
                break
            if end > low:
                result.append((max(low, start), min(high, end)))
            index += 1
        return result


def _chainage_overlap_ranges(segments: tuple[Segment, ...]) -> RangeSet:
    """返回桩号空间中“被至少两个段覆盖”的半开区间集合。"""
    events: list[tuple[int, int]] = []
    for seg in segments:
        if seg.chainage_end > seg.start_chainage:
            events.append((seg.start_chainage, 1))
            events.append((seg.chainage_end, -1))
    if not events:
        return RangeSet([])
    events.sort()
    ranges: list[tuple[int, int]] = []
    count = 0
    index = 0
    while index < len(events):
        coord = events[index][0]
        while index < len(events) and events[index][0] == coord:
            count += events[index][1]
            index += 1
        next_coord = events[index][0] if index < len(events) else None
        if count >= 2 and next_coord is not None and next_coord > coord:
            if ranges and ranges[-1][1] == coord:
                ranges[-1] = (ranges[-1][0], next_coord)
            else:
                ranges.append((coord, next_coord))
    return RangeSet(ranges)


def _intersect_ranges(
    ranges: RangeSet, low: int, high: int
) -> list[tuple[int, int]]:
    """求半开区间集合与 [low, high) 的交集。"""
    return ranges.intersect(low, high)


def _collect_boundaries(
    old_table: SegmentTable,
    new_table: SegmentTable,
    q_start: int,
    q_end: int,
) -> list[int]:
    """收集旧号空间中分类可能变化的边界。返回已排序的唯一整数边界。

    边界来源（均在旧号坐标下）：
    * 旧段桩号区间端点：旧号是否唯一、是否有段覆盖发生变化；
    * 旧段里程与任一其他旧段里程的交叠端点：同一工程位置是否对应多个旧号；
    * 旧段里程与任一新段里程的交叠端点：新表是否需要补段、新号是否重复；
    * 新旧段里程交叠区内，新段桩号与其他新段桩号重叠的区间端点：
      映射到旧号后即新号唯一性变化的位置。

    复杂度与段数相关（近似 O(n log n) 加实际交叠对数），与区间长度无关。
    """
    q_stop = q_end + 1
    boundaries: set[int] = {q_start, q_stop}

    def add(value: int) -> None:
        if q_start < value < q_stop:
            boundaries.add(value)

    old_segs = old_table.segments
    new_segs = new_table.segments

    # 旧段桩号端点
    for old_seg in old_segs:
        add(old_seg.start_chainage)
        add(old_seg.chainage_end)

    # 新表桩号重叠区间，预先算好
    new_chain_overlaps = _chainage_overlap_ranges(new_segs)

    # 旧表里程重叠区间，预先算好（半开）。用 start_chainage 当作里程坐标。
    old_mileage_overlaps = _chainage_overlap_ranges(
        tuple(
            Segment(s.segment_no, s.start_mileage_m, s.end_mileage_m, s.start_mileage_m)
            for s in old_segs
        )
    )

    old_query = [
        seg for seg in old_segs if seg.start_chainage < q_stop and seg.chainage_end > q_start
    ]

    # 用 IntervalIndex 查询，避免整表两两扫描
    new_mileage_index = new_table.mileage_index

    for old_seg in old_query:
        o_m_lo, o_m_hi = old_seg.start_mileage_m, old_seg.end_mileage_m

        # 旧表同一里程被多个段覆盖 → 边界取交叠端点
        for m_lo, m_hi in _intersect_ranges(old_mileage_overlaps, o_m_lo, o_m_hi):
            add(old_seg.chainage_at(m_lo))
            add(old_seg.chainage_at(m_hi))

        # 与旧段里程相交的新段
        overlapping_new = new_mileage_index.overlapping(o_m_lo, o_m_hi)
        for new_seg in overlapping_new:
            m_lo = max(o_m_lo, new_seg.start_mileage_m)
            m_hi = min(o_m_hi, new_seg.end_mileage_m)
            add(old_seg.chainage_at(m_lo))
            add(old_seg.chainage_at(m_hi))

            # 该新段的桩号范围（仅限本交叠区可达的部分）
            chain_low = new_seg.chainage_at(m_lo)
            chain_high = new_seg.chainage_at(m_hi)
            offset = chain_low - old_seg.chainage_at(m_lo)
            for c_lo, c_hi in _intersect_ranges(new_chain_overlaps, chain_low, chain_high):
                add(c_lo - offset)
                add(c_hi - offset)

    return sorted(boundaries)


def _related_for_uncovered(new_table: SegmentTable, mileage: int) -> tuple[str, ...]:
    pred = new_table.mileage_index.predecessor(mileage)
    succ = new_table.mileage_index.successor(mileage)
    nos: list[str] = []
    if pred is not None:
        nos.append(pred.segment_no)
    if succ is not None and succ.segment_no not in nos:
        nos.append(succ.segment_no)
    return tuple(nos)


def _related_for_missing(old_table: SegmentTable, chainage: int) -> tuple[str, ...]:
    pred = old_table.chainage_index.predecessor(chainage)
    succ = old_table.chainage_index.successor(chainage)
    nos: list[str] = []
    if pred is not None:
        nos.append(pred.segment_no)
    if succ is not None and succ.segment_no not in nos:
        nos.append(succ.segment_no)
    return tuple(nos)


def _dedupe_nos(segments: Iterable[Segment]) -> tuple[str, ...]:
    seen: list[str] = []
    for seg in segments:
        if seg.segment_no not in seen:
            seen.append(seg.segment_no)
    return tuple(seen)


def _classify_sample(
    old_table: SegmentTable,
    new_table: SegmentTable,
    chainage: int,
) -> tuple[str, int | None, tuple[str, ...], str]:
    """判断单个旧号的分类。返回 (status, offset, segment_nos, detail)。"""
    old_cov = old_table.chainage_index.covering(chainage)
    if not old_cov:
        nos = _related_for_missing(old_table, chainage)
        return (
            STATUS_MISSING_OLD,
            None,
            nos,
            "旧表没有任何段覆盖此桩号；需先补录段号/补段后才能迁移。"
            + (f" 相邻旧段：{'、'.join(nos)}。" if nos else ""),
        )
    if len(old_cov) > 1:
        nos = _dedupe_nos(old_cov)
        return (
            STATUS_DUPLICATE_OLD,
            None,
            nos,
            f"旧表有 {len(old_cov)} 个段的桩号区间同时覆盖此号，旧号不唯一。",
        )

    old_seg = old_cov[0]
    mileage = old_seg.mileage_at(chainage)

    old_mileage_cov = old_table.mileage_index.covering(mileage)
    if len(old_mileage_cov) > 1:
        nos = _dedupe_nos(old_mileage_cov)
        return (
            STATUS_DUPLICATE_OLD,
            None,
            nos,
            (
                f"旧表有 {len(old_mileage_cov)} 个段覆盖同一工程里程，"
                f"同一位置对应多个旧号，无法确定迁移哪一个。"
            ),
        )

    new_cov = new_table.mileage_index.covering(mileage)
    if not new_cov:
        nos = _related_for_uncovered(new_table, mileage)
        return (
            STATUS_UNCOVERED_NEW,
            None,
            nos,
            "新表没有覆盖该工程里程；需先在新表中补段。"
            + (f" 相邻新段：{'、'.join(nos)}。" if nos else ""),
        )
    if len(new_cov) > 1:
        nos = _dedupe_nos(new_cov)
        return (
            STATUS_DUPLICATE_NEW,
            None,
            nos,
            f"该工程里程在新表被 {len(new_cov)} 个段同时覆盖，新号不唯一。",
        )

    new_seg = new_cov[0]
    new_chainage = new_seg.chainage_at(mileage)
    new_chainage_cov = new_table.chainage_index.covering(new_chainage)
    if len(new_chainage_cov) > 1:
        nos = _dedupe_nos(new_chainage_cov)
        return (
            STATUS_DUPLICATE_NEW,
            None,
            nos,
            f"新号 {new_chainage} 在新表出现 {len(new_chainage_cov)} 次，新号不唯一。",
        )

    offset = new_chainage - chainage
    return (
        STATUS_MIGRATABLE,
        offset,
        (old_seg.segment_no, new_seg.segment_no),
        f"旧段 {old_seg.segment_no} → 新段 {new_seg.segment_no}，新旧号差值 {offset:+d}。",
    )


def _merge_rows(rows: list[MigrationRow]) -> list[MigrationRow]:
    rows.sort(key=lambda r: (r.range_start_old, r.status))
    merged: list[MigrationRow] = []
    for row in rows:
        if merged:
            prev = merged[-1]
            same = (
                prev.status == row.status
                and prev.range_end_old + 1 == row.range_start_old
                and prev.segment_nos == row.segment_nos
                and prev.offset == row.offset
                and prev.detail == row.detail
            )
            if same:
                prev.range_end_old = row.range_end_old
                prev.count = prev.range_end_old - prev.range_start_old + 1
                if prev.migratable and prev.offset is not None:
                    prev.new_end = prev.range_end_old + prev.offset
                continue
        merged.append(row)
    return merged


def analyse_migration(
    old_table: SegmentTable,
    new_table: SegmentTable,
    query_start: int,
    query_end: int,
) -> AnalysisResult:
    if query_start > query_end:
        raise InputError("旧号起不能大于旧号止。")

    boundaries = _collect_boundaries(old_table, new_table, query_start, query_end)
    rows: list[MigrationRow] = []
    # 每个 [boundaries[i], boundaries[i+1]) 半开子区间内分类恒定
    for left, right in pairwise(boundaries):
        if left >= right:
            continue
        status, offset, seg_nos, detail = _classify_sample(old_table, new_table, left)
        new_start = left + offset if offset is not None else None
        new_end = (right - 1) + offset if offset is not None else None
        rows.append(
            MigrationRow(
                status=status,
                range_start_old=left,
                range_end_old=right - 1,
                count=right - left,
                new_start=new_start,
                new_end=new_end,
                offset=offset,
                segment_nos=seg_nos,
                detail=detail,
            )
        )

    rows = _merge_rows(rows)

    # 自检：分类区间应恰好覆盖查询范围，无空洞、无重叠
    covered = _merge_close_ranges([(r.range_start_old, r.range_end_old) for r in rows])
    if covered != [(query_start, query_end)]:
        missing = _subtract_ranges([(query_start, query_end)], covered)
        raise RuntimeError(
            "内部错误：核查结果未完整覆盖查询范围，缺口 "
            + "、".join(f"[{a}, {b}]" for a, b in missing)
        )

    return AnalysisResult(
        old_table=old_table,
        new_table=new_table,
        query_start=query_start,
        query_end=query_end,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# CSV 输出（只含可迁移区间）
# ---------------------------------------------------------------------------

CSV_HEADER = ["旧号起", "旧号止", "旧号个数", "新号起", "新号止", "旧段号", "新段号"]


def result_to_csv(result: AnalysisResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in result.migratable_rows:
        old_seg = row.segment_nos[0] if len(row.segment_nos) > 0 else ""
        new_seg = row.segment_nos[1] if len(row.segment_nos) > 1 else ""
        writer.writerow([
            row.range_start_old,
            row.range_end_old,
            row.count,
            row.new_start,
            row.new_end,
            old_seg,
            new_seg,
        ])
    return buf.getvalue()
