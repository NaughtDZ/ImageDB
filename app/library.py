# -*- coding: utf-8 -*-
"""
目录库管理模块
==============
职责：
    1. 导入用户指定目录：递归扫描，把图片/视频路径与目录结构写入数据库；
    2. 重新扫描：把磁盘上新增的文件补录进来；
    3. 缺失校验：检查数据库记录对应的文件/目录是否还在磁盘上，
       不存在的自动从数据库删除（前台手动触发 + 后台定时触发）；
    4. 构建目录树：启动时优先从数据库读取（不访问磁盘，速度快）。

设计说明：
    - 目录树完全由数据库驱动，磁盘只作为“真实存在性”的校验来源；
    - 导入时只写路径（不做缩略图、不打标），打标由用户手动选择后触发，
      符合“先入库、后打标”的需求。
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
import time

from .database import (chunk_ids, delete, execute, execute_rowcount, executemany,
                      query_all, query_one)
from .imagetag import is_sidecar

logger = logging.getLogger("imagedb.library")

# ---- 根目录可达性检查：TTL 缓存 + 超时保护 ----
# 网络盘/移动盘未挂载或掉线时，os.path.isdir 可能阻塞数秒甚至更久。
# 因此把探测放到线程里跑并设超时（超时视为不可达 = 离线，绝不阻塞请求），
# 再用 TTL 缓存避免每次 /api/tree 都去碰磁盘。
_ROOT_TTL = 20.0        # 缓存有效期（秒）
_ROOT_TIMEOUT = 1.5     # 单次探测超时（秒）
_root_cache: dict[str, tuple[float, bool]] = {}
_probe_pool = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="fsprobe")


def _isdir_fast(path: str, ttl: float = _ROOT_TTL, timeout: float = _ROOT_TIMEOUT) -> bool:
    """带 TTL 缓存 + 超时保护的目录存在性检查（用于根目录这种少量探测）。

    超时或异常一律返回 False（按不可达处理）——宁可显示「离线」，也不阻塞界面。
    """
    if not path:
        return False
    now = time.monotonic()
    hit = _root_cache.get(path)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        val = bool(_probe_pool.submit(os.path.isdir, path).result(timeout=timeout))
    except Exception:  # noqa: BLE001 - 超时/异常 → 视为不可达
        logger.debug("根目录探测超时/异常，按不可达处理：%s", path)
        val = False
    _root_cache[path] = (now, val)
    return val


def invalidate_root_cache() -> None:
    """清空根目录可达性缓存（扫描/重扫后可调用，让状态立即刷新）。"""
    _root_cache.clear()

# 支持的图片扩展名
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".svg", ".ico", ".avif", ".heic", ".heif",
}
# 支持的视频扩展名
VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
    ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".rmvb",
}


def ext_of(path: str) -> str:
    """返回文件扩展名（小写，不含点）。"""
    return os.path.splitext(path)[1].lower().lstrip(".")


def media_type_of(path: str) -> str | None:
    """根据扩展名判断媒体类型，非媒体文件返回 None。"""
    ext = "." + ext_of(path)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def _insert_folder(name: str, path: str, parent_id: int | None, is_root: int = 0) -> int:
    """插入目录记录（已存在则返回已有 id）。"""
    row = query_one("SELECT id, is_root FROM folders WHERE path = ?", (path,))
    if row:
        # 已存在：提升为根目录标记（幂等），保留父目录关系
        if is_root and not row["is_root"]:
            execute("UPDATE folders SET is_root = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    return execute(
        "INSERT INTO folders(name, path, parent_id, is_root) VALUES (?, ?, ?, ?)",
        (name, path, parent_id, is_root),
    )


def _walk_and_insert(dir_path: str, parent_db_id: int, seen: set[str]) -> tuple[int, int]:
    """
    递归扫描目录并写入数据库。
    返回 (新增目录数, 新增媒体数)。
    seen 用于避免符号链接造成的死循环。
    """
    folders_added = 0
    media_added = 0
    try:
        entries = list(os.scandir(dir_path))
    except OSError as exc:
        logger.warning("无法读取目录 %s：%s", dir_path, exc)
        return 0, 0

    media_rows: list[tuple] = []
    subdirs: list[os.DirEntry] = []
    for entry in entries:
        if is_sidecar(entry.name):
            continue   # 跳过 .imgtag / .txttag 等标签侧车
        try:
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(entry)
            elif entry.is_file(follow_symlinks=False):
                mtype = media_type_of(entry.path)
                if mtype:
                    st = entry.stat(follow_symlinks=False)
                    media_rows.append((
                        parent_db_id, entry.path, entry.name, mtype,
                        ext_of(entry.path), st.st_size, st.st_mtime,
                    ))
        except OSError:
            continue

    # 批量写入媒体记录（INSERT OR IGNORE：已存在的路径不重复插入）
    # 注意：media_added 用「实际插入行数」而非「扫描到的文件数」，否则「新增 N」会虚高
    if media_rows:
        media_added = executemany(
            """INSERT OR IGNORE INTO media_items
               (folder_id, path, filename, type, ext, size, mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            media_rows,
        )

    # 递归子目录
    for entry in subdirs:
        real = os.path.realpath(entry.path)
        if real in seen:
            continue
        seen.add(real)
        sub_id = _insert_folder(entry.name, entry.path, parent_db_id)
        folders_added += 1
        a, b = _walk_and_insert(entry.path, sub_id, seen)
        folders_added += a
        media_added += b
    return folders_added, media_added


