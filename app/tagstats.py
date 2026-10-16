"""标签计数缓存：让 /api/tags（标签面板 / 自动补全）从 30 秒降到毫秒级。

为什么需要：
    /api/tags 要显示「每个标签有多少媒体」。实时算 = 对 715 万行 media_tags 做
    GROUP BY + 排序，实测 33 秒（前端一打开标签面板就调它）。

怎么做：
    把聚合结果预计算到 tag_counts 小表（约 1 万行，一次覆盖索引聚合约 0.3 秒），
    查询时 JOIN 这张小表即可。

安全边界：
    - 计数**只用于显示与排序**，短暂滞后无害，绝不参与删除/丢失判定；
    - 全量重算在一个独立写事务里完成，读者不会看到"半张表"；
    - 重算放后台线程，不阻塞请求；首次为空时同步等一次（0.3 秒 << 33 秒）。
"""
from __future__ import annotations

import logging
import threading
import time

from .database import query_one, transaction

logger = logging.getLogger("imagedb.tagstats")

TTL_SEC = 60.0
FIRST_WAIT_SEC = 10.0
COALESCE_SEC = 0.2       # Bounded delay batches bursts without starving continuous edits.

_lock = threading.Lock()
_build_lock = threading.Lock()
_idle = threading.Event()
_idle.set()
_rebuilding = False
_last_rebuild = 0.0
_generation = 0
_built_generation = -1
_initialized = False


def _rebuild_sql(conn) -> int:
    conn.execute("DELETE FROM tag_counts")
    conn.execute(
        "INSERT INTO tag_counts(tag_id, media_count, updated_at) "
        "SELECT tag_id, COUNT(*), datetime('now','localtime') "
        "FROM media_tags GROUP BY tag_id"
    )
    return conn.execute("SELECT COUNT(*) AS c FROM tag_counts").fetchone()[0]



def rebuild_now() -> int:
    """Synchronous rebuild. Only acknowledge changes captured before this build.

    The build lock serializes SQL + publication with other synchronous/background
    builders; a mark during either phase stays pending, even if SQL included it.
    """
    global _last_rebuild, _built_generation, _initialized
    with _build_lock:
        with _lock:
            generation = _generation
        n = transaction(_rebuild_sql)
        with _lock:
            _last_rebuild = time.monotonic()
            _built_generation = generation
            _initialized = True
    logger.info("标签计数缓存已重建：%d 个标签", n)
    return n


def _worker(delay: float = 0.0) -> None:
    global _rebuilding
    try:
        while True:
            if delay:
                time.sleep(delay)
            rebuild_now()
            with _lock:
                if _built_generation == _generation:
                    # Check and release atomically: a later mark will start a worker.
                    _rebuilding = False
                    _idle.set()
                    return
            delay = COALESCE_SEC
    except Exception as exc:  # noqa: BLE001
        logger.warning("标签计数缓存重建失败（沿用旧值）：%s", exc)
        with _lock:
            # Do not acknowledge the generation on failure. Next access retries.
            _rebuilding = False
            _idle.set()


def _kick(delay: float = 0.0) -> bool:
    """Start at most one background builder (including its coalescing wait)."""
    global _rebuilding
    with _lock:
        if _rebuilding:
            return False
        _rebuilding = True
        _idle.clear()
    try:
        threading.Thread(target=_worker, args=(delay,), daemon=True, name="tagcounts").start()
    except Exception:
        with _lock:
            _rebuilding = False
            _idle.set()
        raise
    return True


def warm() -> None:
    """Warm in the background without blocking startup."""
    _kick()


def mark_dirty() -> None:
    """Record every committed change; coalesce bursts and never clear a newer mark."""
    global _generation
    with _lock:
        _generation += 1
    _kick(delay=COALESCE_SEC)


def ensure_fresh(ttl: float = TTL_SEC) -> None:
    """Wait once for a genuinely uninitialized empty cache; otherwise refresh async."""
    with _lock:
        initialized = _initialized
        stale = (_built_generation != _generation or
                 time.monotonic() - _last_rebuild > ttl)
    first_empty = False
    if not initialized:
        try:
            row = query_one("SELECT COUNT(*) AS c FROM tag_counts")
        except Exception:  # noqa: BLE001
            return
        first_empty = not row or row["c"] == 0
    if stale or first_empty:
        _kick()
    if first_empty:
        _idle.wait(FIRST_WAIT_SEC)
