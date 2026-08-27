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

import logging
import os

from .database import delete, execute, executemany, query_all, query_one

logger = logging.getLogger("imagedb.library")

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
    if media_rows:
        executemany(
            """INSERT OR IGNORE INTO media_items
               (folder_id, path, filename, type, ext, size, mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            media_rows,
        )
        media_added = len(media_rows)

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

    root_id = _insert_folder(
        os.path.basename(root_path.rstrip(os.sep)) or root_path,
        root_path, None, is_root=1,
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

    root_id = _insert_folder(
        os.path.basename(root_path.rstrip(os.sep)) or root_path,
        root_path, None, is_root=1,
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
            executemany(
                """INSERT OR IGNORE INTO media_items
                   (folder_id, path, filename, type, ext, size, mtime)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                media_rows,
            )
            media_added += len(media_rows)
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


def _count_media_in(folder_ids: list[int]) -> int:
    """统计若干目录下的媒体数量。"""
    if not folder_ids:
        return 0
    ph = ",".join("?" * len(folder_ids))
    row = query_one(f"SELECT COUNT(*) AS c FROM media_items WHERE folder_id IN ({ph})", folder_ids)
    return row["c"] if row else 0


def rescan_folder(folder_id: int) -> dict:
    """
    重新扫描某个目录子树：
    - 磁盘上已不存在的子目录 → 数据库级联删除；
    - 磁盘上已不存在的媒体 → 删除记录；
    - 磁盘上新增的目录/媒体 → 补录。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        raise ValueError(f"目录 id 不存在：{folder_id}")
    if not os.path.isdir(folder["path"]):
        # 根目录已不存在：整棵子树从数据库删除
        n_media = _count_media_in(_subtree_folder_ids(folder_id))
        delete("DELETE FROM folders WHERE id = ?", (folder_id,))
        return {"added": 0, "removed_media": n_media, "removed_folders": 1}

    added = 0
    removed_media = 0
    removed_folders = 0

    # 1. 删除磁盘上已不存在的子目录记录（级联删除其媒体）
    for f in query_all("SELECT id, path FROM folders WHERE parent_id = ? OR id = ?",
                       (folder_id, folder_id)):
        if not os.path.isdir(f["path"]):
            n = _count_media_in(_subtree_folder_ids(f["id"]))
            delete("DELETE FROM folders WHERE id = ?", (f["id"],))
            removed_media += n
            removed_folders += 1

    # 2. 补录新增的目录与媒体
    seen: set[str] = {os.path.realpath(folder["path"])}
    fa, ma = _walk_and_insert(folder["path"], folder_id, seen)
    added = ma

    # 3. 删除仍存在目录中已消失的媒体
    for f in _subtree_folder_ids(folder_id):
        frow = query_one("SELECT path FROM folders WHERE id = ?", (f,))
        if not frow or not os.path.isdir(frow["path"]):
            continue
        try:
            on_disk = {e.name for e in os.scandir(frow["path"])}
        except OSError:
            continue
        for item in query_all("SELECT id, filename FROM media_items WHERE folder_id = ?", (f,)):
            if item["filename"] not in on_disk:
                delete("DELETE FROM media_items WHERE id = ?", (item["id"],))
                removed_media += 1

    return {"added": added, "removed_media": removed_media, "removed_folders": removed_folders}


def verify_all() -> dict:
    """
    全库校验：遍历所有根目录，磁盘上不存在的目录/文件自动从数据库删除。
    由后台校验线程与“手动校验”按钮调用。
    返回清理统计。
    """
    removed_folders = 0
    removed_media = 0
    roots = query_all("SELECT id, path FROM folders WHERE is_root = 1")
    for root in roots:
        if not os.path.isdir(root["path"]):
            # 根目录不存在：删除整棵子树
            n = _count_media_in(_subtree_folder_ids(root["id"]))
            delete("DELETE FROM folders WHERE id = ?", (root["id"],))
            removed_folders += 1
            removed_media += n
            continue
        # 检查根目录下的所有子目录
        for f in _subtree_folder_ids(root["id"]):
            frow = query_one("SELECT path FROM folders WHERE id = ?", (f,))
            if not frow:
                continue
            if not os.path.isdir(frow["path"]):
                n = _count_media_in(_subtree_folder_ids(f))
                delete("DELETE FROM folders WHERE id = ?", (f,))
                removed_folders += 1
                removed_media += n
                continue
            # 逐目录比对磁盘文件名，删除已消失的媒体
            try:
                on_disk = {e.name for e in os.scandir(frow["path"])}
            except OSError:
                continue
            for item in query_all("SELECT id, filename FROM media_items WHERE folder_id = ?", (f,)):
                if item["filename"] not in on_disk:
                    delete("DELETE FROM media_items WHERE id = ?", (item["id"],))
                    removed_media += 1

    if removed_folders or removed_media:
        logger.info("校验完成：清理缺失目录 %d 个，缺失媒体 %d 个", removed_folders, removed_media)
    return {"removed_folders": removed_folders, "removed_media": removed_media}


def check_folder(folder_id: int) -> dict:
    """
    校验单个目录（用户点击树节点时调用）：
    目录已不存在则自动删除子树并返回 exists=False。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        return {"exists": False, "removed_folders": 0, "removed_media": 0}
    if not os.path.isdir(folder["path"]):
        n = _count_media_in(_subtree_folder_ids(folder_id))
        nf = len(_subtree_folder_ids(folder_id))
        delete("DELETE FROM folders WHERE id = ?", (folder_id,))
        return {"exists": False, "removed_folders": nf, "removed_media": n}
    return {"exists": True, "removed_folders": 0, "removed_media": 0}


def remove_folder(folder_id: int) -> int:
    """
    把目录（含子树）从库中移除（不删除磁盘文件）。
    返回被移除的媒体数量。
    """
    n = _count_media_in(_subtree_folder_ids(folder_id))
    delete("DELETE FROM folders WHERE id = ?", (folder_id,))
    return n


def remove_media_item(media_id: int) -> bool:
    """从库中删除单个媒体记录（文件被外部删除时调用）。"""
    return delete("DELETE FROM media_items WHERE id = ?", (media_id,)) > 0


def build_tree() -> dict:
    """
    从数据库构建目录树（纯数据库读取，不访问磁盘）。
    返回：
        {
          "tree": [ {id, name, path, is_root, parent_id, children: [...], media_count} ],
          "total_folders": int, "total_media": int
        }
    """
    folders = query_all("SELECT * FROM folders ORDER BY name")
    counts = query_all("SELECT folder_id, COUNT(*) AS c FROM media_items GROUP BY folder_id")
    count_map = {r["folder_id"]: r["c"] for r in counts}

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
        }

    roots: list[dict] = []
    for f in folders:
        node = nodes[f["id"]]
        if f["parent_id"] and f["parent_id"] in nodes:
            nodes[f["parent_id"]]["children"].append(node)
        else:
            roots.append(node)

    return {
        "tree": roots,
        "total_folders": len(folders),
        "total_media": sum(count_map.values()),
    }