def count_media_files(root_path: str) -> tuple[int, int]:
    """
    多线程快速统计目录树：返回 (子目录数, 媒体文件数)。
    只做 scandir 遍历，不写数据库，速度快，用于导入前的进度预估。
    """
    import concurrent.futures as cf
    total_dirs = [0]
    total_files = [0]
    seen = {os.path.realpath(root_path)}

    def count_dir(dir_path: str) -> tuple[int, int]:
        """统计单个目录：返回 (子目录数, 媒体文件数)。"""
        dirs = 0
        files = 0
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if is_sidecar(entry.name):
                        continue   # 跳过 .imgtag / .txttag
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs += 1
                        elif entry.is_file(follow_symlinks=False):
                            if media_type_of(entry.path):
                                files += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return dirs, files

    # 第一遍：收集所有目录路径（单线程收集，避免符号链接循环）
    all_dirs = [root_path]
    queue = [root_path]
    while queue:
        cur = queue.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if is_sidecar(entry.name):
                        continue   # 跳过 .imgtag / .txttag
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            real = os.path.realpath(entry.path)
                            if real in seen:
                                continue
                            seen.add(real)
                            all_dirs.append(entry.path)
                            queue.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue

    # 第二遍：多线程并行统计每个目录的文件数
    workers = min(16, max(4, (os.cpu_count() or 4) * 2))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(count_dir, all_dirs))
    for d, f in results:
        total_dirs[0] += d
        total_files[0] += f
    return total_dirs[0], total_files[0]


def _find_nearest_ancestor(path: str) -> tuple[str, int] | None:
    """从 path 的父目录起向上，找最近一个已导入的祖先目录。

    返回 (祖先路径, 祖先目录 id)；若没有已导入祖先则返回 None。
    用于"导入目录若落在某个已导入目录之下，则自动复用其作为父目录"。
    """
    parent = os.path.dirname(os.path.abspath(path))
    while parent and parent != os.path.dirname(parent):
        row = query_one("SELECT id, path FROM folders WHERE path = ?", (parent,))
        if row:
            return row["path"], row["id"]
        parent = os.path.dirname(parent)
    return None


