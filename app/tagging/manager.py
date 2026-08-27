# -*- coding: utf-8 -*-
"""
打标插件管理器
==============
职责：
    1. 扫描 app/tagging/plugins/ 目录，自动发现并注册打标插件；
    2. 提供插件列表（供设置页与打标对话框使用）；
    3. 执行打标任务（单图 / 多图 / 整个目录），进度写入 tag_jobs 表；
    4. 视频打标：自动抽帧 → 逐帧打标 → 聚合标签（由基类实现）。

解耦说明：
    - 程序本体只与 PluginManager 交互；
    - 新增/移除打标工具不需要修改程序本体，只需增删插件文件。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import threading

from .. import media as media_service
from ..database import execute, query_all, query_one
from .base import TaggerPlugin

logger = logging.getLogger("imagedb.tagging.manager")

# 插件目录（app/tagging/plugins/）
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


class PluginManager:
    """打标插件管理器。"""

    def __init__(self, config_provider):
        """
        config_provider：可调用对象，返回 AppConfig 实例
        （传入 lambda，避免与配置模块循环依赖）。
        """
        self._config_provider = config_provider
        self._plugins: dict[str, TaggerPlugin] = {}
        self._lock = threading.Lock()
        self._discover()

    # ---- 插件发现 ----
    def _discover(self) -> None:
        """扫描插件目录，加载所有 TaggerPlugin 子类。"""
        if not os.path.isdir(PLUGINS_DIR):
            logger.warning("插件目录不存在：%s", PLUGINS_DIR)
            return
        for fname in sorted(os.listdir(PLUGINS_DIR)):
            if not fname.endswith(".py") or fname.startswith("__"):
                continue
            mod_name = fname[:-3]
            try:
                mod = importlib.import_module(f".plugins.{mod_name}", package=__package__)
                cls = getattr(mod, "PLUGIN_CLASS", None)
                if cls is None:
                    # 退而求其次：查找模块内的 TaggerPlugin 子类
                    for attr in vars(mod).values():
                        if (isinstance(attr, type) and issubclass(attr, TaggerPlugin)
                                and attr is not TaggerPlugin):
                            cls = attr
                            break
                if cls is None:
                    logger.warning("插件 %s 未导出 PLUGIN_CLASS，已跳过", fname)
                    continue
                cfg = self._plugin_config(cls.name)
                plugin = cls(cfg)
                plugin.set_proxy(self._proxy_config())
                self._plugins[cls.name] = plugin
                logger.info("已发现打标插件：%s（%s）", plugin.display_name, plugin.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("加载插件 %s 失败：%s", fname, exc)

    def _plugin_config(self, tool_name: str) -> dict:
        """从配置读取某个工具的 JSON 配置。"""
        cfg = self._config_provider()
        return cfg.tool_config(tool_name)

    def _proxy_config(self) -> dict:
        cfg = self._config_provider()
        return cfg.proxy_dict()

    # ---- 对外接口 ----
    def list_tools(self) -> list[dict]:
        """返回所有插件信息（含加载状态、错误信息）。"""
        with self._lock:
            return [p.to_dict() for p in self._plugins.values()]

    def get(self, name: str) -> TaggerPlugin | None:
        return self._plugins.get(name)

    def reload(self) -> None:
        """重新加载插件（设置变更后调用）。"""
        with self._lock:
            for p in self._plugins.values():
                try:
                    p.unload()
                except Exception:  # noqa: BLE001
                    pass
            self._plugins.clear()
        self._discover()

    # ---- 打标任务 ----
    def start_job(self, tool: str, scope_type: str, scope_ids: list[int],
                  overwrite: bool = False) -> int:
        """
        启动一个打标任务（异步执行，进度写入 tag_jobs 表）。
        scope_type: 'media'（具体媒体 id 列表）或 'folder'（整个目录子树）
        """
        plugin = self.get(tool)
        if plugin is None:
            raise ValueError(f"打标工具不存在：{tool}")
        job_id = execute(
            """INSERT INTO tag_jobs(scope_type, scope_ids, tool, overwrite, status, total, done)
               VALUES (?, ?, ?, ?, 'pending', 0, 0)""",
            (scope_type, json.dumps(scope_ids), tool, 1 if overwrite else 0),
        )
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, tool, scope_type, scope_ids, overwrite),
            daemon=True,
            name=f"tag-job-{job_id}",
        )
        thread.start()
        logger.info("打标任务已启动：job=%s tool=%s scope=%s ids=%s",
                    job_id, tool, scope_type, scope_ids[:20])
        return job_id

    # ---- 任务执行 ----
    def _run_job(self, job_id: int, tool: str, scope_type: str,
                 scope_ids: list[int], overwrite: bool) -> None:
        """后台执行打标任务（工作线程）。"""
        plugin = self.get(tool)
        if plugin is None:
            self._finish_job(job_id, "failed", "工具已被移除")
            return

        # 解析目标媒体 id 列表
        if scope_type == "folder":
            target_ids: list[int] = []
            for fid in scope_ids:
                target_ids.extend(self._media_in_folders(fid))
        else:
            target_ids = list(scope_ids)

        if not target_ids:
            self._finish_job(job_id, "failed", "没有可打标的媒体文件")
            return

        total = len(target_ids)
        execute("UPDATE tag_jobs SET status='running', total=? WHERE id=?", (total, job_id))

        # 懒加载模型
        if not plugin.is_loaded and not plugin.load():
            self._finish_job(job_id, "failed", plugin.error or "模型加载失败")
            return

        # 读取并行打标量（每次批量推理的图片数）
        cfg_provider = self._config_provider()
        parallel = cfg_provider.get_int("tagging_parallel", 4)
        parallel = max(1, min(parallel, 64))   # 限制在 1~64

        # 预取全部媒体行（减少逐条查询数据库）
        ph = ",".join("?" * len(target_ids))
        rows_by_id: dict[int, dict] = {}
        for r in query_all(
            f"SELECT * FROM media_items WHERE id IN ({ph})", target_ids):
            rows_by_id[r["id"]] = r

        done = 0
        batch_rows: list[dict] = []
        for mid in target_ids:
            # 支持取消：每批处理前检查一次任务状态
            st = query_one("SELECT status FROM tag_jobs WHERE id = ?", (job_id,))
            if st and st["status"] == "cancelled":
                self._finish_job(job_id, "cancelled", "用户取消")
                return
            row = rows_by_id.get(mid)
            if row is None:
                done += 1
                self._update_progress(job_id, done, total)
                continue
            batch_rows.append(row)
            # 凑满一批后批量打标
            if len(batch_rows) >= parallel:
                done = self._process_batch(job_id, plugin, batch_rows, tool,
                                           overwrite, done, total)
                batch_rows = []
        # 处理剩余不足一批的
        if batch_rows:
            done = self._process_batch(job_id, plugin, batch_rows, tool,
                                       overwrite, done, total)

        self._finish_job(job_id, "done")

    def _process_batch(self, job_id: int, plugin: TaggerPlugin, rows: list[dict],
                       tool: str, overwrite: bool, done: int, total: int) -> int:
        """处理一批媒体：批量推理 + 写入标签 + 更新进度。"""
        try:
            results_list = self._tag_batch(plugin, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量处理失败：%s", exc)
            results_list = [[] for _ in rows]
        for row, tags in zip(rows, results_list):
            if tags:
                self._write_tags(row["id"], tags, tool, overwrite)
            done += 1
            self._update_progress(job_id, done, total)
        return done

    def _tag_one(self, plugin: TaggerPlugin, row: dict) -> list:
        """对单个媒体打标：图片直接打标；视频先抽帧再聚合。"""
        if row["type"] == "image":
            return plugin.tag_image(row["path"])

        # 视频：按配置抽帧
        cfg = self._config_provider()
        interval = cfg.get_float("video_frame_interval_sec", 5.0)
        max_frames = cfg.get_int("video_max_frames", 20)
        frames = media_service.extract_frames(row["path"], interval, max_frames)
        try:
            results = plugin.tag_video_frames(frames)
        finally:
            # 无论成功失败都清理临时帧文件
            media_service.cleanup_frames(frames)
        return results

    def _tag_image_safe(self, plugin: TaggerPlugin, row: dict) -> list:
        """线程安全地打标单张图片（供 CPU 线程池调用）。"""
        try:
            return plugin.tag_image(row["path"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("打标失败 id=%s：%s", row["id"], exc)
            return []

    def _tag_batch(self, plugin: TaggerPlugin, rows: list[dict]) -> list[list]:
        """
        批量打标（并行核心）：返回与 rows 等长的标签列表。
        - 图片：GPU（DirectML）→ batch 推理；CPU → 线程池并行；
        - 视频：逐条抽帧打标（帧内部也走批量推理）。
        """
        img_rows = [r for r in rows if r["type"] == "image"]
        vid_rows = [r for r in rows if r["type"] == "video"]
        result_map: dict[int, list] = {}

        # 图片并行打标
        if img_rows:
            if hasattr(plugin, "tag_images"):
                paths = [r["path"] for r in img_rows]
                providers = getattr(plugin, "_providers", []) or []
                is_gpu = any(("Dml" in p or "CUDA" in p or "Tensorrt" in p
                              or "ROCm" in p) for p in providers)
                if is_gpu:
                    # GPU：batch 推理（一次 session.run 处理多张，充分利用 GPU 并行度）
                    try:
                        results_list = plugin.tag_images(paths)
                        for r, tags in zip(img_rows, results_list):
                            result_map[r["id"]] = tags
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("GPU 批量打标失败（%d 张）：%s", len(img_rows), exc)
                        for r in img_rows:
                            result_map[r["id"]] = self._tag_image_safe(plugin, r)
                else:
                    # CPU：线程池并行（多核同时推理，比 batch 更快）
                    import concurrent.futures as cf
                    parallel = self._config_provider().get_int("tagging_parallel", 4)
                    parallel = max(1, min(parallel, 32))
                    with cf.ThreadPoolExecutor(max_workers=parallel) as pool:
                        future_map = {pool.submit(self._tag_image_safe, plugin, r): r
                                      for r in img_rows}
                        for fut in cf.as_completed(future_map):
                            r = future_map[fut]
                            try:
                                result_map[r["id"]] = fut.result()
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("线程打标失败 id=%s：%s", r["id"], exc)
                                result_map[r["id"]] = []
            else:
                for r in img_rows:
                    result_map[r["id"]] = self._tag_image_safe(plugin, r)

        # 视频：逐条抽帧打标
        for r in vid_rows:
            try:
                result_map[r["id"]] = self._tag_one(plugin, r)
            except Exception as exc:  # noqa: BLE001
                logger.warning("视频打标失败 id=%s：%s", r["id"], exc)
                result_map[r["id"]] = []

        return [result_map.get(r["id"], []) for r in rows]

    def _write_tags(self, media_id: int, results: list,
                    source: str, overwrite: bool) -> None:
        """把标签写入数据库（去重；overwrite 时替换同来源旧标签）。"""
        if overwrite:
            # 删除该来源的旧标签，避免堆积
            execute("""DELETE FROM media_tags WHERE media_id = ?
                       AND source = ? AND source != 'manual'""", (media_id, source))
        existing = set()
        if not overwrite:
            for r in query_all("SELECT tag_id FROM media_tags WHERE media_id = ?", (media_id,)):
                existing.add(r["tag_id"])

        for r in results:
            tag = (r.tag or "").strip()
            if not tag:
                continue
            # 插入标签（若已存在则获取 id）
            tag_row = query_one("SELECT id FROM tags WHERE name = ?", (tag,))
            if tag_row:
                tag_id = tag_row["id"]
            else:
                tag_id = execute("INSERT INTO tags(name, source) VALUES (?, ?)", (tag, source))
            if tag_id in existing:
                continue
            execute(
                """INSERT OR REPLACE INTO media_tags(media_id, tag_id, confidence, source)
                   VALUES (?, ?, ?, ?)""",
                (media_id, tag_id, round(max(0.0, min(1.0, r.confidence)), 4), source),
            )
            existing.add(tag_id)

    def _media_in_folders(self, folder_id: int) -> list[int]:
        """收集目录子树下的所有媒体 id。"""
        rows = query_all(
            """WITH RECURSIVE sub(id) AS (
                   SELECT id FROM folders WHERE id = ?
                   UNION ALL
                   SELECT f.id FROM folders f JOIN sub s ON f.parent_id = s.id
               )
               SELECT m.id FROM media_items m JOIN sub s ON m.folder_id = s.id""",
            (folder_id,),
        )
        return [r["id"] for r in rows]

    # ---- 任务进度 ----
    def _update_progress(self, job_id: int, done: int, total: int) -> None:
        execute("UPDATE tag_jobs SET done = ? WHERE id = ?", (done, job_id))

    def _finish_job(self, job_id: int, status: str, message: str | None = None) -> None:
        execute(
            """UPDATE tag_jobs SET status = ?, message = ?,
               finished_at = datetime('now','localtime') WHERE id = ?""",
            (status, message or "", job_id),
        )

    def get_job(self, job_id: int) -> dict | None:
        return query_one("SELECT * FROM tag_jobs WHERE id = ?", (job_id,))

    def list_jobs(self, limit: int = 30) -> list[dict]:
        return query_all("SELECT * FROM tag_jobs ORDER BY id DESC LIMIT ?", (limit,))


# ---- 模块级单例（由 server 在启动时初始化）----
_manager: PluginManager | None = None


def init_manager(config_provider) -> PluginManager:
    """初始化插件管理器单例。"""
    global _manager
    _manager = PluginManager(config_provider)
    return _manager


def get_manager() -> PluginManager | None:
    """获取插件管理器单例（未初始化时返回 None）。"""
    return _manager
