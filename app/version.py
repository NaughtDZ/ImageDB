# -*- coding: utf-8 -*-
"""
运行版本信息模块
================
用途：让「当前跑的是不是最新代码」一眼可查，避免"改了代码但进程还是旧的"这种误判。

实现要点：
    - 不调用 git 命令（避免子进程/权限问题），直接读 .git/HEAD 与对应 ref 文件；
    - 读不到就降级为 unknown，绝不影响程序运行；
    - 进程启动时间在导入时固定，用于显示"已运行多久"。
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import time

logger = logging.getLogger("imagedb.version")

# 程序根目录（version.py 在 app/ 下）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 进程启动时间（导入本模块时固定）
_START_TS = time.time()
_START_STR = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_START_TS))

_cache: dict = {}
_CODE_EXTS = (".py", ".js", ".html", ".css")


def _fmt(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _read_git_commit() -> tuple[str, str, float | None]:
    """读取当前 commit，返回 (完整 hash, 短 hash, ref 文件 mtime)；失败返回 ("", "", None)。"""
    git = os.path.join(BASE_DIR, ".git")
    head_path = os.path.join(git, "HEAD")
    try:
        with open(head_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except OSError:
        return "", "", None

    if content.startswith("ref:"):
        ref = content[4:].strip()
        loose = os.path.join(git, *ref.split("/"))
        try:
            with open(loose, "r", encoding="utf-8") as f:
                h = f.read().strip()
            try:
                mt = os.path.getmtime(loose)
            except OSError:
                mt = None
            return h, h[:7], mt
        except OSError:
            pass
        # 松散 ref 不存在时可能在 packed-refs 里
        try:
            with open(os.path.join(git, "packed-refs"), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith(("#", "^")) and line.endswith(" " + ref):
                        h = line.split(" ", 1)[0]
                        return h, h[:7], None
        except OSError:
            pass
        return "", "", None

    # detached HEAD：HEAD 内容就是 commit
    return content, content[:7], None


def code_mtime() -> float | None:
    """源码最后修改时间（app/、web/、main.py 里 .py/.js/.html/.css 的最大 mtime）。"""
    newest: float | None = None
    targets = [os.path.join(BASE_DIR, "main.py"),
               os.path.join(BASE_DIR, "app"),
               os.path.join(BASE_DIR, "web")]
    for t in targets:
        if os.path.isfile(t):
            try:
                mt = os.path.getmtime(t)
                newest = mt if newest is None else max(newest, mt)
            except OSError:
                pass
        elif os.path.isdir(t):
            for root, dirs, files in os.walk(t):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in files:
                    if not fn.endswith(_CODE_EXTS):
                        continue
                    try:
                        mt = os.path.getmtime(os.path.join(root, fn))
                    except OSError:
                        continue
                    newest = mt if newest is None else max(newest, mt)
    return newest


def info() -> dict:
    """返回版本信息字典（静态部分只算一次并缓存）。"""
    if not _cache:
        try:
            from . import __version__ as app_version
        except Exception:  # noqa: BLE001
            app_version = "unknown"
        full, short, ref_mt = _read_git_commit()
        cm = code_mtime()
        _cache.update({
            "version": app_version,
            "commit": full,
            "commit_short": short or None,
            "commit_time": _fmt(ref_mt),
            "code_mtime": _fmt(cm),
            "base_dir": BASE_DIR,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        })
    out = dict(_cache)
    out["started_at"] = _START_STR
    out["uptime_sec"] = int(time.time() - _START_TS)
    return out


def short_label() -> str:
    """给日志用的一行标签，如 "v1.0.0 @9f66016"。"""
    i = info()
    commit = i.get("commit_short") or "nogit"
    return f"v{i.get('version')} @{commit}"