def _prepare_parent_for_import(path: str) -> tuple[int | None, int]:
    """为导入的根目录确定父目录 id（并补建缺失的中间目录链）。

    返回 (parent_id, is_root)：
    - 若 path 的某级祖先目录已在库中（如已导入父目录），则把最近祖先作为父目录，
      并沿路径从上到下补建缺失的中间目录（is_root=0）。返回 (最近父目录 id, 0)。
    - 若没有任何已导入祖先，则作为独立顶层根目录，返回 (None, 1)。
    性能说明：仅在根导入时执行，祖先查找为逐级索引查询（约等于路径深度次，通常 1~2 次）；
    对子目录逐个插入的原有精确路径查询开销不变，因此不会随子目录数量放大。
    """
    root = os.path.abspath(path)
    hit = _find_nearest_ancestor(root)
    if not hit:
        return None, 1
    anc_path, anc_id = hit
    parent_path = os.path.dirname(root)   # 导入目录的父目录（挂载点）
    # 收集 anc_path 与 parent_path 之间缺失的中间目录（不含 anc_path，含 parent_path），自上而下补建
    chain: list[str] = []
    cur = parent_path
    while cur and os.path.normcase(os.path.normpath(cur)) != os.path.normcase(os.path.normpath(anc_path)):
        chain.append(cur)
        nxt = os.path.dirname(cur)
        if nxt == cur:   # 已到根，安全退出
            break
        cur = nxt
    chain.reverse()
    parent_id = anc_id
    for d in chain:
        row = query_one("SELECT id FROM folders WHERE path = ?", (d,))
        if row:
            parent_id = row["id"]
        else:
            parent_id = _insert_folder(os.path.basename(d) or d, d, parent_id)
    return parent_id, 0


def import_folder(root_path: str) -> dict:
    """
    导入一个目录（及其全部子目录）：
    - 先把目录树写入 folders 表；
    - 再把其中的图片/视频路径写入 media_items 表。
    返回统计信息。目录不存在时抛 ValueError。
    """
    root_path = os.path.abspath(root_path)
    if not os.path.isdir(root_path):
        raise ValueError(f"目录不存在：{root_path}")

    parent_id, is_root = _prepare_parent_for_import(root_path)
    root_id = _insert_folder(
        os.path.basename(root_path.rstrip(os.sep)) or root_path,
        root_path, parent_id, is_root=is_root,
    )
    logger.info("开始导入目录：%s", root_path)

    seen: set[str] = {os.path.realpath(root_path)}
    folders_added, media_added = _walk_and_insert(root_path, root_id, seen)

    # 重新统计该根目录下的媒体总数
    total = query_one(
        """SELECT COUNT(*) AS c FROM media_items
           WHERE folder_id IN (
               WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               ) SELECT id FROM sub)""",
        (root_id,),
    )["c"]
    logger.info("导入完成：目录 %d 个，新增媒体 %d 个，库内总计 %d 个",
                folders_added, media_added, total)
    return {"folder_id": root_id, "folders_added": folders_added,
            "media_added": media_added, "media_total": total}


