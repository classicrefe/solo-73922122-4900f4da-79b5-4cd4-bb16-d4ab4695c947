"""复查会话：复用两版桩号表与全线判重索引。

首次核查成功后，把两版表、全线判重索引（旧表里程重叠、新表桩号重叠）和
提示信息放进 ``AnalysisSession``，之后在结果页换旧号范围时直接复用，无需
重新上传或重新解析。

切分边界按“旧段”惰性准备：某个旧段第一次被查询命中时才计算它的边界，
结果缓存在会话里；换查同一段的另一截时直接命中缓存。同一旧段被并发首次
命中时只准备一次，所有等待者共享同一个成败结论。失败不会被永久缓存：
并发等待者都拿到原因后，该段的下一次提交会重新尝试。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from .analysis import BoundaryIndex, Segment, SegmentTable, table_warnings


class SegmentPreparationError(RuntimeError):
    """旧段切分边界准备失败；消息可直接展示给用户。"""


@dataclass
class _Preparation:
    """一个旧段的单飞准备状态。"""

    condition: threading.Condition = field(default_factory=threading.Condition)
    done: bool = False
    boundaries: tuple[int, ...] | None = None
    error: SegmentPreparationError | None = None


class AnalysisSession:
    """一次核查的会话数据（不含任何单次查询范围）。"""

    def __init__(self, old_table: SegmentTable, new_table: SegmentTable):
        self.old_table = old_table
        self.new_table = new_table
        # 全线判重索引只构建一次，后续所有复查共用。
        self.boundary_index = BoundaryIndex.build(old_table, new_table)
        self.warnings: list[str] = table_warnings(old_table) + table_warnings(new_table)

        self._ready: dict[Segment, tuple[int, ...]] = {}
        self._inflight: dict[Segment, _Preparation] = {}
        self._lock = threading.Lock()
        self._prepared_count = 0
        self._failed_count = 0

    @property
    def prepared_count(self) -> int:
        """真正执行过（不命中缓存的）边界准备次数，用于测试/诊断。"""
        with self._lock:
            return self._prepared_count

    @property
    def failed_count(self) -> int:
        with self._lock:
            return self._failed_count

    @property
    def prepared_segments(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(seg.segment_no for seg in self._ready)

    def boundaries_for(self, old_seg: Segment) -> tuple[int, ...]:
        """返回旧段边界；必要时惰性准备，并保证同一段只准备一次。

        成功结果永久缓存在会话内；失败结果只交给“本次准备”的所有并发
        等待者，不写入缓存，因此下一次提交会重新准备。
        """
        leader_entry: _Preparation | None = None
        with self._lock:
            cached = self._ready.get(old_seg)
            if cached is not None:
                return cached
            entry = self._inflight.get(old_seg)
            if entry is None:
                entry = _Preparation()
                self._inflight[old_seg] = entry
                leader_entry = entry

        if leader_entry is not None:
            return self._prepare(old_seg, leader_entry)

        # 跟随者：等同一段的首次准备结束，共享它的成败结论。
        with entry.condition:
            while not entry.done:
                entry.condition.wait()
        if entry.error is not None:
            # 同一个异常对象，保证并发等待者得到完全一致的结论。
            raise entry.error
        assert entry.boundaries is not None
        return entry.boundaries

    def _prepare(self, old_seg: Segment, entry: _Preparation) -> tuple[int, ...]:
        try:
            boundaries = tuple(self.boundary_index.boundaries_for(old_seg))
        except BaseException as exc:  # noqa: BLE001 - 必须唤醒等待者，避免永久挂起
            error = SegmentPreparationError(
                f"旧段 {old_seg.segment_no} 的切分边界准备失败：{exc}"
            )
            with self._lock:
                self._failed_count += 1
                self._finish(old_seg, entry, error=error)
            if isinstance(exc, Exception):
                raise error from exc
            raise  # KeyboardInterrupt / SystemExit 等照常向上传播
        with self._lock:
            self._prepared_count += 1
            self._finish(old_seg, entry, boundaries=boundaries)
        return boundaries

    def _finish(
        self,
        old_seg: Segment,
        entry: _Preparation,
        *,
        boundaries: tuple[int, ...] | None = None,
        error: SegmentPreparationError | None = None,
    ) -> None:
        """在持有 ``self._lock`` 时结束一次准备并唤醒等待者。"""
        with entry.condition:
            if boundaries is not None:
                entry.boundaries = boundaries
                self._ready[old_seg] = boundaries
            entry.error = error
            entry.done = True
            entry.condition.notify_all()
        # 失败不常驻缓存：并发等待者已拿到同一原因，下一次提交重试。
        if self._inflight.get(old_seg) is entry:
            del self._inflight[old_seg]

    def prepare_all(self, segments: Iterable[Segment] | None = None) -> None:
        """预准备（可选）：按给定旧段或全表顺序准备边界。"""
        for seg in segments if segments is not None else self.old_table.segments:
            self.boundaries_for(seg)
