# -*- coding: utf-8 -*-
"""
数据库模块
==========
负责 SQLite 的建表与所有读写操作。
程序的一切操作记录都保存在 <程序根目录>/data/imagedb.sqlite 中。

线程安全说明：
    - 每次操作打开一个短连接（SQLite 对单文件并发读是安全的）；
    - 写操作通过全局互斥锁串行化，避免 "database is locked"；
    - 开启 WAL 模式提升并发读写性能（注意：网络驱动器上 WAL 可能较慢，
      若体验不佳可在 SCHEMA 后改为普通模式，见 README 常见问题）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger("imagedb.db")

# 程序根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据目录（数据库、缩略图、抽帧临时图都放在这里）
DATA_DIR = os.path.join(BASE_DIR, "data")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
FRAMES_DIR = os.path.join(DATA_DIR, "frames")
RECYCLE_DIR = os.path.join(DATA_DIR, "recycle_bin")

# SQLite 数据库文件
DB_PATH = os.path.join(DATA_DIR, "imagedb.sqlite")

# 写操作互斥锁
_write_lock = threading.Lock()

# SQLite 单条 SQL 中 IN (...) 占位符（绑定变量）数量有上限：
#   旧版默认 999，新版（>=3.32.1）为 32766，且随编译参数不同。
# 对可能上万 id 的列表，统一按块切分，避免抛 "too many SQL variables"。
SQLITE_IN_CHUNK_SIZE = 500


def chunk_ids(ids):
    """把 id 列表切成小块，每块的 IN (...) 绑定变量数不超过 SQLITE_IN_CHUNK_SIZE。"""
    n = SQLITE_IN_CHUNK_SIZE
    for i in range(0, len(ids), n):
        yield ids[i:i + n]

# 数据库表结构
SCHEMA = """
-- 目录表：记录用户导入的目录及其子目录（构成程序内目录树）
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,              -- 目录名
    path        TEXT    NOT NULL UNIQUE,       -- 磁盘绝对路径
    parent_id   INTEGER,                       -- 父目录 id（树结构）
    is_root     INTEGER DEFAULT 0,             -- 是否为用户手动导入的根目录
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

-- 媒体文件表：图片 / 视频的路径与元信息
CREATE TABLE IF NOT EXISTS media_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id   INTEGER NOT NULL,              -- 所属目录
    path        TEXT    NOT NULL UNIQUE,       -- 磁盘绝对路径
    filename    TEXT    NOT NULL,              -- 文件名
    type        TEXT    NOT NULL,              -- 'image' / 'video'
    ext         TEXT,                          -- 扩展名（小写，不含点）
    size        INTEGER DEFAULT 0,             -- 字节数
    mtime       REAL    DEFAULT 0,             -- 文件修改时间（时间戳）
    width       INTEGER,                       -- 图片/视频宽
    height      INTEGER,                       -- 图片/视频高
    duration    REAL,                          -- 视频时长（秒）
    thumbnail   TEXT,                          -- 缩略图相对路径（如 thumbs/123.jpg）
    status      TEXT    DEFAULT 'ok',          -- 'ok' / 'missing'（异常标记）
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media_items(folder_id);
CREATE INDEX IF NOT EXISTS idx_media_type ON media_items(type);
CREATE INDEX IF NOT EXISTS idx_media_filename ON media_items(filename);

-- 标签表
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,       -- 标签名
    source      TEXT    DEFAULT 'manual',      -- 来源：manual / cl_tagger / wd14 / llm
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);

-- 媒体-标签关联表
CREATE TABLE IF NOT EXISTS media_tags (
    media_id    INTEGER NOT NULL,
    tag_id      INTEGER NOT NULL,
    confidence  REAL    DEFAULT 1.0,           -- 置信度（手动标签为 1.0）
    source      TEXT    DEFAULT 'manual',      -- 来源
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (media_id, tag_id),
    FOREIGN KEY (media_id) REFERENCES media_items(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)   REFERENCES tags(id)   ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag_id);

-- 打标任务表：记录每个打标任务的进度
CREATE TABLE IF NOT EXISTS tag_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type  TEXT,                          -- 'media' 或 'folder'
    scope_ids   TEXT,                          -- 目标 id 列表（JSON 数组）
    tool        TEXT,                          -- 使用的打标工具名
    overwrite   INTEGER DEFAULT 0,             -- 是否覆盖已有标签
    status      TEXT    DEFAULT 'pending',     -- pending/running/done/failed/cancelled
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    message     TEXT,                          -- 失败或说明信息
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

-- 设置表：键值对存储应用配置（代理、打标参数等）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 应用内回收站表：记录从"不支持系统回收站"的盘（如网络盘）移入本地暂存的素材，
-- 支持在本应用内"还原/恢复"与"清空"。标签以 JSON 快照保存，恢复时一并写回。
CREATE TABLE IF NOT EXISTS recycle_bin (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id    INTEGER,                    -- 原 media_items.id（恢复时复用该 id）
    folder_id   INTEGER,                    -- 原所属目录 id
    path        TEXT NOT NULL,              -- 原磁盘绝对路径（恢复时移回）
    filename    TEXT NOT NULL,              -- 文件名
    type        TEXT NOT NULL,              -- 'image' / 'video'
    ext         TEXT,                       -- 扩展名
    size        INTEGER DEFAULT 0,          -- 字节数
    mtime       REAL DEFAULT 0,             -- 文件修改时间
    thumbnail   TEXT,                       -- 原缩略图相对路径
    stored_path TEXT,                       -- 现在暂存在本地回收站的路径
    tags_json   TEXT,                       -- 标签快照（JSON，恢复时重新写回）
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_recycle_media ON recycle_bin(media_id);
"""


def ensure_dirs() -> None:
    """确保数据目录存在。"""
    for d in (DATA_DIR, THUMBS_DIR, FRAMES_DIR, RECYCLE_DIR):
        os.makedirs(d, exist_ok=True)


def init_schema() -> None:
    """建表（幂等，可重复调用）。"""
    ensure_dirs()
    with _write_lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _connect() -> sqlite3.Connection:
    """打开一个新连接（每操作一个连接，避免多线程互踩）。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # WAL 提升并发读写性能
    except sqlite3.Error:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def execute(sql: str, params: tuple | list = ()) -> int:
    """执行写操作，返回最后插入的行 id。"""
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def executemany(sql: str, seq: list[tuple]) -> None:
    """批量执行写操作。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.executemany(sql, seq)
            conn.commit()
        finally:
            conn.close()


def execute_rowcount(sql: str, params: tuple | list = ()) -> int:
    """执行写操作，返回受影响行数（INSERT OR IGNORE 时忽略的不计入）。"""
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def delete(sql: str, params: tuple | list = ()) -> int:
    """执行 DELETE，返回删除行数。"""
    with _write_lock:
        conn = _connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def query_all(sql: str, params: tuple | list = ()) -> list[dict]:
    """查询多行，返回字典列表。"""
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    """查询单行，返回字典或 None。"""
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