def import_folder_progress(root_path: str, progress_cb=None) -> dict:
    """
    带进度回调的导入：先插入根目录，再递归扫描写入，每处理完一个目录
    调用 progress_cb(done_count) 更新进度。progress_cb 接收已处理的媒体数。
    返回与 import_folder 相同的统计信息。
    """
    root_path = os.path.abspath(root_path)
    if not os.path.isdir(root_path):
        raise ValueError(f"目录不存在：{root_path}")

    parent_id, is_root = _prepare_parent_for_import(root_path)
    root_id = _insert_folder(
        os.path.basename(root_path.rstrip(os.sep)) or root_path,
        root_path, parent_id, is_root=is_root,
    )
    logger.info("开始导入目录：%s", root_path)

    done_counter = {"n": 0}
    folders_added = 0
    media_added = 0

    def walk(dir_path, parent_db_id):
        """递归扫描写入（带进度回调）。"""
        nonlocal folders_added, media_added
        try:
            entries = list(os.scandir(dir_path))
        except OSError as exc:
            logger.warning("无法读取目录 %s：%s", dir_path, exc)
            return
        media_rows: list[tuple] = []
        subdirs: list[os.DirEntry] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    mtype = media_type_of(entry.path)
                    if mtype:
                        st = entry.stat(follow_symlinks=False)
                        media_rows.append((
                            parent_db_id, entry.path, entry.name, mtype,
                            ext_of(entry.path), st.st_size, st.st_mtime,
                        ))
            except OSError:
                continue
        if media_rows:
            media_added += executemany(
                """INSERT OR IGNORE INTO media_items
                   (folder_id, path, filename, type, ext, size, mtime)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                media_rows,
            )
            done_counter["n"] += len(media_rows)
            if progress_cb:
                progress_cb(done_counter["n"])

        for entry in subdirs:
            real = os.path.realpath(entry.path)
            if real in seen_set:
                continue
            seen_set.add(real)
            sub_id = _insert_folder(entry.name, entry.path, parent_db_id)
            folders_added += 1
            walk(entry.path, sub_id)

    seen_set: set[str] = {os.path.realpath(root_path)}
    walk(root_path, root_id)

    total = query_one(
        """SELECT COUNT(*) AS c FROM media_items
           WHERE folder_id IN (
               WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               ) SELECT id FROM sub)""",
        (root_id,),
    )["c"]
    logger.info("导入完成：目录 %d 个，新增媒体 %d 个，库内总计 %d 个",
                folders_added, media_added, total)
    return {"folder_id": root_id, "folders_added": folders_added,
            "media_added": media_added, "media_total": total}


def _subtree_folder_ids(root_id: int) -> list[int]:
    """递归获取某目录（含自身）的全部子目录 id。"""
    rows = query_all(
        """WITH RECURSIVE sub(id) AS (
               SELECT id FROM folders WHERE id = ?
               UNION ALL
               SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
           ) SELECT id FROM sub""",
        (root_id,),
    )
    return [r["id"] for r in rows]


def _root_of(folder_id: int) -> int | None:
    """沿 parent_id 上溯到根目录（is_root=1）的 id；无根祖先返回 None。"""
    cur: int | None = folder_id
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = query_one("SELECT id, parent_id, is_root FROM folders WHERE id = ?", (cur,))
        if row is None:
            return None
        if row["is_root"]:
            return row["id"]
        cur = row["parent_id"]
    return None


def root_offline(root_id: int) -> bool:
    """根目录是否不可达（盘没挂/被拔/目录被删）→ 视为「离线」，此时不做丢失标记。

    这样把「盘没挂」与「文件真被删」区分开，避免拔盘就把整库标成 missing。
    """
    row = query_one("SELECT path FROM folders WHERE id = ?", (root_id,))
    return bool(row) and not _isdir_fast(row["path"])


def _count_media_in(folder_ids: list[int]) -> int:
    """统计若干目录下的媒体数量。"""
    if not folder_ids:
        return 0
    ph = ",".join("?" * len(folder_ids))
    row = query_one(f"SELECT COUNT(*) AS c FROM media_items WHERE folder_id IN ({ph})", folder_ids)
    return row["c"] if row else 0


# ---------------- 丢失判定：三态（present / absent / unknown） ----------------
# 核心原则：**只有「确认不存在」才允许标记丢失**。
# 权限不足、网络抖动、盘掉线等一律算 'unknown'（未知）→ 不写库、不改状态。
# 历史教训：旧代码用 os.path.isfile() 判定，它会把任何异常都吞成 False，
# 于是「读不到」被当成「文件没了」，浏览一遍就会把好文件误标为丢失。


def probe_file(path: str) -> str:
    """文件三态：'present'（在）/ 'absent'（确认不在）/ 'unknown'（读不到，无法确认）。"""
    try:
        os.stat(path)
        return "present"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"


def probe_dir(path: str) -> str:
    """目录三态：'present' / 'absent' / 'unknown'（含义同上）。"""
    try:
        os.stat(path)
        return "present"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"


def _mark_all_missing(folder_ids: list[int], reason: str = "mark") -> int:
    """把若干目录下的所有媒体标记为 missing（**不删除记录**）。返回被标记的数量。

    仅用于「目录确认不存在」这种明确情形（重扫时发现目录没了）。
    """
    if not folder_ids:
        return 0
    n = 0
    for chunk in chunk_ids(folder_ids):
        ph = ",".join("?" * len(chunk))
        n += execute_rowcount(
            "UPDATE media_items SET status = 'missing',"
            " status_at = datetime('now','localtime'), status_reason = ?"
            f" WHERE folder_id IN ({ph}) AND (status IS NULL OR status != 'missing')",
            [reason] + list(chunk),
        )
    return n


def _sync_dir_status(folder_db_id: int, dir_path: str, reason: str = "rescan:not_found"
                     ) -> tuple[int, int, int]:
    """按磁盘实际目录项，把该目录下媒体在 ok/missing 间同步（**不删除记录**）。

    返回 (新标记为 missing 数, 恢复为 ok 数, 是否未知)。
    目录读不到（scandir 抛 OSError）→ 返回 (0, 0, 1)，**一条都不标记**。
    用 scandir 目录项比对而不是 os.path.isfile：天然区分「读不到目录」与「文件不在」。
    """
    try:
        on_disk = {e.name for e in os.scandir(dir_path)}
    except OSError:
        return 0, 0, 1
    miss = 0
    ok = 0
    for item in query_all("SELECT id, filename, status FROM media_items WHERE folder_id = ?",
                          (folder_db_id,)):
        want = "ok" if item["filename"] in on_disk else "missing"
        if (item["status"] or "ok") != want:
            execute(
                "UPDATE media_items SET status = ?,"
                " status_at = datetime('now','localtime'), status_reason = ? WHERE id = ?",
                (want, reason if want == "missing" else "rescan:present", item["id"]),
            )
            if want == "missing":
                miss += 1
            else:
                ok += 1
    return miss, ok, 0


def rescan_folder(folder_id: int) -> dict:
    """重新扫描某个目录子树（**用户显式操作** / 只标记、不删除）：
    - 磁盘上新增的目录/媒体 → 补录；
    - 磁盘上已消失的媒体 → 标记 status='missing'（保留记录/标签/缩略图）；
    - 之前 missing 现在又出现的 → 标回 'ok'；
    - 目录本身确认不存在 → 其下媒体全部标记 missing（不删目录、不删记录）；
    - **目录读不到（权限/网络/盘掉线）→ 计为 dirs_unknown，一条都不标记**。

    这是**唯一**会把记录标记为丢失的地方（另一处是用户主动删除）。
    返回 {added, missing, recovered, dir_missing, root_offline, dirs_unknown}。
    真正删除记录请用 purge_missing()（用户显式确认后）。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        raise ValueError(f"目录 id 不存在：{folder_id}")
    invalidate_root_cache()   # 用户主动扫描：重新探测根目录可达性
    # 根目录不可达（盘没挂/被拔）→ 视为「离线」，不做任何标记
    rid = _root_of(folder_id)
    if rid is not None and root_offline(rid):
        return {"added": 0, "missing": 0, "recovered": 0, "dirs_unknown": 0,
                "dir_missing": False, "root_offline": True}
    state = probe_dir(folder["path"])
    if state == "unknown":
        # 读不到 ≠ 不存在：不标记、不补录，交由用户稍后重试
        return {"added": 0, "missing": 0, "recovered": 0, "dirs_unknown": 1,
                "dir_missing": False, "root_offline": False}
    if state == "absent":
        missing = _mark_all_missing(_subtree_folder_ids(folder_id), "rescan:dir_missing")
        return {"added": 0, "missing": missing, "recovered": 0, "dirs_unknown": 0,
                "dir_missing": True, "root_offline": False}

    # 1. 补录磁盘上新增的目录与媒体
    seen: set[str] = {os.path.realpath(folder["path"])}
    _fa, added = _walk_and_insert(folder["path"], folder_id, seen)

    # 2. 逐目录同步 ok/missing（不删任何记录；读不到的目录跳过）
    missing = 0
    recovered = 0
    unknown = 0
    for f in _subtree_folder_ids(folder_id):
        frow = query_one("SELECT path FROM folders WHERE id = ?", (f,))
        if not frow:
            continue
        st = probe_dir(frow["path"])
        if st == "unknown":
            unknown += 1
            continue
        if st == "absent":
            missing += _mark_all_missing([f], "rescan:dir_missing")
            continue
        m, o, u = _sync_dir_status(f, frow["path"])
        missing += m
        recovered += o
        unknown += u

    return {"added": added, "missing": missing, "recovered": recovered,
            "dirs_unknown": unknown, "dir_missing": False, "root_offline": False}


