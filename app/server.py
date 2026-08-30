# -*- coding: utf-8 -*-
"""
HTTP 服务模块
=============
基于 FastAPI 提供 REST API 与前端静态页面。
所有数据操作最终落在 SQLite（data/imagedb.sqlite）。

API 概览：
    GET    /api/tree                      目录树（纯数据库读取）
    POST   /api/library/import            导入目录
    POST   /api/library/rescan            重新扫描目录
    POST   /api/library/remove            从库中移除目录
    POST   /api/library/verify            全库缺失校验（自动清理）
    POST   /api/library/check             检查单个目录是否存在
    GET    /api/media                     搜索/筛选媒体（文件名/目录名/标签/类型）
    GET    /api/media/{id}                媒体详情
    GET    /api/media/{id}/file           流式读取原文件（支持视频 Range 拖动）
    GET    /api/media/{id}/thumbnail      缩略图（不存在则自动生成）
    GET    /api/media/{id}/frames         视频抽帧（返回帧图 URL 列表）
    POST   /api/media/{id}/tags           手动添加标签
    POST   /api/media/{id}/tags/remove    手动移除标签
    GET    /api/tags                      标签自动补全列表
    GET    /api/tagging/tools             打标工具列表
    POST   /api/tagging/run               启动打标任务
    GET    /api/tagging/jobs              任务列表
    GET    /api/tagging/jobs/{id}         任务进度
    POST   /api/tagging/jobs/{id}/cancel  取消任务
    POST   /api/tagging/reload            重载插件
    GET    /api/settings                  读取设置
    PUT    /api/settings                  保存设置
    POST   /api/settings/test-proxy       测试代理
    POST   /api/models/download           下载模型
    GET    /api/downloads                 下载/更新任务列表
    POST   /api/deps/update               更新依赖
"""
from __future__ import annotations

import ctypes
import json
import shutil
import sys
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import library, media as media_service
from .config import AppConfig
from .database import (DATA_DIR, FRAMES_DIR, THUMBS_DIR, RECYCLE_DIR, execute, execute_rowcount,
                      executemany, query_all, query_one)
from .downloader import (download_model, install_directml,
                      list_jobs as list_dl_jobs, test_proxy, update_deps)
from .tagging import manager as tagging_manager

logger = logging.getLogger("imagedb.server")
# send2trash 用于把文件移到系统回收站（Windows/macOS/Linux 均支持本地文件系统）。
# 若未安装则降级：一律走应用内回收站（移到本地暂存，可还原），绝不完全删除磁盘文件。
try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    send2trash = None
    HAS_SEND2TRASH = False


def _os_recycle_supported(path: str) -> bool:
    """判断该路径是否适合走系统的"真正回收站"。

    - 非 Windows（macOS/Linux）：send2trash 都能正常进系统回收站，返回 True；
    - Windows：仅本地固定盘（DRIVE_FIXED）有系统回收站；网络盘/可移动盘/光驱等没有，
      send2trash 在那些盘上会静默变成"彻底删除"，因此返回 False 改走应用内回收站，
      确保任何情况下都不丢数据。
    """
    if not HAS_SEND2TRASH:
        return False
    if sys.platform != "win32":
        return True
    try:
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return False  # 无盘符（异常路径）→ 保守走应用内回收站
        root = drive + os.sep
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        return drive_type == 3  # DRIVE_FIXED
    except Exception:
        return False  # 判定失败 → 保守走应用内回收站（不丢数据）


def _app_trash_move(row: dict, tags: list[dict]) -> str:
    """把文件移到本地"应用内回收站" data/recycle_bin/，并写入 recycle_bin 表（可还原）。

    返回暂存路径。标签以 JSON 快照保存，恢复时一并写回，避免丢失标签元数据。
    """
    os.makedirs(RECYCLE_DIR, exist_ok=True)
    dest = os.path.join(RECYCLE_DIR, f"{row['id']}_{os.path.basename(row['path'])}")
    shutil.move(row["path"], dest)
    tags_json = json.dumps(
        [{"name": t["name"], "confidence": t.get("confidence", 1.0),
          "source": t.get("source", "manual")} for t in tags],
        ensure_ascii=False)
    execute(
        "INSERT INTO recycle_bin(media_id, folder_id, path, filename, type, ext, size, mtime, "
        "thumbnail, stored_path, tags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["folder_id"], row["path"], row["filename"], row["type"],
         row["ext"], row["size"], row["mtime"], row["thumbnail"], dest, tags_json))
    return dest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")


# ---------------- 请求体模型 ----------------
class ImportRequest(BaseModel):
    path: str


class FolderIdRequest(BaseModel):
    folder_id: int


class TagRunRequest(BaseModel):
    tool: str
    scope_type: str          # 'media'（按 id 列表）或 'folder'（整棵目录树）
    scope_ids: list[int]
    overwrite: bool = False


class TagListRequest(BaseModel):
    tags: list[str]



class FolderTagRequest(BaseModel):
    """目录标签批量操作：不需要 media_ids，作用于整个目录子树。"""
    tags: list[str]
    action: str = "add"          # add / remove

class BatchTagRequest(BaseModel):
    """批量添加/移除标签（应用到多个媒体）。"""
    media_ids: list[int]
    tags: list[str]
    action: str = "add"          # 'add' 或 'remove'


class TagRenameRequest(BaseModel):
    """全局重命名标签（应用到所有含有该标签的媒体）。"""
    old_name: str
    new_name: str


