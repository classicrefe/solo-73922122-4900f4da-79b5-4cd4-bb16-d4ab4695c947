"""复查会话：惰性边界准备、单飞并发、失败重试、会话回收。"""

from __future__ import annotations

import threading
import time

from app.analysis import Segment, SegmentTable, analyse_migration
from app.session import AnalysisSession, SegmentPreparationError
from app.store import ResultStore


def build(rows, label="表"):
    return SegmentTable.build(
        [Segment(no, sm, em, sc) for no, sm, em, sc in rows], label=label
    )


OLD = [
    ("K1", 0, 500, 1000),
    ("K2", 500, 900, 1500),
    ("K3", 900, 1000, 1850),
    ("K4", 1200, 1600, 2100),
]
NEW = [
    ("N1", 0, 400, 2000),
    ("N2", 400, 850, 3000),
    ("N3", 850, 950, 5000),
    ("N4", 1000, 1050, 2300),
    ("N5", 1200, 1600, 6000),
]


def make_session() -> AnalysisSession:
    return AnalysisSession(build(OLD, "旧"), build(NEW, "新"))


class CountingIndex:
    """包装真实索引，统计每个旧段被真正准备了多少次，可注入延迟/失败。"""

    def __init__(self, real, delay=0.0, fail_for=()):
        self.real = real
        self.calls = {}
        self.delay = delay
        self.fail_for = set(fail_for)
        self.lock = threading.Lock()

    def boundaries_for(self, old_seg):
        with self.lock:
            self.calls[old_seg.segment_no] = self.calls.get(old_seg.segment_no, 0) + 1
        if self.delay:
            time.sleep(self.delay)
        if old_seg.segment_no in self.fail_for:
            raise RuntimeError("模拟边界准备失败")
        return self.real.boundaries_for(old_seg)


def k1(session):
    return next(s for s in session.old_table.segments if s.segment_no == "K1")


def test_boundary_prepared_lazily_and_reused_for_other_slice():
    session = make_session()
    counting = CountingIndex(session.boundary_index)
    session.boundary_index = counting

    # K1 第一次命中
    first = session.boundaries_for(k1(session))
    assert counting.calls == {"K1": 1}
    # 换查 K1 另一截，边界直接复用，不再准备
    second = session.boundaries_for(k1(session))
    assert counting.calls == {"K1": 1}
    assert first is second
    # 查询其它段不会触发 K1 重算
    session.boundaries_for(next(s for s in session.old_table.segments if s.segment_no == "K2"))
    assert counting.calls == {"K1": 1, "K2": 1}
    assert session.prepared_count == 2


def test_concurrent_first_hit_prepares_once_and_shares_success():
    session = make_session()
    counting = CountingIndex(session.boundary_index, delay=0.15)
    session.boundary_index = counting
    seg = k1(session)

    results: list[tuple[int, ...]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(session.boundaries_for(seg))
        except BaseException as exc:  # pragma: no cover - 失败即测试失败
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert counting.calls == {"K1": 1}, "并发首次命中应只准备一次"
    assert session.prepared_count == 1
    assert len(results) == 8
    assert all(r == results[0] for r in results)


def test_concurrent_first_hit_shares_same_failure_then_retries():
    session = make_session()
    counting = CountingIndex(session.boundary_index, delay=0.15, fail_for={"K1"})
    session.boundary_index = counting
    seg = k1(session)

    errors: list[SegmentPreparationError] = []
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        try:
            session.boundaries_for(seg)
        except SegmentPreparationError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 一次准备，所有并发请求拿到同一个异常对象/同一结论
    assert counting.calls == {"K1": 1}
    assert len(errors) == 5
    assert all(err is errors[0] for err in errors)
    assert "K1" in str(errors[0]) and "失败" in str(errors[0])
    assert session.failed_count == 1

    # 失败不常驻：再次提交会重新尝试；这次恢复成功
    counting.fail_for.clear()
    boundaries = session.boundaries_for(seg)
    assert counting.calls == {"K1": 2}
    assert boundaries == session.boundary_index.real.boundaries_for(seg)


def test_failure_in_one_segment_does_not_block_ready_segments():
    session = make_session()
    counting = CountingIndex(session.boundary_index, fail_for={"K3"})
    session.boundary_index = counting
    # 先准备 K1，K3 不参与
    k1_bounds = session.boundaries_for(k1(session))
    assert session.prepared_segments == ("K1",)
    # 只涉及已就绪段的复查可照常完成
    result = analyse_migration(
        session.old_table,
        session.new_table,
        1000,
        1499,
        session.boundaries_for,
    )
    assert sum(row.count for row in result.rows) == 500
    # 未触碰会失败的 K3
    assert counting.calls == {"K1": 1}
    assert k1_bounds


def test_store_expiry_reclaims_session_and_all_recheck_downloads():
    store = ResultStore(ttl_seconds=60)
    session = make_session()
    stored = store.create_session(session, "old.csv", "new.csv")
    first = store.put("a\r\n", "a.csv", {}, session_id=stored.session_id)
    second = store.put("b\r\n", "b.csv", {}, session_id=stored.session_id)
    assert first and second
    assert store.get(first.token) is first
    assert store.get_session(stored.session_id) is stored

    # 模拟过期
    store._ttl = -1
    assert store.get_session(stored.session_id) is None
    # 复查数据（全部下载令牌）一并回收
    assert store.get(first.token) is None
    assert store.get(second.token) is None
    assert store._items == {}
    assert store._sessions == {}


def test_store_evicts_oldest_session_with_its_results():
    store = ResultStore(ttl_seconds=60, max_sessions=1)
    s1 = store.create_session(make_session(), "a.csv", "b.csv")
    r1 = store.put("x", "x.csv", {}, session_id=s1.session_id)
    s2 = store.create_session(make_session(), "c.csv", "d.csv")
    r2 = store.put("y", "y.csv", {}, session_id=s2.session_id)
    assert store.get_session(s2.session_id) is s2
    assert store.get_session(s1.session_id) is None
    assert r1 and store.get(r1.token) is None
    assert r2 and store.get(r2.token) is r2


def test_put_rejects_unknown_session():
    store = ResultStore()
    assert store.put("x", "x.csv", {}, session_id="nope") is None
