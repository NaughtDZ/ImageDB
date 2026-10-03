# -*- coding: utf-8 -*-
"""通用后台任务注册表（带进度，供前端轮询）。

为什么需要：
    有些操作很慢——网络盘上遍历上千个目录（.imgtag 导出/导入/自检、目录重新扫描），
    如果放在 HTTP 请求里同步跑完，前端就只能干等，界面看起来像卡死。
    这里提供统一机制：**起任务 → 立即返回 job_id → 前端轮询进度**。

用法：
    jid = jobs.new_job("scan", "重新扫描目录")
    jobs.run_async(jid, lambda: do_work(jobs.progress_cb(jid)))

任务记录字段：
    id / kind / label / status(running|done|failed) / total / done / progress(0-100)
    / message / result / error / started_at / finished_at
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger("imagedb.jobs")

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
MAX_JOBS = 50


def new_job(kind: str, label: str) -> str:
    """新建任务记录，返回 12 位 job_id。超过上限时丢弃最旧的。"""
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(JOBS) >= MAX_JOBS:
            for k in sorted(JOBS, key=lambda x: JOBS[x]["_ts"])[: len(JOBS) - MAX_JOBS + 1]:
                JOBS.pop(k, None)
        JOBS[jid] = {
            "id": jid, "kind": kind, "label": label, "status": "running",
            "total": 0, "done": 0, "progress": 0,
            "message": "准备中…", "result": None, "error": None,
            "_ts": time.time(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return jid


def progress_cb(jid: str):
    """生成进度回调：cb(done, total, message)。进度最高只报 99，完成由 finish 置 100。"""
    def cb(done: int, total: int, message: str = "") -> None:
        pct = int(done / total * 100) if total else 0
        with _LOCK:
            job = JOBS.get(jid)
            if job:
                job.update(done=done, total=total, progress=min(99, pct),
                           message=message or (str(done) + "/" + str(total)))
    return cb


def percent_cb(jid: str, cap: int = 99):
    """生成「绝对百分比」进度回调：cb(pct, message)。

    用于**多阶段**任务（例如目录扫描 = 遍历磁盘 + 逐目录同步）：各阶段自己把
    进度折算成总百分比，避免每段都从 0 跑到 100 再跳回去。
    计数信息直接写进 message（前端会原样显示）。
    """
    def cb(pct, message: str = "") -> None:
        with _LOCK:
            job = JOBS.get(jid)
            if job:
                job.update(progress=max(0, min(cap, int(pct))),
                           message=message or job.get("message"))
    return cb


def finish(jid: str, status: str, result=None, error: str | None = None) -> None:
    """结束任务：status 取 done / failed。"""
    with _LOCK:
        job = JOBS.get(jid)
        if job:
            job.update(status=status, result=result, error=error,
                       progress=100 if status == "done" else job.get("progress", 0),
                       message="完成" if status == "done" else (error or "失败"),
                       finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))


def run_async(jid: str, fn, name: str = "job") -> str:
    """在后台线程里跑 fn()，完成后写入 result；异常写 error（绝不让线程把进程带崩）。"""
    def worker() -> None:
        try:
            finish(jid, "done", result=fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("后台任务失败 %s（%s）：%s", jid, name, exc)
            finish(jid, "failed", error=str(exc))
    threading.Thread(target=worker, daemon=True, name=name + "-" + jid).start()
    return jid


def get_job(jid: str) -> dict | None:
    with _LOCK:
        job = JOBS.get(jid)
        return dict(job) if job else None


def list_jobs(limit: int = 30) -> list[dict]:
    """最近任务（不含 result，避免响应过大）。"""
    with _LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["_ts"], reverse=True)
        return [{k: v for k, v in j.items() if k not in ("result", "_ts")} for j in items[:limit]]


def busy(kind: str | None = None) -> dict | None:
    """是否已有同类任务在跑（用于互斥，避免重复点造成并发写库）。"""
    with _LOCK:
        for j in JOBS.values():
            if j["status"] == "running" and (kind is None or j["kind"] == kind):
                return dict(j)
    return None