class TagDeleteRequest(BaseModel):
    """全局删除标签（应用到所有含有该标签的媒体）。"""
    name: str


class MediaDeleteRequest(BaseModel):
    """从数据库删除媒体记录（不删除磁盘文件）。"""
    media_ids: list[int]


class MediaTrashRequest(BaseModel):
    """把媒体文件移到回收站（无回收站系统则彻底删除需二次确认）。"""
    media_ids: list[int]


class RecycleIdsRequest(BaseModel):
    """应用内回收站批量操作请求。"""
    ids: list[int]


class MediaMoveRequest(BaseModel):
    """把媒体文件移动到指定目录。"""
    media_ids: list[int]
    dest_dir: str


class MediaAddRequest(BaseModel):
    """向指定目录添加单个媒体文件记录。"""
    folder_id: int
    path: str


class SettingsUpdateRequest(BaseModel):
    settings: dict


class ProxyTestRequest(BaseModel):
    proxy: dict


class ModelDownloadRequest(BaseModel):
    repo_id: str
    tool: str = "cl_tagger"


class DepsUpdateRequest(BaseModel):
    packages: str = "fastapi uvicorn requests pillow numpy"


# ---------------- 导入任务存储（内存） ----------------
# 用于后台导入任务的进度跟踪：job_id -> {status, progress, total, done, message, started_at}
IMPORT_JOBS: dict[str, dict] = {}
IMPORT_JOBS_LOCK = threading.Lock()