def purge_missing(folder_id: int) -> dict:
    """删除某目录子树下所有 status='missing' 的媒体记录（**仅在用户显式确认后调用**）。

    同时清理这些记录的缩略图缓存。返回 {removed}。
    """
    ids = _subtree_folder_ids(folder_id)
    if not ids:
        return {"removed": 0}
    mids: list[int] = []
    for chunk in chunk_ids(ids):
        ph = ",".join("?" * len(chunk))
        mids.extend(r["id"] for r in query_all(
            f"SELECT id FROM media_items WHERE folder_id IN ({ph}) AND status = 'missing'", chunk))
    if not mids:
        return {"removed": 0}
    try:
        from . import media as media_service
        media_service.delete_thumbnails(mids)
    except Exception:  # noqa: BLE001
        pass
    removed = 0
    for chunk in chunk_ids(mids):
        ph = ",".join("?" * len(chunk))
        removed += delete(f"DELETE FROM media_items WHERE id IN ({ph})", chunk)
    logger.info("清理丢失记录：目录 %s 下删除 %d 条", folder_id, removed)
    return {"removed": removed}


def check_folder(folder_id: int) -> dict:
    """校验单个目录（用户点击树节点时调用）：**只报告、绝不改库**。

    - 所在根目录不可达 → root_offline=True；
    - 目录确认不存在 → exists=False（**不标记任何记录**，丢失标记只由重扫产生）；
    - 目录读不到（权限/网络）→ unknown=True，视作还在，避免误报「目录已丢失」。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        return {"exists": False, "missing": 0, "root_offline": False, "unknown": False}
    rid = _root_of(folder_id)
    if rid is not None and root_offline(rid):
        return {"exists": False, "missing": 0, "root_offline": True, "unknown": False}
    state = probe_dir(folder["path"])
    if state == "unknown":
        return {"exists": True, "missing": 0, "root_offline": False, "unknown": True}
    return {"exists": state == "present", "missing": 0,
            "root_offline": False, "unknown": False}


def recheck_missing(folder_id: int | None = None) -> dict:
    """把「标记为丢失、但实际还在」的记录恢复回 ok（**只恢复，不标记、不删除**）。

    只遍历 status='missing' 的记录（当前库里通常只有几十条），按目录聚合后
    每个目录只做一次 scandir，因此即使丢失记录很多也是秒级。
    目录读不到（权限/网络）→ 计入 unknown，保持原状态，绝不误判。
    返回 {checked, recovered, still_missing, unknown}。
    """
    if folder_id is None:
        rows = query_all(
            "SELECT id, path, filename FROM media_items WHERE status = 'missing'")
    else:
        ids = _subtree_folder_ids(folder_id)
        rows = []
        for chunk in chunk_ids(ids):
            ph = ",".join("?" * len(chunk))
            rows.extend(query_all(
                "SELECT id, path, filename FROM media_items"
                f" WHERE status = 'missing' AND folder_id IN ({ph})", chunk))
    if not rows:
        return {"checked": 0, "recovered": 0, "still_missing": 0, "unknown": 0}

    by_dir: dict[str, list] = {}
    for r in rows:
        by_dir.setdefault(os.path.dirname(r["path"]), []).append(r)

    recovered = 0
    still = 0
    unknown = 0
    for d, items in by_dir.items():
        try:
            names: set[str] | None = {e.name for e in os.scandir(d)}
        except OSError:
            names = None      # 读不到目录 → 未知，一个都不动
        if names is None:
            unknown += len(items)
            continue
        for it in items:
            if it["filename"] in names:
                execute(
                    "UPDATE media_items SET status = 'ok',"
                    " status_at = datetime('now','localtime'), status_reason = ? WHERE id = ?",
                    ("recheck:recovered", it["id"]),
                )
                recovered += 1
            else:
                still += 1
    logger.info("恢复校验：检查 %d 条，恢复 %d 条，仍缺失 %d 条，无法确认 %d 条",
                len(rows), recovered, still, unknown)
    return {"checked": len(rows), "recovered": recovered,
            "still_missing": still, "unknown": unknown}

def remove_folder(folder_id: int) -> int:
    """
    把目录（含子树）从库中移除（不删除磁盘文件，但清理缩略图缓存文件）。
    返回被移除的媒体数量。
    """
    n = _count_media_in(_subtree_folder_ids(folder_id))
    try:
        from . import media as media_service
        rows = query_all(
            """WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               )
               SELECT m.id FROM media_items m JOIN sub s ON m.folder_id = s.id""",
            (folder_id,),
        )
        media_service.delete_thumbnails([r["id"] for r in rows])
    except Exception:  # noqa: BLE001
        pass
    delete("DELETE FROM folders WHERE id = ?", (folder_id,))
    return n


def remove_media_item(media_id: int) -> bool:
    """从库中删除单个媒体记录（仅在**用户显式删除**时调用），并同步清理缩略图文件。

    注意：文件被外部删除时**不要**调用本函数——那只会把 status 标记为 missing。
    外部丢失的判定只发生在用户显式重扫（rescan_folder）时，且需确认目录可读。
    """
    try:
        from . import media as media_service
        media_service.delete_thumbnails([media_id])
    except Exception:  # noqa: BLE001
        pass
    return delete("DELETE FROM media_items WHERE id = ?", (media_id,)) > 0


def build_tree() -> dict:
    """
    从数据库构建目录树。

    **性能不变量（务必遵守）**：除「根目录可达性」外**不得访问磁盘**。
    根目录通常只有 1~4 个（走 _isdir_fast，带超时+缓存）；
    一旦在这里对每个目录 isdir/scandir，网络盘上 5000+ 目录会变成十几秒的卡顿。
    返回：
        {
          "tree": [ {id, name, path, is_root, parent_id, children: [...], media_count} ],
          "total_folders": int, "total_media": int
        }
    """
    import time as _time
    _t0 = _time.perf_counter()
    folders = query_all("SELECT * FROM folders ORDER BY name")
    counts = query_all("SELECT folder_id, COUNT(*) AS c FROM media_items GROUP BY folder_id")
    count_map = {r["folder_id"]: r["c"] for r in counts}
    miss_rows = query_all(
        "SELECT folder_id, COUNT(*) AS c FROM media_items WHERE status = 'missing' GROUP BY folder_id")
    miss_map = {r["folder_id"]: r["c"] for r in miss_rows}

    nodes: dict[int, dict] = {}
    for f in folders:
        nodes[f["id"]] = {
            "id": f["id"],
            "name": f["name"],
            "path": f["path"],
            "is_root": bool(f["is_root"]),
            "parent_id": f["parent_id"],
            "children": [],
            "media_count": count_map.get(f["id"], 0),
            # 目录本身是否还在磁盘上：**这里绝不做磁盘检查**（否则 5000+ 目录每个一次 isdir，
            # 网络盘上就是十几秒）。统一在下面 _walk 里按根目录可达性推断；
            # 单目录是否真的被删，只在用户点击节点时做一次只读探测（check_folder，不改库），
            # 真正的「丢失标记」只由用户显式重扫（rescan_folder）产生。
            "missing": False,
            # 该目录下被标记为丢失的媒体数
            "missing_count": miss_map.get(f["id"], 0),
            # 所在根目录是否不可达（盘没挂/被拔）→ 由下面 walk 覆盖
            "offline": False,
        }

    roots: list[dict] = []
    for f in folders:
        node = nodes[f["id"]]
        if f["parent_id"] and f["parent_id"] in nodes:
            nodes[f["parent_id"]]["children"].append(node)
        else:
            roots.append(node)

    # 性能要点：目录树必须保持「纯数据库读取」，**绝不逐目录 stat**。
    # 网络盘上 5000+ 个目录各来一次 isdir 会让启动/加载目录树慢十几秒。
    # 因此这里只在「根目录」上做可达性检查（通常 1~2 个根 = 1~2 次 stat），
    # 再向下传播 offline 标记；单目录是否被删改成点击该节点时做只读探测（见 check_folder，绝不改库）。
    def _walk(node: dict, offline: bool) -> None:
        node["offline"] = offline
        node["missing"] = False   # 启动时不逐个 stat；点击时按需判定
        for ch in node["children"]:
            _walk(ch, offline)

    for r in roots:
        _walk(r, offline=not _isdir_fast(r["path"]))

    _ms = (_time.perf_counter() - _t0) * 1000
    if _ms > 500:
        logger.warning("build_tree 耗时 %.0f ms（%d 目录）——目录树应保持纯数据库读取，如有磁盘访问请检查",
                       _ms, len(folders))
    return {
        "tree": roots,
        "total_folders": len(folders),
        "total_media": sum(count_map.values()),
        "missing_total": sum(miss_map.values()),
        "offline_roots": [r["path"] for r in roots if r["offline"]],
    }
