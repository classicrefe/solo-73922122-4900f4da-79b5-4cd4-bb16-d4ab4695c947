"""桩号表与核查逻辑的测试。

其中 test_randomised_against_brute_force 用逐米枚举作为对照实现，
随机生成合法/非法表，验证区间算法与基准结果完全一致。
"""

from __future__ import annotations

import io
import random
import time

import pytest

from app.analysis import (
    STATUS_DUPLICATE_NEW,
    STATUS_DUPLICATE_OLD,
    STATUS_MIGRATABLE,
    STATUS_MISSING_OLD,
    STATUS_UNCOVERED_NEW,
    InputError,
    Segment,
    SegmentTable,
    analyse_migration,
    parse_csv_bytes,
    parse_table_bytes,
    result_to_csv,
    table_warnings,
    validate_table,
)


def build(rows, label="表"):
    return SegmentTable.build(
        [Segment(no, sm, em, sc) for no, sm, em, sc in rows], label=label
    )


def classify(result):
    """把结果展开成 {旧号: (状态, 新号)}，仅用于小范围测试。"""
    out = {}
    for row in result.rows:
        for x in range(row.range_start_old, row.range_end_old + 1):
            if row.migratable:
                offset = row.new_start - row.range_start_old
                out[x] = (row.status, x + offset)
            else:
                out[x] = (row.status, None)
    return out


# ---------------------------------------------------------------------------
# 基础计算
# ---------------------------------------------------------------------------


def test_segment_arithmetic():
    seg = Segment("K1", 1000, 1500, 20000)
    assert seg.span == 500
    assert seg.chainage_end == 20500
    assert seg.mileage_at(20000) == 1000
    assert seg.mileage_at(20499) == 1499
    assert seg.chainage_at(1499) == 20499


def test_interval_index_covering_and_overlap():
    table = build([("A", 0, 100, 0), ("B", 50, 150, 500)])
    assert [s.segment_no for s in table.chainage_index.covering(10)] == ["A"]
    assert [s.segment_no for s in table.chainage_index.covering(500)] == ["B"]
    assert table.chainage_index.covering(1000) == []
    assert [s.segment_no for s in table.mileage_index.covering(60)] == ["A", "B"]
    assert [s.segment_no for s in table.mileage_index.overlapping(40, 60)] == ["A", "B"]
    assert [s.segment_no for s in table.mileage_index.overlapping(100, 200)] == ["B"]


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------


def test_simple_shift_all_migratable():
    old = build([("K1", 0, 500, 1000)])
    new = build([("N1", 0, 500, 1200)])
    result = analyse_migration(old, new, 1000, 1499)
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.status == STATUS_MIGRATABLE
    assert (row.range_start_old, row.range_end_old) == (1000, 1499)
    assert (row.new_start, row.new_end) == (1200, 1699)
    assert row.count == 500
    assert row.segment_nos == ("K1", "N1")


def test_missing_old_number():
    old = build([("K1", 0, 100, 1000), ("K2", 100, 200, 1300)])
    new = build([("N1", 0, 200, 1000)])
    result = analyse_migration(old, new, 1050, 1350)
    by_old = classify(result)
    assert by_old[1050][0] == STATUS_MIGRATABLE
    assert by_old[1100][0] == STATUS_MISSING_OLD  # K1 止于 1099
    assert by_old[1299][0] == STATUS_MISSING_OLD
    assert by_old[1300][0] == STATUS_MIGRATABLE
    missing_row = next(r for r in result.rows if r.status == STATUS_MISSING_OLD)
    assert missing_row.segment_nos == ("K1", "K2")  # 相邻段号
    assert "补" in missing_row.detail


