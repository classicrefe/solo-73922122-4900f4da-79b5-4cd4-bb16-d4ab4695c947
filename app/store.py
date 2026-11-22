"""核查会话与下载结果的短期内存存储。

首次核查成功后，会话保存两版桩号表和全线判重索引，复查只换旧号范围，
不必重新上传。下载 CSV 仍按“每次核查一个令牌”保存，各自对应各自的范围与
结果；所有令牌都归属于某个会话。

会话过期/被挤占时，一并回收它的全部复查数据（会话对象和下载令牌）；
再用旧链接访问会提示重新上传。进程重启后同样需要重新核查。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from .session import AnalysisSession


@dataclass(frozen=True)
class StoredSession:
    session_id: str
    created_at: float
    session: AnalysisSession
    old_name: str
    new_name: str


@dataclass(frozen=True)
class StoredResult:
    token: str
    created_at: float
    csv_text: str
    filename: str
    summary: dict[str, object]
    session_id: str | None = None


class ResultStore:
    """会话与下载结果的短期缓存，线程安全。

    ``ttl_seconds`` 从创建时刻起算，会话及其复查数据同时失效。
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_entries: int = 200,
        max_sessions: int = 50,
    ):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, StoredSession] = OrderedDict()
        self._items: OrderedDict[str, StoredResult] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    # -- 内部回收 ---------------------------------------------------------
    def _drop_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        for token in [
            token
            for token, item in self._items.items()
            if item.session_id == session_id
        ]:
            self._items.pop(token, None)

    def _purge(self, now: float) -> None:
        expired_sessions = [
            sid
            for sid, item in self._sessions.items()
            if now - item.created_at > self._ttl
        ]
        for sid in expired_sessions:
            self._drop_session(sid)
        while len(self._sessions) > self._max_sessions:
            oldest = next(iter(self._sessions))
            self._drop_session(oldest)
        expired = [
            token
            for token, item in self._items.items()
            if now - item.created_at > self._ttl
        ]
        for token in expired:
            self._items.pop(token, None)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    # -- 会话 -------------------------------------------------------------
    def create_session(
        self, session: AnalysisSession, old_name: str, new_name: str
    ) -> StoredSession:
        now = time.time()
        stored = StoredSession(
            session_id=secrets.token_urlsafe(16),
            created_at=now,
            session=session,
            old_name=old_name,
            new_name=new_name,
        )
        with self._lock:
            self._purge(now)
            self._sessions[stored.session_id] = stored
        return stored

    def get_session(self, session_id: str) -> StoredSession | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            return self._sessions.get(session_id)

    # -- 下载结果 ---------------------------------------------------------
    def put(
        self,
        csv_text: str,
        filename: str,
        summary: dict[str, object],
        session_id: str | None = None,
    ) -> StoredResult | None:
        """保存一份下载结果；指定会话但会话已失效时返回 None。"""
        now = time.time()
        token = secrets.token_urlsafe(16)
        with self._lock:
            self._purge(now)
            if session_id is not None and session_id not in self._sessions:
                return None
            item = StoredResult(
                token=token,
                created_at=now,
                csv_text=csv_text,
                filename=filename,
                summary=summary,
                session_id=session_id,
            )
            self._items[token] = item
            return item

    def get(self, token: str) -> StoredResult | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            item = self._items.get(token)
            if item is None:
                return None
            if item.session_id is not None and item.session_id not in self._sessions:
                # 防御：会话失效时不应再留下孤儿下载（正常由 _purge 回收）。
                self._items.pop(token, None)
                return None
            return item

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._items.clear()