def _new_import_job(path: str) -> str:
    """创建导入任务记录，返回 job_id。"""
    import uuid
    jid = uuid.uuid4().hex[:12]
    with IMPORT_JOBS_LOCK:
        IMPORT_JOBS[jid] = {
            "id": jid,
            "path": path,
            "status": "counting",   # counting -> importing -> done/failed
            "progress": 0,          # 0~100
            "total": 0,             # 预估总文件数
            "done": 0,              # 已导入数
            "message": "正在统计文件……",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return jid


def _update_import_job(jid: str, **kwargs) -> None:
    with IMPORT_JOBS_LOCK:
        if jid in IMPORT_JOBS:
            IMPORT_JOBS[jid].update(kwargs)


def _import_worker(jid: str, path: str) -> None:
    """后台导入工作线程：先多线程统计总数，再逐目录导入并更新进度。"""
    try:
        # 阶段 1：多线程快速统计（预估总文件数）
        _update_import_job(jid, status="counting", message="正在统计文件数量……")
        total_dirs, total_files = library.count_media_files(path)
        _update_import_job(jid, status="importing", total=total_files,
                           message=f"统计完成：{total_files} 个媒体文件，开始导入……")
        logger.info("导入前统计：目录 %d 个，媒体 %d 个", total_dirs, total_files)
        if total_files == 0:
            _update_import_job(jid, status="done", progress=100,
                               message="该目录下没有找到图片或视频")
            return

        # 阶段 2：导入（内部每批写入后通过回调更新进度）
        result = library.import_folder_progress(
            path,
            progress_cb=lambda done: _update_import_job(
                jid, done=done,
                progress=min(100, round(done / max(total_files, 1) * 100)),
            ),
        )
        _update_import_job(jid, status="done", progress=100,
                           message=f"导入完成：新增 {result['media_added']} 个媒体文件")
    except ValueError as exc:
        _update_import_job(jid, status="failed", message=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("导入任务异常：%s", exc)
        _update_import_job(jid, status="failed", message=f"导入失败：{exc}")


# ---------------- 内部辅助 ----------------
def _media_to_dict(r: dict) -> dict:
    """媒体记录 → 前端 JSON 字典。"""
    return {
        "id": r["id"],
        "folder_id": r["folder_id"],
        "path": r["path"],
        "filename": r["filename"],
        "type": r["type"],
        "ext": r["ext"],
        "size": r["size"],
        "mtime": r["mtime"],
        "width": r["width"],
        "height": r["height"],
        "duration": r["duration"],
        "thumbnail": r["thumbnail"],
        "status": r["status"],
        "tags": [],
    }


def search_media(folder_id: Optional[int] = None, q: str = "", dir_q: str = "",
                 tags: str = "", tag_any: bool = False, type: str = "",
                 page: int = 1, page_size: int = 60, sort: str = "name") -> dict:
    """
    媒体搜索/筛选：
    - q       ：按文件名模糊匹配；
    - dir_q   ：按目录路径模糊匹配；
    - tags    ：按标签过滤（逗号分隔多个标签，默认取交集 AND，tag_any=True 取并集 OR）；
    - type    ：image / video / 空（全部）；
    - folder_id：限定某目录及其子树。
    """
    where: list[str] = []
    args: list = []

    if folder_id is not None:
        where.append("""m.folder_id IN (
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM folders WHERE id = ?
                UNION ALL
                SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
            ) SELECT id FROM sub)""")
        args.append(folder_id)
    if q:
        where.append("m.filename LIKE ?")
        args.append(f"%{q}%")
    if dir_q:
        where.append("f.path LIKE ?")
        args.append(f"%{dir_q}%")
    if type in ("image", "video"):
        where.append("m.type = ?")
        args.append(type)

    # 标签过滤：支持空格 / 逗号 / 顿号混合分割，多个标签默认取交集（AND）。
    # 注意：标签名本身可能含空格（如 "long hair"），因此用 LIKE 包含匹配，
    #       而不是精确相等，这样 "long hair solo" 能正确命中含 "long hair" 与 "solo" 的图。
    import re as _re
    tag_list = [t for t in _re.split(r"[\s,，、]+", tags) if t.strip()]
    if tag_list:
        if tag_any:
            # 任一标签命中（OR）：任一关键词被任一标签包含即可
            ph = ",".join("?" * len(tag_list))
            or_parts = ["tg.name LIKE ?"] * len(tag_list)
            where.append(f"""m.id IN (
                SELECT mt.media_id FROM media_tags mt
                JOIN tags tg ON tg.id = mt.tag_id
                WHERE ({' OR '.join(or_parts)}))""")
            args.extend([f"%{t}%" for t in tag_list])
        else:
            # 全部标签命中（AND）：每个关键词都必须被至少一个标签包含
            for t in tag_list:
                where.append("""EXISTS (
                    SELECT 1 FROM media_tags mt JOIN tags tg ON tg.id = mt.tag_id
                    WHERE mt.media_id = m.id AND tg.name LIKE ?)""")
                args.append(f"%{t}%")

    base = "FROM media_items m JOIN folders f ON f.id = m.folder_id"
    if where:
        base += " WHERE " + " AND ".join(where)

    total = query_one(f"SELECT COUNT(*) AS c {base}", args)["c"]

    order_map = {
        "name": "m.filename ASC, m.id ASC",
        "date": "m.created_at DESC, m.id DESC",
        "size": "m.size DESC",
        "mtime": "m.mtime DESC",
    }
    order = order_map.get(sort, order_map["name"])
    offset = (page - 1) * page_size
    rows = query_all(f"SELECT m.* {base} ORDER BY {order} LIMIT ? OFFSET ?",
                     args + [page_size, offset])

    # 批量附加标签
    ids = [r["id"] for r in rows]
    tag_map: dict[int, list] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for row in query_all(
            f"""SELECT mt.media_id AS mid, t.name, mt.confidence, mt.source
                FROM media_tags mt JOIN tags t ON t.id = mt.tag_id
                WHERE mt.media_id IN ({ph}) ORDER BY mt.confidence DESC""",
            ids,
        ):
            tag_map.setdefault(row["mid"], []).append({
                "name": row["name"],
                "confidence": row["confidence"],
                "source": row["source"],
            })

    items = []
    for r in rows:
        d = _media_to_dict(r)
        d["tags"] = tag_map.get(r["id"], [])
        items.append(d)

    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _maybe_cleanup_thumbs() -> None:
    """低频触发缩略图缓存清理（每次访问时调用，但内部节流避免频繁扫描）。
    仅在缓存明显超限时才真正扫描清理。"""
    try:
        limit = AppConfig().get_int("thumb_cache_limit_mb", 200)
        if limit <= 0:
            return  # 0 = 不限制
        # 粗筛：定期（每 60 次访问）检查一次总大小
        global _thumb_clean_counter
        _thumb_clean_counter += 1
        if _thumb_clean_counter % 60 != 0:
            return
        media_service.cleanup_thumb_cache(limit)
    except Exception:  # noqa: BLE001
        pass


_thumb_clean_counter = 0


def _require_media(mid: int, auto_clean: bool = True) -> dict:
    """
    查询媒体记录；若文件已被外部删除：
    - 自动从数据库清理该条目（满足“即时清理”需求）；
    - 返回 None 并抛 HTTP 410。
    """
    row = query_one("SELECT * FROM media_items WHERE id = ?", (mid,))
    if row is None:
        raise HTTPException(404, "记录不存在")
    if auto_clean and not os.path.isfile(row["path"]):
        library.remove_media_item(mid)
        raise HTTPException(410, "文件不存在，已自动从库中删除")
    return row


# ---------------- 应用工厂 ----------------
def create_app(config: AppConfig) -> FastAPI:
    stop_event = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动：初始化打标插件管理器
        tagging_manager.init_manager(lambda: config)
        # 启动后台校验线程（自动清理磁盘上已不存在的文件记录）
        interval = config.get_int("verify_interval_sec", 60)
        if interval > 0:
            def verify_loop() -> None:
                while not stop_event.wait(interval):
                    try:
                        library.verify_all()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("后台校验异常：%s", exc)
            threading.Thread(target=verify_loop, daemon=True, name="verify-loop").start()
            logger.info("后台校验线程已启动（间隔 %d 秒）", interval)
        yield
        stop_event.set()

    app = FastAPI(title="ImageDB", version="1.0.0", lifespan=lifespan)
    app.state.config = config

    # ================= 目录库 =================
    @app.get("/api/tree")
    def api_tree() -> dict:
        """返回目录树（纯数据库读取，启动时优先由此构建界面）。"""
        return library.build_tree()

    @app.post("/api/library/import")
    @app.post("/api/library/import")
    def api_import(req: ImportRequest) -> dict:
        """导入目录（后台任务）：先多线程统计文件数，再逐目录导入，带进度条。"""
        path = os.path.abspath(req.path)
        if not os.path.isdir(path):
            raise HTTPException(400, f"目录不存在：{path}")
        jid = _new_import_job(path)
        t = threading.Thread(target=_import_worker, args=(jid, path),
                             daemon=True, name=f"import-{jid}")
        t.start()
        return {"job_id": jid}

    @app.get("/api/library/import/jobs")
    def api_import_jobs() -> dict:
        """导入任务列表（含进度）。"""
        with IMPORT_JOBS_LOCK:
            return {"jobs": [dict(v) for v in IMPORT_JOBS.values()]}

    @app.get("/api/library/import/jobs/{jid}")
    def api_import_job(jid: str) -> dict:
        """单个导入任务进度。"""
        with IMPORT_JOBS_LOCK:
            job = IMPORT_JOBS.get(jid)
            if job is None:
                raise HTTPException(404, "任务不存在")
            return dict(job)
    @app.post("/api/library/rescan")
    def api_rescan(req: FolderIdRequest) -> dict:
        """重新扫描目录：补录新增、清理缺失。"""
        try:
            return library.rescan_folder(req.folder_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/library/remove")
    def api_remove(req: FolderIdRequest) -> dict:
        """把目录从库中移除（不影响磁盘文件）。"""
        return {"removed_media": library.remove_folder(req.folder_id)}

    @app.post("/api/library/verify")
    def api_verify() -> dict:
        """全库校验：磁盘上不存在的目录/文件自动清理。"""
        return library.verify_all()

    @app.post("/api/library/check")
    def api_check(req: FolderIdRequest) -> dict:
        """检查单个目录是否仍然存在（用户点击树节点时调用，不存在则自动清理）。"""
        return library.check_folder(req.folder_id)

    # ================= 媒体 =================
    @app.get("/api/media")
    def api_media(
        folder_id: Optional[int] = None,
        q: str = "",
        dir_q: str = "",
        tags: str = "",
        tag_any: bool = False,
        type: str = "",
        page: int = Query(1, ge=1),
        page_size: int = Query(60, ge=1, le=200),
        sort: str = "name",
    ) -> dict:
        """搜索/筛选媒体。"""
        return search_media(
            folder_id=folder_id, q=q, dir_q=dir_q, tags=tags,
            tag_any=tag_any, type=type, page=page, page_size=page_size, sort=sort,
        )

    @app.get("/api/media/{mid}")
    def api_media_detail(mid: int) -> dict:
        """媒体详情（含标签）。"""
        row = _require_media(mid, auto_clean=False)
        d = _media_to_dict(row)
        for t in query_all(
            """SELECT t.name, mt.confidence, mt.source FROM media_tags mt
               JOIN tags t ON t.id = mt.tag_id WHERE mt.media_id = ? ORDER BY mt.confidence DESC""",
            (mid,),
        ):
            d["tags"].append(t)
        return d

    @app.post("/api/media/delete")
    def api_media_delete(req: MediaDeleteRequest) -> dict:
        """批量从数据库删除媒体记录（不删除磁盘文件）。
        同时清理缩略图文件与无引用的标签。"""
        if not req.media_ids:
            raise HTTPException(400, "未指定媒体")
        ph = ",".join("?" * len(req.media_ids))
        # 删除缩略图文件
        for r in query_all(f"SELECT thumbnail FROM media_items WHERE id IN ({ph})", req.media_ids):
            if r["thumbnail"]:
                tp = os.path.join(DATA_DIR, r["thumbnail"])
                try:
                    if os.path.isfile(tp):
                        os.remove(tp)
                except OSError:
                    pass
        removed = execute_rowcount(
            f"DELETE FROM media_items WHERE id IN ({ph})", req.media_ids)
        return {"removed": removed}

    @app.post("/api/media/trash")
    def api_media_trash(req: MediaTrashRequest) -> dict:
        """把选中的媒体文件移到回收站。

        - 文件所在盘支持系统回收站（本地固定盘）→ send2trash 进系统回收站（文件管理器可还原）；
        - 文件所在盘不支持系统回收站（网络盘/可移动盘等）→ 移到本地 data/recycle_bin/ 作"应用内回收站"，
          本应用可还原，绝不彻底删除磁盘文件，确保任何情况都不丢数据。
        """
        if not req.media_ids:
            raise HTTPException(400, "未指定媒体")
        ph = ",".join("?" * len(req.media_ids))
        rows = query_all(
            "SELECT id, folder_id, path, filename, type, ext, size, mtime, thumbnail "
            f"FROM media_items WHERE id IN ({ph})", req.media_ids)
        os_trash = 0
        app_trash = 0
        for r in rows:
            try:
                if not os.path.exists(r["path"]):
                    # 文件已被外部删除：仅清理数据库记录
                    execute("DELETE FROM media_items WHERE id = ?", (r["id"],))
                    continue
                if _os_recycle_supported(r["path"]):
                    send2trash(r["path"])                       # 进系统回收站
                    media_service.delete_thumbnails([r["id"]])  # 清理缩略图缓存
                    os_trash += 1
                else:
                    # 该盘不支持系统回收站（如网络盘 K:）：移到应用内回收站，可还原
                    tags = query_all(
                        "SELECT mt.tag_id, mt.confidence, mt.source, t.name "
                        "FROM media_tags mt JOIN tags t ON t.id = mt.tag_id WHERE mt.media_id = ?",
                        (r["id"],))
                    _app_trash_move(r, tags)
                    app_trash += 1
                execute("DELETE FROM media_items WHERE id = ?", (r["id"],))
            except Exception as exc:  # noqa: BLE001
                logger.warning("移文件到回收站失败 id=%s：%s", r["id"], exc)
                continue  # 失败时保留记录，避免数据库与磁盘不一致
        execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM media_tags)")
        if os_trash and app_trash:
            msg = f"{os_trash} 个已移到系统回收站，{app_trash} 个已移入应用回收站（可还原）"
        elif os_trash:
            msg = f"{os_trash} 个已移到系统回收站"
        else:
            msg = f"{app_trash} 个已移入应用回收站（可用回收站功能还原）"
        return {"removed": os_trash + app_trash, "os_trash": os_trash,
                "app_trash": app_trash, "message": msg}


    @app.post("/api/media/move")
    def api_media_move(req: MediaMoveRequest) -> dict:
        """把选中的媒体文件移动到指定目录，并更新数据库记录（folder_id + path）。"""
        if not req.media_ids:
            raise HTTPException(400, "未指定媒体")
        dest_dir = os.path.abspath(req.dest_dir)
        if not os.path.isdir(dest_dir):
            raise HTTPException(400, f"目标目录不存在：{dest_dir}")
        folder = query_one("SELECT id FROM folders WHERE path = ?", (dest_dir,))
        if not folder:
            raise HTTPException(400, f"目标目录未入库：{dest_dir}（请先导入该目录）")
        ph = ",".join("?" * len(req.media_ids))
        rows = query_all(f"SELECT id, path, filename FROM media_items WHERE id IN ({ph})", req.media_ids)
        moved = 0
        for r in rows:
            if not os.path.exists(r["path"]):
                execute("DELETE FROM media_items WHERE id = ?", (r["id"],))
                continue
            src = r["path"]
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.abspath(dest) == os.path.abspath(src):
                continue
            try:
                if os.path.exists(dest):
                    stem, ext = os.path.splitext(os.path.basename(src))
                    i = 1
                    while os.path.exists(dest):
                        dest = os.path.join(dest_dir, f"{stem}_{i}{ext}")
                        i += 1
                os.rename(src, dest)
            except OSError:
                try:
                    shutil.move(src, dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("移动文件失败 id=%s：%s", r["id"], exc)
                    continue
            execute("UPDATE media_items SET path = ?, folder_id = ? WHERE id = ?",
                    (dest, folder["id"], r["id"]))
            moved += 1
        return {"moved": moved}

    # ================= 应用内回收站 =================
    @app.get("/api/recycle/list")
    def api_recycle_list() -> dict:
        """列出应用内回收站的素材（不含已进系统回收站的部分）。"""
        rows = query_all("SELECT * FROM recycle_bin ORDER BY id DESC")
        return {"items": rows}


    @app.post("/api/recycle/restore")
    def api_recycle_restore(req: RecycleIdsRequest) -> dict:
        """把应用内回收站的素材还原到原目录，并恢复数据库记录（含原标签、缩略图）。"""
        if not req.ids:
            raise HTTPException(400, "未指定回收站条目")
        ph = ",".join("?" * len(req.ids))
        rows = query_all(f"SELECT * FROM recycle_bin WHERE id IN ({ph})", req.ids)
        restored = 0
        errors: list[str] = []
        for r in rows:
            try:
                folder = query_one("SELECT id FROM folders WHERE id = ?", (r["folder_id"],))
                if not folder:
                    errors.append(f"{r['filename']}：原目录已从库中移除，无法还原")
                    continue
                if os.path.exists(r["path"]):
                    errors.append(f"{r['filename']}：原位置已存在同名文件，未还原")
                    continue
                if os.path.exists(r["stored_path"]):
                    shutil.move(r["stored_path"], r["path"])
                execute(
                    "INSERT INTO media_items(id, folder_id, path, filename, type, ext, size, "
                    "mtime, thumbnail, status) VALUES (?,?,?,?,?,?,?,?,?, 'ok')",
                    (r["media_id"], r["folder_id"], r["path"], r["filename"], r["type"],
                     r["ext"], r["size"], r["mtime"], r["thumbnail"]))
                if r.get("tags_json"):
                    for tg in json.loads(r["tags_json"]):
                        name = (tg.get("name") or "").strip()
                        if not name:
                            continue
                        tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
                        if tag_row:
                            tag_id = tag_row["id"]
                        else:
                            tag_id = execute(
                                "INSERT INTO tags(name, source) VALUES (?, ?)",
                                (name, tg.get("source", "manual")))
                        execute(
                            "INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source) "
                            "VALUES (?,?,?,?)",
                            (r["media_id"], tag_id, tg.get("confidence", 1.0),
                             tg.get("source", "manual")))
                execute("DELETE FROM recycle_bin WHERE id = ?", (r["id"],))
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("还原失败 id=%s：%s", r["id"], exc)
                errors.append(f"{r['filename']}：{exc}")
        return {"restored": restored, "errors": errors}


    @app.post("/api/recycle/delete")
    def api_recycle_delete(req: RecycleIdsRequest) -> dict:
        """从应用内回收站中永久删除素材（前端需二次确认）。"""
        if not req.ids:
            raise HTTPException(400, "未指定回收站条目")
        ph = ",".join("?" * len(req.ids))
        rows = query_all(f"SELECT * FROM recycle_bin WHERE id IN ({ph})", req.ids)
        deleted = 0
        for r in rows:
            try:
                media_service.delete_thumbnails([r["media_id"]])
                if os.path.exists(r["stored_path"]):
                    os.remove(r["stored_path"])
                execute("DELETE FROM recycle_bin WHERE id = ?", (r["id"],))
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("彻底删除失败 id=%s：%s", r["id"], exc)
        return {"deleted": deleted}


    @app.post("/api/media/add")
    def api_media_add(req: MediaAddRequest) -> dict:
        """向指定目录添加单个媒体文件记录（文件须已存在于磁盘）。
        用于用户手动把新图片放进目录后，从库外补录单张。"""
        folder = query_one("SELECT * FROM folders WHERE id = ?", (req.folder_id,))
        if not folder:
            raise HTTPException(404, "目录不存在")
        path = os.path.abspath(req.path)
        if not os.path.isfile(path):
            raise HTTPException(400, f"文件不存在：{path}")
        mtype = library.media_type_of(path)
        if not mtype:
            raise HTTPException(400, f"不支持的媒体类型：{path}")
        st = os.stat(path)
        # 若已存在相同路径则更新，否则插入
        exist = query_one("SELECT id FROM media_items WHERE path = ?", (path,))
        if exist:
            return {"media_id": exist["id"], "added": 0}
        media_id = execute(
            """INSERT INTO media_items(folder_id, path, filename, type, ext, size, mtime)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (req.folder_id, path, os.path.basename(path), mtype,
             library.ext_of(path), st.st_size, st.st_mtime),
        )
        # 立即生成缩略图（后台异步，避免阻塞）
        def _gen():
            media_service.make_thumbnail(
                media_id, path, mtype,
                thumb_sec=config.get_float("video_thumb_frame_sec", 1.0),
                thumb_size=config.get_int("thumb_size", 320),
            )
        threading.Thread(target=_gen, daemon=True, name="thumb-gen").start()
        return {"media_id": media_id, "added": 1}

    @app.get("/api/media/{mid}/file")
    def api_media_file(mid: int):
        """流式读取原文件（FileResponse 支持视频 Range 拖动）。"""
        row = _require_media(mid)
        return FileResponse(row["path"], filename=row["filename"])

    @app.get("/api/media/{mid}/thumbnail")
    def api_media_thumbnail(mid: int, regenerate: bool = False):
        """缩略图：已存在直接返回，否则自动生成（视频取指定秒数的帧）。"""
        row = _require_media(mid)
        if row["thumbnail"] and not regenerate:
            abs_path = os.path.join(DATA_DIR, row["thumbnail"])
            if os.path.isfile(abs_path):
                try:
                    os.utime(abs_path, None)
                except OSError:
                    pass
                _maybe_cleanup_thumbs()
                return FileResponse(abs_path)
        cfg = config
        media_service.make_thumbnail(
            mid, row["path"], row["type"],
            thumb_sec=cfg.get_float("video_thumb_frame_sec", 1.0),
            thumb_size=cfg.get_int("thumb_size", 320),
        )
        row2 = query_one("SELECT thumbnail FROM media_items WHERE id = ?", (mid,))
        if row2 and row2["thumbnail"]:
            abs_path = os.path.join(DATA_DIR, row2["thumbnail"])
            if os.path.isfile(abs_path):
                return FileResponse(abs_path)
        raise HTTPException(500, "缩略图生成失败（可能需要安装 Pillow / OpenCV）")

    @app.get("/api/media/{mid}/frames")
    def api_media_frames(mid: int, interval: Optional[float] = None):
        """视频抽帧（供打标预览），返回帧图 URL 列表。"""
        row = _require_media(mid)
        if row["type"] != "video":
            raise HTTPException(400, "仅视频支持抽帧")
        cfg = config
        iv = interval if interval and interval > 0 else cfg.get_float("video_frame_interval_sec", 5.0)
        mf = cfg.get_int("video_max_frames", 20)
        try:
            paths = media_service.extract_frames(row["path"], iv, mf)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"抽帧失败：{exc}")
        urls = []
        for p in paths:
            rel = os.path.relpath(p, FRAMES_DIR).replace(os.sep, "/")
            urls.append("/frames/" + rel)
        return {"frames": urls}

    @app.post("/api/media/{mid}/tags")
    def api_add_tags(mid: int, req: TagListRequest) -> dict:
        """手动添加标签（source=manual）。"""
        row = _require_media(mid, auto_clean=False)
        added = 0
        for name in req.tags:
            name = (name or "").strip()
            if not name:
                continue
            tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
            tag_id = tag_row["id"] if tag_row else execute(
                "INSERT INTO tags(name, source) VALUES (?, 'manual')", (name,))
            added += execute_rowcount(
                "INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'manual')",
                (mid, tag_id),
            )
        return {"added": added}

    @app.post("/api/media/{mid}/tags/remove")
    def api_remove_tags(mid: int, req: TagListRequest) -> dict:
        """移除标签（任意来源）。"""
        row = _require_media(mid, auto_clean=False)
        removed = 0
        for name in req.tags:
            tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
            if tag_row:
                removed += execute_rowcount(
                    "DELETE FROM media_tags WHERE media_id = ? AND tag_id = ?",
                    (mid, tag_row["id"]),
                )
        return {"removed": removed}

    @app.get("/api/tags")
    def api_tags(q: str = "", limit: int = 200) -> dict:
        """标签自动补全列表（含使用次数）。"""
        like = f"%{q}%" if q else "%"
        rows = query_all(
            """SELECT t.name, COUNT(mt.media_id) AS c FROM tags t
               LEFT JOIN media_tags mt ON mt.tag_id = t.id
               WHERE t.name LIKE ? GROUP BY t.id ORDER BY c DESC, t.name LIMIT ?""",
            (like, limit),
        )
        return {"tags": [{"name": r["name"], "count": r["c"]} for r in rows]}
    @app.get("/api/library/{folder_id}/tags")
    def api_folder_tags(folder_id: int) -> dict:
        """统计某目录（含子目录）下所有媒体标签的聚合（名称 + 媒体数 + 覆盖数）。"""
        # 校验目录存在
        folder = query_one("SELECT id FROM folders WHERE id = ?", (folder_id,))
        if not folder:
            raise HTTPException(404, "目录不存在")
        rows = query_all(
            """WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               )
               SELECT t.name, COUNT(mt.media_id) AS media_count
               FROM tags t
               JOIN media_tags mt ON mt.tag_id = t.id
               JOIN media_items m ON m.id = mt.media_id
               WHERE m.folder_id IN (SELECT id FROM sub)
               GROUP BY t.id, t.name
               ORDER BY media_count DESC, t.name""",
            (folder_id,),
        )
        return {"tags": [{"name": r["name"], "media_count": r["media_count"]} for r in rows]}

    @app.post("/api/library/{folder_id}/tags")
    def api_folder_tag_apply(folder_id: int, req: FolderTagRequest) -> dict:
        """对整个目录（含子目录）的所有媒体批量添加/移除标签。"""
        folder = query_one("SELECT id FROM folders WHERE id = ?", (folder_id,))
        if not folder:
            raise HTTPException(404, "目录不存在")
        # 收集目录子树下所有媒体 id
        media_ids = [r["id"] for r in query_all(
            """WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               )
               SELECT m.id FROM media_items m JOIN sub s ON m.folder_id = s.id""",
            (folder_id,),
        )]
        if not media_ids:
            raise HTTPException(400, "该目录下没有媒体文件")
        count = 0
        for name in req.tags:
            name = (name or "").strip()
            if not name:
                continue
            if req.action == "add":
                tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
                tag_id = tag_row["id"] if tag_row else execute(
                    "INSERT INTO tags(name, source) VALUES (?, 'manual')", (name,))
                executemany(
                    "INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'manual')",
                    [(mid, tag_id) for mid in media_ids],
                )
                count += 1
            else:
                tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
                if tag_row:
                    # 整个目录子树可能上万：分块删除，避免 IN (...) 绑定变量超限
                    for chunk in tagging_manager._chunked(media_ids):
                        ph = ",".join("?" * len(chunk))
                        count += execute_rowcount(
                            "DELETE FROM media_tags WHERE tag_id = ? AND media_id IN (%s)" % ph,
                            [tag_row["id"]] + chunk,
                        )
        return {"ok": True, "count": count, "media": len(media_ids)}


    @app.post("/api/media/tags/batch")
    def api_batch_tags(req: BatchTagRequest) -> dict:
        """批量添加/移除标签：应用到所有指定媒体（右侧边栏多选时使用）。"""
        if not req.media_ids:
            raise HTTPException(400, "未指定媒体")
        # 校验媒体都存在
        ph = ",".join("?" * len(req.media_ids))
        exist = query_one(
            f"SELECT COUNT(*) AS c FROM media_items WHERE id IN ({ph})", req.media_ids)["c"]
        if exist != len(req.media_ids):
            raise HTTPException(404, "部分媒体不存在（可能已被外部删除）")
        count = 0
        for name in req.tags:
            name = (name or "").strip()
            if not name:
                continue
            if req.action == "add":
                # 确保标签存在
                tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
                tag_id = tag_row["id"] if tag_row else execute(
                    "INSERT INTO tags(name, source) VALUES (?, 'manual')", (name,))
                # 批量关联（INSERT OR IGNORE 避免重复）
                executemany(
                    """INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source)
                       VALUES (?, ?, 1.0, 'manual')""",
                    [(mid, tag_id) for mid in req.media_ids],
                )
                count += 1
            else:
                tag_row = query_one("SELECT id FROM tags WHERE name = ?", (name,))
                if tag_row:
                    count += execute_rowcount(
                        f"DELETE FROM media_tags WHERE tag_id = ? AND media_id IN ({ph})",
                        [tag_row["id"]] + req.media_ids,
                    )
        return {"ok": True, "count": count}

    @app.post("/api/tags/rename")
    def api_rename_tag(req: TagRenameRequest) -> dict:
        """全局重命名标签：所有含有旧标签的媒体自动变为新标签。"""
        old = (req.old_name or "").strip()
        new = (req.new_name or "").strip()
        if not old or not new:
            raise HTTPException(400, "标签名不能为空")
        if old == new:
            return {"ok": True}
        old_row = query_one("SELECT id FROM tags WHERE name = ?", (old,))
        if old_row is None:
            raise HTTPException(404, f"标签不存在：{old}")
        new_row = query_one("SELECT id FROM tags WHERE name = ?", (new,))
        if new_row:
            # 新标签已存在：把旧标签的关联全部迁移到新标签（去重）
            execute(
                """INSERT OR IGNORE INTO media_tags(media_id, tag_id, confidence, source)
                   SELECT media_id, ?, confidence, source FROM media_tags WHERE tag_id = ?""",
                (new_row["id"], old_row["id"]),
            )
            execute("DELETE FROM media_tags WHERE tag_id = ?", (old_row["id"],))
            execute("DELETE FROM tags WHERE id = ?", (old_row["id"],))
        else:
            execute("UPDATE tags SET name = ? WHERE id = ?", (new, old_row["id"]))
        return {"ok": True}

    @app.post("/api/tags/delete")
    def api_delete_tag(req: TagDeleteRequest) -> dict:
        """全局删除标签：所有含有该标签的媒体同时移除。"""
        name = (req.name or "").strip()
        if not name:
            raise HTTPException(400, "标签名不能为空")
        removed = execute_rowcount("DELETE FROM tags WHERE name = ?", (name,))
        return {"ok": True, "removed": removed}

    # ================= 打标 =================
    @app.get("/api/tagging/tools")
    def api_tagging_tools() -> dict:
        """打标工具列表（含加载状态与配置）。"""
        mgr = tagging_manager.get_manager()
        return {"tools": mgr.list_tools() if mgr else []}

    @app.post("/api/tagging/run")
    def api_tagging_run(req: TagRunRequest) -> dict:
        """启动打标任务。"""
        mgr = tagging_manager.get_manager()
        if mgr is None:
            raise HTTPException(500, "打标插件管理器未初始化")
        try:
            job_id = mgr.start_job(req.tool, req.scope_type, req.scope_ids, req.overwrite)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"job_id": job_id}

    @app.get("/api/tagging/jobs")
    def api_tagging_jobs() -> dict:
        """最近打标任务列表。"""
        mgr = tagging_manager.get_manager()
        return {"jobs": mgr.list_jobs() if mgr else []}

    @app.get("/api/tagging/jobs/{jid}")
    def api_tagging_job(jid: int) -> dict:
        """单个任务进度。"""
        mgr = tagging_manager.get_manager()
        job = mgr.get_job(jid) if mgr else None
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job

    @app.post("/api/tagging/jobs/{jid}/cancel")
    def api_tagging_cancel(jid: int) -> dict:
        """取消任务（工作线程会在下一个文件处检查状态并停止）。"""
        execute("""UPDATE tag_jobs SET status='cancelled'
                   WHERE id = ? AND status IN ('pending', 'running')""", (jid,))
        return {"ok": True}

    @app.post("/api/tagging/reload")
    def api_tagging_reload() -> dict:
        """重新加载打标插件（设置变更后调用）。"""
        mgr = tagging_manager.get_manager()
        if mgr:
            mgr.reload()
        return {"ok": True}

    # ================= 设置 / 下载 =================
    @app.get("/api/settings")
    def api_get_settings() -> dict:
        """读取全部设置（工具配置展开为 JSON 对象）。"""
        config.reload()
        data = config.to_dict()
        for k, v in list(data.items()):
            if k.startswith("tool_"):
                try:
                    data[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass
        data["data_dir"] = os.path.join(BASE_DIR, "data")
        return {"settings": data}

    @app.put("/api/settings")
    def api_update_settings(req: SettingsUpdateRequest) -> dict:
        """保存设置（全量合并），随后重载配置与插件。"""
        for k, v in req.settings.items():
            if k.startswith("tool_"):
                if not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
            elif not isinstance(v, str):
                v = str(v)
            execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, v))
        config.reload()
        # 插件配置可能变化 → 重新加载插件
        try:
            mgr = tagging_manager.get_manager()
            if mgr:
                mgr.reload()
        except Exception as exc:  # noqa: BLE001
            logger.warning("插件重载失败：%s", exc)
        return {"ok": True}

    @app.post("/api/settings/test-proxy")
    def api_test_proxy(req: ProxyTestRequest) -> dict:
        """测试代理连通性。"""
        return test_proxy(req.proxy)

    @app.post("/api/models/download")
    def api_model_download(req: ModelDownloadRequest) -> dict:
        """从 HuggingFace 下载模型（走代理 + 可选 HF Token 访问受限模型）。
        自动把 model_dir 指向下载目录。"""
        tool = (req.tool or "cl_tagger").strip()
        dest = os.path.join(os.path.join(BASE_DIR, "data", "models"), tool)
        # 自动更新该工具的 model_dir 配置
        raw = config.get(f"tool_{tool}", "{}") or "{}"
        try:
            tcfg = json.loads(raw)
        except (ValueError, TypeError):
            tcfg = {}
        tcfg["model_dir"] = dest
        execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (f"tool_{tool}", json.dumps(tcfg, ensure_ascii=False)))
        config.reload()
        # 读取用户在设置页配置的 HuggingFace Token（用于 gated 模型）
        hf_token = config.get("hf_token", "") or ""
        jid = download_model(req.repo_id, dest, config.proxy_dict(), token=hf_token.strip() or None)
        return {"job_id": jid, "model_dir": dest}

    @app.get("/api/downloads")
    def api_downloads() -> dict:
        """下载/依赖更新任务列表。"""
        return {"jobs": list_dl_jobs()}

    @app.post("/api/deps/update")
    def api_deps_update(req: DepsUpdateRequest) -> dict:
        """用 pip 更新依赖（应用代理）。"""
        jid = update_deps(req.packages, config.proxy_dict())
        return {"job_id": jid}

    @app.post("/api/deps/install-directml")
    def api_install_directml() -> dict:
        """一键安装 onnxruntime-directml（GPU 加速，5090 必备）。
        自动卸载冲突的普通 onnxruntime 并安装 DirectML 版。"""
        jid = install_directml(config.proxy_dict())
        return {"job_id": jid}

    # ================= 静态资源 =================
    os.makedirs(THUMBS_DIR, exist_ok=True)
    os.makedirs(FRAMES_DIR, exist_ok=True)
    # 缩略图与抽帧图直接以静态文件方式提供
    app.mount("/thumbs", StaticFiles(directory=THUMBS_DIR), name="thumbs")
    app.mount("/frames", StaticFiles(directory=FRAMES_DIR), name="frames")
    # 前端页面（必须是最后一个挂载点，避免吞掉 API 路由）
    if os.path.isdir(WEB_DIR):
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    else:
        logger.warning("前端目录不存在：%s", WEB_DIR)

    return app