def test_old_duplicate_chainage_overlap():
    old = build([("K1", 0, 100, 1000), ("K2", 0, 100, 1050)])
    new = build([("N1", 0, 100, 1000)])
    result = analyse_migration(old, new, 1000, 1200)
    by_old = classify(result)
    # 1050..1099 两个旧段的桩号区间都覆盖
    assert by_old[1050][0] == STATUS_DUPLICATE_OLD
    assert by_old[1099][0] == STATUS_DUPLICATE_OLD
    # 1100..1149 仅有 K2 覆盖，但其里程 50..99 又被 K1 覆盖，同一位置两个旧号
    assert by_old[1100][0] == STATUS_DUPLICATE_OLD
    # 1150 起旧表无段
    assert by_old[1150][0] == STATUS_MISSING_OLD
    dup_row = next(r for r in result.rows if r.status == STATUS_DUPLICATE_OLD)
    assert dup_row.segment_nos == ("K1", "K2")


def test_old_mileage_overlap_makes_old_numbers_ambiguous():
    # 两个旧段覆盖工程里程 50..99，同一位置有两个旧号
    old = build([("K1", 0, 100, 1000), ("K2", 50, 150, 2000)])
    new = build([("N1", 0, 200, 5000)])
    result = analyse_migration(old, new, 1000, 2200)
    by_old = classify(result)
    # 里程 50..99 对应旧号 K1 1050..1099、K2 2000..2049
    assert by_old[1060][0] == STATUS_DUPLICATE_OLD
    assert by_old[2020][0] == STATUS_DUPLICATE_OLD
    assert by_old[1030][0] == STATUS_MIGRATABLE
    # K2 的里程 100..149 唯一对应旧号 2050..2099
    assert by_old[2050][0] == STATUS_MIGRATABLE
    assert by_old[2100][0] == STATUS_MISSING_OLD  # 旧表止于 2099


def test_new_table_uncovered():
    old = build([("K1", 0, 1000, 1000)])
    new = build([("N1", 0, 400, 1000), ("N2", 600, 1000, 1500)])
    result = analyse_migration(old, new, 1300, 1800)
    by_old = classify(result)
    # 旧号 1300..1399 → 里程 300..399 由 N1 覆盖
    assert by_old[1300][0] == STATUS_MIGRATABLE
    # 1400..1599 → 里程 400..599 无新段
    assert by_old[1500][0] == STATUS_UNCOVERED_NEW
    row = next(r for r in result.rows if r.status == STATUS_UNCOVERED_NEW)
    assert row.segment_nos == ("N1", "N2")
    # 1600.. 由 N2 覆盖
    assert by_old[1600][0] == STATUS_MIGRATABLE


def test_new_duplicate_chainage_elsewhere():
    # 新表另一段占用了相同新号
    old = build([("K1", 0, 100, 1000)])
    new = build([("N1", 0, 100, 1000), ("N2", 500, 600, 1050)])
    result = analyse_migration(old, new, 1000, 1100)
    by_old = classify(result)
    # 旧号 1050..1099 → 新号 1050..1099，同时被 N1、N2 占用
    assert by_old[1050][0] == STATUS_DUPLICATE_NEW
    assert by_old[1099][0] == STATUS_DUPLICATE_NEW
    assert by_old[1049][0] == STATUS_MIGRATABLE
    # 1100 已超出旧表 K1 的桩号范围
    assert by_old[1100][0] == STATUS_MISSING_OLD


def test_new_mileage_overlap():
    old = build([("K1", 0, 200, 1000)])
    new = build([("N1", 0, 200, 1000), ("N2", 100, 150, 5000)])
    result = analyse_migration(old, new, 1000, 1200)
    by_old = classify(result)
    # 里程 100..149 → 旧号 1100..1149，新表两段同时覆盖
    assert by_old[1100][0] == STATUS_DUPLICATE_NEW
    assert by_old[1149][0] == STATUS_DUPLICATE_NEW
    assert by_old[1099][0] == STATUS_MIGRATABLE
    assert by_old[1150][0] == STATUS_MIGRATABLE


def test_segment_boundaries_are_split():
    old = build([("K1", 0, 100, 1000), ("K2", 100, 200, 1100)])
    new = build([("N1", 0, 100, 5000), ("N2", 100, 200, 6000)])
    result = analyse_migration(old, new, 1000, 1199)
    assert [(r.status, r.range_start_old, r.range_end_old) for r in result.rows] == [
        (STATUS_MIGRATABLE, 1000, 1099),
        (STATUS_MIGRATABLE, 1100, 1199),
    ]
    assert result.rows[0].segment_nos == ("K1", "N1")
    assert result.rows[1].segment_nos == ("K2", "N2")


