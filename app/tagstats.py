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

TTL_SEC = 60.0            # 缓存寿命（秒）：过期后由下一次访问触发后台刷新
FIRST_WAIT_SEC = 10.0     # 首次（空表）同步等待上限

_lock = threading.Lock()
_rebuilding = False
_last_rebuild = 0.0
_dirty = True


def _rebuild_sql(conn) -> int:
    conn.execute("DELETE FROM tag_counts")
    conn.execute(
        "INSERT INTO tag_counts(tag_id, media_count, updated_at) "
        "SELECT tag_id, COUNT(*), datetime('now','localtime') "
        "FROM media_tags GROUP BY tag_id"
    )
    return conn.execute("SELECT COUNT(*) AS c FROM tag_counts").fetchone()[0]


def rebuild_now() -> int:
    """同步全量重建（供测试/维护用），返回写入的标签数。"""
    n = transaction(_rebuild_sql)
    global _last_rebuild, _dirty
    with _lock:
        _last_rebuild = time.time()
        _dirty = False
    logger.info("标签计数缓存已重建：%d 个标签", n)
    return n


def _worker() -> None:
    global _rebuilding, _last_rebuild, _dirty
    try:
        n = transaction(_rebuild_sql)
        logger.info("标签计数缓存已重建：%d 个标签", n)
        with _lock:
            _last_rebuild = time.time()
            _dirty = False
    except Exception as exc:  # noqa: BLE001
        logger.warning("标签计数缓存重建失败（沿用旧值）：%s", exc)
    finally:
        with _lock:
            _rebuilding = False


def _kick() -> bool:
    """启动后台重算（已在跑则跳过）。返回是否真的启动了。"""
    global _rebuilding
    with _lock:
        if _rebuilding:
            return False
        _rebuilding = True
    threading.Thread(target=_worker, daemon=True, name="tagcounts").start()
    return True


def warm() -> None:
    """启动时预热：直接丢后台重建，不阻塞。"""
    _kick()


def mark_dirty() -> None:
    """标签数据发生变化（增删标签 / 删除媒体 / 打标完成等）→ 立即后台重算。"""
    global _dirty
    with _lock:
        _dirty = True
    _kick()


def ensure_fresh(ttl: float = TTL_SEC) -> None:
    """确保计数缓存可用：空表 → 同步等一次；过期/脏 → 后台刷新（不阻塞）。"""
    try:
        row = query_one("SELECT COUNT(*) AS c FROM tag_counts")
    except Exception:  # noqa: BLE001
        return
    n = row["c"] if row else 0
    if n == 0:
        # 首次：同步等重建完成，避免面板上所有计数都显示 0
        _kick()
        deadline = time.time() + FIRST_WAIT_SEC
        while time.time() < deadline:
            with _lock:
                if not _rebuilding:
                    return
            time.sleep(0.05)
        return
    with _lock:
        stale = _dirty or (time.time() - _last_rebuild) > ttl
    if stale:
        _kick()
