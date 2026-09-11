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

from .database import (chunk_ids, delete, execute, execute_rowcount, executemany,
                      query_all, query_one)
from .imagetag import is_sidecar

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


def _count_media_in(folder_ids: list[int]) -> int:
    """统计若干目录下的媒体数量。"""
    if not folder_ids:
        return 0
    ph = ",".join("?" * len(folder_ids))
    row = query_one(f"SELECT COUNT(*) AS c FROM media_items WHERE folder_id IN ({ph})", folder_ids)
    return row["c"] if row else 0


# ---------------- 丢失标记（外部丢失不删记录） ----------------
def _mark_all_missing(folder_ids: list[int]) -> int:
    """把若干目录下的所有媒体标记为 missing（**不删除**）。返回被标记的数量。"""
    if not folder_ids:
        return 0
    n = 0
    for chunk in chunk_ids(folder_ids):
        ph = ",".join("?" * len(chunk))
        n += execute_rowcount(
            "UPDATE media_items SET status = 'missing'"
            f" WHERE folder_id IN ({ph}) AND (status IS NULL OR status != 'missing')",
            chunk,
        )
    return n


def _sync_dir_status(folder_db_id: int, dir_path: str) -> tuple[int, int]:
    """按磁盘实际文件名，把该目录下媒体记录在 ok/missing 间同步（**不删除**）。
    返回 (新标记为 missing 数, 恢复为 ok 数)。"""
    try:
        on_disk = {e.name for e in os.scandir(dir_path)}
    except OSError:
        return 0, 0
    miss = 0
    ok = 0
    for item in query_all("SELECT id, filename, status FROM media_items WHERE folder_id = ?",
                          (folder_db_id,)):
        want = "ok" if item["filename"] in on_disk else "missing"
        if (item["status"] or "ok") != want:
            execute("UPDATE media_items SET status = ? WHERE id = ?", (want, item["id"]))
            if want == "missing":
                miss += 1
            else:
                ok += 1
    return miss, ok


def rescan_folder(folder_id: int) -> dict:
    """重新扫描某个目录子树（**只标记、不删除**）：
    - 磁盘上新增的目录/媒体 → 补录；
    - 磁盘上已消失的媒体 → 标记 status='missing'（保留记录/标签/缩略图）；
    - 之前 missing 现在又出现的 → 标回 'ok'；
    - 目录本身消失 → 其下媒体全部标记 missing（不删目录、不删记录）。
    返回 {added, missing, recovered, dir_missing}。
    真正删除记录请用 purge_missing()（用户显式确认后）。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        raise ValueError(f"目录 id 不存在：{folder_id}")
    if not os.path.isdir(folder["path"]):
        missing = _mark_all_missing(_subtree_folder_ids(folder_id))
        return {"added": 0, "missing": missing, "recovered": 0, "dir_missing": True}

    # 1. 补录磁盘上新增的目录与媒体
    seen: set[str] = {os.path.realpath(folder["path"])}
    _fa, added = _walk_and_insert(folder["path"], folder_id, seen)

    # 2. 逐目录同步 ok/missing（不删任何记录）
    missing = 0
    recovered = 0
    for f in _subtree_folder_ids(folder_id):
        frow = query_one("SELECT path FROM folders WHERE id = ?", (f,))
        if not frow:
            continue
        if not os.path.isdir(frow["path"]):
            missing += _mark_all_missing([f])
            continue
        m, o = _sync_dir_status(f, frow["path"])
        missing += m
        recovered += o

    return {"added": added, "missing": missing, "recovered": recovered, "dir_missing": False}


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


def verify_all() -> dict:
    """全库校验（**只标记、不删除**）：磁盘上不存在的媒体 → status='missing'；
    重新出现的 → 标回 'ok'。**绝不删除任何数据库记录**（清理须用户显式确认）。
    由后台校验线程与「扫描丢失」按钮调用。返回 {missing, recovered, dirs_missing}。
    """
    missing = 0
    recovered = 0
    dirs_missing = 0
    for f in query_all("SELECT id, path FROM folders"):
        if not os.path.isdir(f["path"]):
            dirs_missing += 1
            missing += _mark_all_missing([f["id"]])
            continue
        m, o = _sync_dir_status(f["id"], f["path"])
        missing += m
        recovered += o
    return {"missing": missing, "recovered": recovered, "dirs_missing": dirs_missing}


def check_folder(folder_id: int) -> dict:
    """校验单个目录（用户点击树节点时调用）：
    目录已不存在 → 只把其下媒体标记 missing 并返回 exists=False（**绝不删记录**）。
    """
    folder = query_one("SELECT * FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        return {"exists": False, "missing": 0}
    if not os.path.isdir(folder["path"]):
        n = _mark_all_missing(_subtree_folder_ids(folder_id))
        return {"exists": False, "missing": n}
    return {"exists": True, "missing": 0}

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

    注意：文件被外部删除时**不要**调用本函数——那只会把 status 标记为 missing（见 verify_all）。
    """
    try:
        from . import media as media_service
        media_service.delete_thumbnails([media_id])
    except Exception:  # noqa: BLE001
        pass
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
            # 目录本身是否还在磁盘上（动态检测，不写库）；缺失目录仍显示，仅标注
            "missing": not os.path.isdir(f["path"]),
            # 该目录下被标记为丢失的媒体数
            "missing_count": miss_map.get(f["id"], 0),
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