# ---------------------------------------------------------------------------
# 长区间 / 性能
# ---------------------------------------------------------------------------


def test_long_range_is_not_expanded():
    old = build([("K1", 0, 50_000_000, 0)])
    new = build([("N1", 0, 50_000_000, 10)])
    start = time.perf_counter()
    result = analyse_migration(old, new, 0, 49_999_999)
    elapsed = time.perf_counter() - start
    assert result.rows[0].count == 50_000_000
    assert result.rows[0].new_start == 10
    assert elapsed < 2.0, f"长区间处理过慢：{elapsed:.3f}s"


def test_many_segments_performance():
    rows_old, rows_new = [], []
    for i in range(5000):
        rows_old.append((f"K{i}", i * 1000, (i + 1) * 1000, i * 1000))
        rows_new.append((f"N{i}", i * 1000, (i + 1) * 1000, i * 1000 + (i % 7)))
    old = build(rows_old)
    new = build(rows_new)
    start = time.perf_counter()
    result = analyse_migration(old, new, 0, 4_999_999)
    elapsed = time.perf_counter() - start
    assert sum(r.count for r in result.rows) == 5_000_000
    assert elapsed < 5.0, f"多段处理过慢：{elapsed:.3f}s"


# ---------------------------------------------------------------------------
# 随机对照：区间算法 vs 逐米枚举
# ---------------------------------------------------------------------------


def brute_force(old_rows, new_rows, q_start, q_end):
    """逐米基准实现，仅用于小范围。"""

    def covering(segs, value, idx):
        return [s for s in segs if s[idx] <= value < s[idx] + s[2]]

    old = [Segment(no, sm, em, sc) for no, sm, em, sc in old_rows]
    new = [Segment(no, sm, em, sc) for no, sm, em, sc in new_rows]
    out = {}
    for x in range(q_start, q_end + 1):
        old_hit = [s for s in old if s.start_chainage <= x < s.chainage_end]
        if not old_hit:
            out[x] = (STATUS_MISSING_OLD, None)
            continue
        if len(old_hit) > 1:
            out[x] = (STATUS_DUPLICATE_OLD, None)
            continue
        m = old_hit[0].mileage_at(x)
        old_m_hit = [s for s in old if s.start_mileage_m <= m < s.end_mileage_m]
        if len(old_m_hit) > 1:
            out[x] = (STATUS_DUPLICATE_OLD, None)
            continue
        new_m_hit = [s for s in new if s.start_mileage_m <= m < s.end_mileage_m]
        if not new_m_hit:
            out[x] = (STATUS_UNCOVERED_NEW, None)
            continue
        if len(new_m_hit) > 1:
            out[x] = (STATUS_DUPLICATE_NEW, None)
            continue
        n = new_m_hit[0].chainage_at(m)
        new_c_hit = [s for s in new if s.start_chainage <= n < s.chainage_end]
        if len(new_c_hit) != 1:
            out[x] = (STATUS_DUPLICATE_NEW, None)
            continue
        out[x] = (STATUS_MIGRATABLE, n)
    return out


def random_table(rng, count, base_mileage, base_chainage, max_span=40, overlap_prob=0.25):
    rows = []
    cursor_m = base_mileage
    cursor_c = base_chainage
    for i in range(count):
        span = rng.randint(1, max_span)
        start_m = cursor_m
        end_m = cursor_m + span
        start_c = cursor_c + rng.choice([0, 0, 0, rng.randint(-5, 5)])
        rows.append((f"S{i}", start_m, end_m, start_c))
        if rng.random() < overlap_prob:
            cursor_m += rng.randint(0, span)  # 制造里程重叠/空隙
        else:
            cursor_m = end_m
        cursor_c = start_c + span + rng.choice([0, 0, 0, rng.randint(-5, 5)])
    return rows


