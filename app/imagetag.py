# -*- coding: utf-8 -*-
"""
.imgtag 侧车（每个图片目录一个 SQLite）—— 标签导出/导入迁移
==========================================================
用途：把标签写成「随文件走」的可迁移侧车，换盘符/挪目录后不用重打标。

设计原则：
    - .imgtag 只在「显式 导出/导入」时读写；程序日常读写标签一律走 data/imagedb.sqlite 主库；
    - 每个目录一份 .imgtag，按「本目录内文件名」关联（不存 media_id），所以换目录/换盘仍能对上；
    - 纯标准库 sqlite3 + json，零新增依赖；脱离软件也可用 python 标准库读取；
    - 只读写媒体目录旁的 .imgtag，绝不碰媒体文件本身；默认不用 WAL（避免残留 -wal/-shm）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

from .database import (chunk_ids, execute, execute_rowcount, executemany,
                       query_all, query_one)

logger = logging.getLogger("imagedb.imagetag")

# 侧车文件名（扫描时会被 library 跳过）
SIDECAR_NAME = ".imgtag"
SIDECAR_NAMES = {".imgtag", ".txttag"}


def is_sidecar(name: str) -> bool:
    """判断一个文件名/目录名是否为标签侧车（应被扫描跳过）。"""
    base = os.path.basename(name or "")
    return base in SIDECAR_NAMES


def sidecar_path(directory: str) -> str:
    return os.path.join(directory, SIDECAR_NAME)


def _open(directory: str) -> sqlite3.Connection:
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(sidecar_path(directory), timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS tags (filename TEXT PRIMARY KEY, tags_json TEXT NOT NULL)")
    return conn


# ---------------- 读写侧车 ----------------
def write_tags(directory: str, tags_by_filename: dict, rebuild: bool = False) -> int:
    """把 {filename: [ {name,source,confidence} ]} 写入 directory/.imgtag（合并）。

    rebuild=True 时先清空该目录 .imgtag 再写（整目录重建，用于文件夹导出同步到当前库）。
    返回写入的文件数；失败返回 0（不抛错）。
    """
    entries = tags_by_filename or {}
    if not entries:
        return 0
    try:
        conn = _open(directory)
        try:
            if rebuild:
                conn.execute("DELETE FROM tags")
            for fname, tags in entries.items():
                conn.execute("INSERT OR REPLACE INTO tags(filename, tags_json) VALUES (?, ?)",
                             (fname, json.dumps(tags, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 .imgtag 失败 %s：%s", directory, exc)
        return 0
    return len(entries)


def read_tags(directory: str) -> dict:
    """读取 directory/.imgtag，返回 {filename: [ {name,source,confidence} ]}；无/异常返回 {}。"""
    if not os.path.isfile(sidecar_path(directory)):
        return {}
    out: dict = {}
    try:
        conn = sqlite3.connect(sidecar_path(directory), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute("SELECT filename, tags_json FROM tags"):
                try:
                    tags = json.loads(r["tags_json"])
                except Exception:
                    tags = []
                out[r["filename"]] = tags if isinstance(tags, list) else []
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 .imgtag 失败 %s：%s", directory, exc)
    return out


# ---------------- 标签辅助 ----------------
def _fetch_tags(media_ids: list[int]) -> dict:
    """批量取 {media_id: [ {name,source,confidence} ]}（分块，防变量超限）。"""
    out: dict = {}
    if not media_ids:
        return out
    for chunk in chunk_ids(media_ids):
        ph = ",".join("?" * len(chunk))
        for r in query_all(
            "SELECT mt.media_id AS mid, t.name, mt.confidence, mt.source "
            "FROM media_tags mt JOIN tags t ON t.id = mt.tag_id "
            "WHERE mt.media_id IN (%s)" % ph, chunk,
        ):
            out.setdefault(r["mid"], []).append({
                "name": r["name"], "source": r["source"], "confidence": r["confidence"],
            })
    return out


def _write_tags(media_id: int, tags: list, source: str, overwrite: bool = False) -> int:
    """把一组标签写入主库 media_tags/tags（source 区分；overwrite 替换同来源）。返回新增数。"""
    if overwrite:
        execute("DELETE FROM media_tags WHERE media_id = ? AND source = ?", (media_id, source))
    existing: set = set()
    if not overwrite:
        for r in query_all("SELECT tag_id FROM media_tags WHERE media_id = ?", (media_id,)):
            existing.add(r["tag_id"])
    added = 0
    for t in (tags or []):
        name = (t.get("name") if isinstance(t, dict) else t) or ""
        name = str(name).strip()
        if not name:
            continue
        row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
        tag_id = row["id"] if row else execute("INSERT INTO tags(name, source) VALUES (?, ?)", (name, source))
        if tag_id in existing:
            continue
        conf = 1.0
        if isinstance(t, dict):
            try:
                conf = float(t.get("confidence", 1.0) or 1.0)
            except (TypeError, ValueError):
                conf = 1.0
        execute("INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                (media_id, tag_id, max(0.0, min(1.0, conf)), source))
        existing.add(tag_id)
        added += 1
    return added


def _subtree_folders(folder_id: int) -> list[dict]:
    """目录子树下所有目录 (id, path, name)。"""
    return query_all(
        """WITH RECURSIVE sub(id) AS (
               SELECT id FROM folders WHERE id = ?
               UNION ALL
               SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
           )
           SELECT f.id, f.path, f.name FROM folders f JOIN sub s ON f.id = s.id""",
        (folder_id,),
    )


# ---------------- 导出 ----------------
def export_media(media_rows: list[dict]) -> dict:
    """按选中的媒体（单图/多图）导出：把它们的 tags 写入各自目录的 .imgtag（合并）。
    media_rows: [{id, path, filename}, ...]。返回统计。"""
    ids = [r["id"] for r in media_rows]
    tmap = _fetch_tags(ids)
    by_dir: dict[str, dict] = {}
    for r in media_rows:
        d = os.path.dirname(r["path"]) or os.getcwd()
        by_dir.setdefault(d, {})[r["filename"]] = tmap.get(r["id"], [])
    n = 0
    for d, entries in by_dir.items():
        n += write_tags(d, entries, rebuild=False)
    return {"dirs": len(by_dir), "media": len(media_rows), "written": n}


def export_folder(folder_id: int) -> dict:
    """导出整棵目录树：每个目录写一份 .imgtag（整目录重建为当前库状态）。返回统计。"""
    dirs = 0
    media = 0
    written = 0
    for f in _subtree_folders(folder_id):
        rows = query_all("SELECT id, filename FROM media_items WHERE folder_id = ?", (f["id"],))
        if not rows:
            continue
        tmap = _fetch_tags([r["id"] for r in rows])
        entries = {r["filename"]: tmap.get(r["id"], []) for r in rows}
        w = write_tags(f["path"], entries, rebuild=True)
        dirs += 1
        media += len(rows)
        written += w
    return {"dirs": dirs, "media": media, "written": written}


# ---------------- 导入 ----------------
def import_folder(folder_id: int, overwrite: bool = False) -> dict:
    """导入目录子树：读各目录 .imgtag，按文件名匹配回主库媒体并写回标签（source=import）。

    overwrite=True 时替换该来源旧标签，否则去重追加（不覆盖）。
    返回 {media: 匹配媒体数, tags: 新增标签数, files: 处理了哪些 .imgtag 数}。
    """
    media_hit = 0
    tags_added = 0
    files = 0
    for f in _subtree_folders(folder_id):
        side = read_tags(f["path"])
        if not side:
            continue
        files += 1
        rows = query_all("SELECT id, filename FROM media_items WHERE folder_id = ?", (f["id"],))
        byname = {r["filename"]: r["id"] for r in rows}
        for fname, tags in side.items():
            mid = byname.get(fname)
            if mid is None:
                continue
            media_hit += 1
            tags_added += _write_tags(mid, tags, "import", overwrite)
    return {"media": media_hit, "tags": tags_added, "files": files}
