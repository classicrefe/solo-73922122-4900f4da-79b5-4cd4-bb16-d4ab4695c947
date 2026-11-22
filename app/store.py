"""核查结果临时存储。

分析结果只在下载 CSV 时需要，使用短期内存缓存保存，避免把大表放进 URL 或
反复上传。进程重启或过期后需重新核查。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredResult:
    token: str
    created_at: float
    csv_text: str
    filename: str
    summary: dict[str, object]


class ResultStore:
    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 200):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._items: OrderedDict[str, StoredResult] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        expired = [token for token, item in self._items.items() if now - item.created_at > self._ttl]
        for token in expired:
            self._items.pop(token, None)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def put(self, csv_text: str, filename: str, summary: dict[str, object]) -> StoredResult:
        now = time.time()
        token = secrets.token_urlsafe(16)
        item = StoredResult(
            token=token,
            created_at=now,
            csv_text=csv_text,
            filename=filename,
            summary=summary,
        )
        with self._lock:
            self._purge(now)
            self._items[token] = item
        return item

    def get(self, token: str) -> StoredResult | None:
        now = time.time()
        with self._lock:
            self._purge(now)
            return self._items.get(token)