@pytest.mark.parametrize("seed", range(60))
def test_randomised_against_brute_force(seed):
    rng = random.Random(seed)
    old_rows = random_table(rng, rng.randint(1, 6), 0, rng.randint(-20, 20))
    new_rows = random_table(rng, rng.randint(1, 6), rng.randint(-10, 10), rng.randint(-20, 20))
    q_start = min(r[3] for r in old_rows) - rng.randint(0, 15)
    q_end = max(r[3] + (r[2] - r[1]) for r in old_rows) + rng.randint(0, 15)

    result = analyse_migration(build(old_rows, "旧"), build(new_rows, "新"), q_start, q_end)
    expected = brute_force(old_rows, new_rows, q_start, q_end)
    actual = classify(result)
    assert actual == expected, (
        f"seed={seed}\nold={old_rows}\nnew={new_rows}\nq=({q_start},{q_end})"
    )
    assert sum(r.count for r in result.rows) == q_end - q_start + 1


# ---------------------------------------------------------------------------
# 输入解析与输出
# ---------------------------------------------------------------------------


def test_parse_csv_with_english_headers():
    data = (
        b"segment_no,start_mileage_m,end_mileage_m,start_chainage\n"
        b"K1,0,500,1000\n"
        b"K2,500,1000,1500\n"
    )
    table = parse_csv_bytes(data, "old.csv")
    assert [(s.segment_no, s.start_chainage) for s in table.segments] == [
        ("K1", 1000),
        ("K2", 1500),
    ]


def test_parse_csv_gb18030_and_bom():
    text = "段号,里程起点,里程终点,起点桩号\n甲,0,10,100\n"
    table = parse_csv_bytes(text.encode("gb18030"), "gbk.csv")
    assert table.segments[0].segment_no == "甲"
    table2 = parse_csv_bytes(("﻿" + text).encode("utf-8"), "bom.csv")
    assert len(table2.segments) == 1


def test_parse_rejects_non_integer_and_missing_columns():
    with pytest.raises(Exception) as exc:
        parse_csv_bytes("段号,里程起点,里程终点,起点桩号\nK1,0,1.5,100\n".encode(), "x.csv")
    assert "整数" in str(exc.value)

    with pytest.raises(Exception) as exc:
        parse_csv_bytes("段号,里程起点,起点桩号\nK1,0,100\n".encode(), "x.csv")
    assert "里程终点" in str(exc.value)


def test_parse_xlsx():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["段号", "里程起点", "里程终点", "起点桩号"])
    ws.append(["K1", 0, 500, 1000])
    buf = io.BytesIO()
    wb.save(buf)
    table = parse_table_bytes(buf.getvalue(), "t.xlsx")
    assert table.segments[0] == Segment("K1", 0, 500, 1000)


def test_validate_table_reports_bad_segments():
    table = build([("K1", 0, 0, 1000), ("K1", 20, 30, 2000)])
    errors = validate_table(table)
    assert any("非空区间" in e for e in errors)
    assert any("段号重复" in e for e in errors)


def test_warnings_for_overlap_and_gap():
    old = build([("K1", 0, 100, 1000), ("K2", 90, 150, 1100), ("K3", 160, 200, 1300)])
    warns = table_warnings(old)
    assert any("工程里程范围重叠" in w for w in warns)
    assert any("不连续" in w for w in warns)


def test_csv_contains_only_migratable_and_covers_all_of_them():
    old = build([("K1", 0, 1000, 1000)])
    new = build([("N1", 0, 400, 1000), ("N2", 600, 1000, 1300)])
    result = analyse_migration(old, new, 1000, 1999)
    csv_text = result_to_csv(result)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "旧号起,旧号止,旧号个数,新号起,新号止,旧段号,新段号"
    assert len(lines) == 1 + len(result.migratable_rows)
    assert "缺号" not in csv_text and "未覆盖" not in csv_text
    assert "1000,1299,300,1000,1299,K1,N1" in csv_text
    assert "1700,1999,300,1400,1699,K1,N2" in csv_text


def test_query_inverted_rejected():
    with pytest.raises(InputError):
        analyse_migration(build([("K1", 0, 10, 0)]), build([("N1", 0, 10, 0)]), 100, 99)
