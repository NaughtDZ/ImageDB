# -*- coding: utf-8 -*-
"""
下载与依赖更新模块
==================
职责：
    1. 使用用户配置的代理下载模型（HuggingFace 仓库或直链）；
    2. 测试代理连通性；
    3. 通过 pip 更新/安装依赖（应用代理）。

说明：
    - 后台任务进度保存在内存字典中（重启后丢失，仅用于本次会话的进度展示）；
    - 模型的最终落盘目录为 data/models/<工具名>/。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid

import requests

from .tagging.base import build_proxies

logger = logging.getLogger("imagedb.downloader")

# 简易后台任务存储（内存）：id -> {status, progress, message, lines[]}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# 常见模型文件后缀（用于从仓库文件列表中筛选需要下载的文件）
# 同时兼容两种布局：
#   - cl_tagger 风格：cl_tagger.onnx + cl_tagger_tag_mapping.json
#   - 通用/wd14 风格：model.onnx + tags.txt / selected_tags.csv
# 注意：不再逐个 HEAD 猜测文件名，而是通过 HF API 获取仓库真实文件列表，
#       按后缀筛选（可容纳 cl_tagger_1_02.onnx 这类任意命名）。
MODEL_FILE_SUFFIXES = (".onnx", "_tag_mapping.json", "tags.txt", "selected_tags.csv", "tags.json")


def parse_hf_url(url: str) -> tuple[str, str | None]:
    """
    解析 HuggingFace 页面链接，返回 (repo_id, 指定文件名或 None)。

    支持多种输入：
      - 裸 repo_id：         cella110n/cl_tagger
      - 模型主页：           https://huggingface.co/cella110n/cl_tagger
      - 文件树页：           https://huggingface.co/cella110n/cl_tagger/tree/main
      - 具体文件页：         https://huggingface.co/cella110n/cl_tagger/tree/main/cl_tagger_1_02
      - 直接文件链接：       https://huggingface.co/cella110n/cl_tagger/resolve/main/cl_tagger_1_02.onnx
    """
    url = (url or "").strip()
    if not url:
        return "", None
    # 去掉协议与域名前缀（同时兼容 huggingface.co 与 hf-mirror.com）
    if "://" in url:
        url = url.split("://", 1)[1]
    parts = [p for p in url.split("/") if p]
    # 去掉域名段（第一个段是 host，如 huggingface.co）
    if parts and ("." in parts[0] or parts[0] == "hf-mirror"):
        parts = parts[1:]
    if len(parts) < 2:
        return "", None
    repo_id = f"{parts[0]}/{parts[1]}"
    # 剩余路径：/tree/main/文件名 或 /resolve/main/文件名
    rest = parts[2:]
    filename = None
    if rest:
        # 去掉 /tree|/resolve 与分支名（main/master/commit hash）
        if rest[0] in ("tree", "resolve", "blob"):
            rest = rest[1:]
        if rest and rest[0] in ("main", "master", "main/"):
            rest = rest[1:]
        if rest:
            filename = "/".join(rest)
    return repo_id, filename


def get_repo_file_list(session: requests.Session, repo_id: str) -> list[str]:
    """
    通过 HF API 获取仓库全部文件路径列表。
    失败时回退到空列表（由调用方决定后续处理）。
    """
    api_url = f"https://huggingface.co/api/models/{repo_id.strip('/')}"
    try:
        resp = session.get(api_url, timeout=30)
        if resp.status_code != 200:
            logger.warning("获取仓库文件列表失败 %s：HTTP %s", repo_id, resp.status_code)
            return []
        data = resp.json()
        return [s.get("rfilename", "") for s in data.get("siblings", []) if s.get("rfilename")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取仓库文件列表异常 %s：%s", repo_id, exc)
        return []


def make_session(proxy_cfg: dict | None, token: str | None = None) -> requests.Session:
    """构造带代理（及可选 HuggingFace 令牌）的 requests 会话。"""
    s = requests.Session()
    proxies = build_proxies(proxy_cfg)
    if proxies:
        s.proxies.update(proxies)
    s.headers.update({"User-Agent": "ImageDB/1.0"})
    if token:
        # HuggingFace 使用 Bearer 认证；自动补全 Bearer 前缀
        if not token.lower().startswith("bearer "):
            token = "Bearer " + token
        s.headers.update({"Authorization": token})
    return s


def test_proxy(proxy_cfg: dict | None, timeout: int = 10) -> dict:
    """测试代理是否可用：尝试访问 huggingface.co。"""
    proxies = build_proxies(proxy_cfg)
    try:
        start = time.time()
        resp = requests.get(
            "https://huggingface.co",
            proxies=proxies or None,
            timeout=timeout,
            headers={"User-Agent": "ImageDB/1.0"},
        )
        latency_ms = round((time.time() - start) * 1000)
        return {"ok": resp.status_code == 200, "status": resp.status_code, "latency_ms": latency_ms}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _new_job(label: str) -> str:
    """创建后台任务记录，返回任务 id。"""
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {
            "id": jid,
            "label": label,
            "status": "running",
            "progress": 0,
            "message": "准备中……",
            "lines": [],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return jid


def _update_job(jid: str, **kwargs) -> None:
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kwargs)


def list_jobs() -> list[dict]:
    with JOBS_LOCK:
        return [dict(v) for v in JOBS.values()]


def download_model(repo_input: str, dest_dir: str, proxy_cfg: dict | None,
                   token: str | None = None) -> str:
    """
    从 HuggingFace 下载模型文件到 dest_dir（后台执行）。
    repo_input 支持：裸 repo_id / 仓库主页链接 / 文件树链接 / 具体文件链接。
    token 用于下载受限（gated）模型，如 cella110n/cl_tagger_v2。
    返回任务 id，前端轮询 /api/downloads 查看进度。
    """
    # 解析用户输入：可能是 URL，也可能是裸 repo_id
    repo_id, only_file = parse_hf_url(repo_input)
    if not repo_id:
        jid = _new_job(f"下载模型 {repo_input}")
        _update_job(jid, status="failed",
                    message=f"无法识别的 HuggingFace 地址：{repo_input}。请粘贴仓库主页链接或 repo_id")
        return jid

    jid = _new_job(f"下载模型 {repo_id}" + (f"（{only_file}）" if only_file else ""))
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    def worker() -> None:
        session = make_session(proxy_cfg, token=token)
        _update_job(jid, message="正在查询仓库文件列表……")

        # 方式一：通过 HF API 获取仓库真实文件列表（最可靠）
        all_files = get_repo_file_list(session, repo_id)
        if only_file:
            # 用户指定了具体文件：从列表中匹配（支持精确匹配、无扩展名模糊匹配）
            candidates = [f for f in all_files
                          if f == only_file or f.endswith(only_file)
                          or f.startswith(only_file + ".")]
            files = candidates[:1] or ([only_file] if only_file else [])
        else:
            # 按模型相关后缀筛选
            files = [f for f in all_files
                     if f.lower().endswith(MODEL_FILE_SUFFIXES)
                     and not f.endswith((".gitattributes", "README.md"))]
            # 优先 .onnx 文件排在前面
            files.sort(key=lambda f: (0, f) if f.lower().endswith(".onnx") else (1, f))

        # 方式二（API 失败时回退）：直接尝试下载常见候选名（含用户指定的文件）
        if not files and not all_files:
            fallback = ["cl_tagger.onnx", "cl_tagger_tag_mapping.json", "model.onnx",
                        "model_fp16.onnx", "tags.txt", "selected_tags.csv"]
            if only_file:
                fallback = [only_file]
            files = fallback

        if not files:
            _update_job(jid, status="failed",
                        message=f"仓库中未找到模型文件（{repo_id}）。"
                                f"请确认仓库地址正确、文件确为模型（.onnx），"
                                f"受限模型还需在设置页配置 HF Token")
            return

        total = len(files)
        for i, cand in enumerate(files, 1):
            # 下载路径：优先 resolve/main，若用户指定了非 main 分支则用完整路径
            url = f"https://huggingface.co/{repo_id.strip('/')}/resolve/main/{cand}"
            # 本地保存：去掉子目录，取文件名（避免路径注入）
            out_name = os.path.basename(cand.rstrip("/")) or "model.onnx"
            out_path = os.path.join(dest_dir, out_name)
            _update_job(jid, message=f"下载 {out_name}（{i}/{total}）……",
                        progress=round((i - 1) / total * 100))
            try:
                with session.get(url, stream=True, timeout=60) as resp:
                    if resp.status_code in (401, 403):
                        _update_job(jid, status="failed",
                                    message=f"下载 {out_name} 被拒绝（HTTP {resp.status_code}）："
                                            f"该仓库为受限模型，请在设置页配置已授权的 HF Token")
                        return
                    resp.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            if chunk:
                                f.write(chunk)
                logger.info("下载完成：%s", out_path)
            except Exception as exc:  # noqa: BLE001
                _update_job(jid, status="failed", message=f"下载 {out_name} 失败：{exc}")
                return
        _update_job(jid, status="done", progress=100,
                    message=f"下载完成，共 {total} 个文件 → {dest_dir}")

    threading.Thread(target=worker, daemon=True, name="model-download").start()
    return jid


def install_directml(proxy_cfg: dict | None) -> str:
    """
    安装 onnxruntime-directml（GPU 加速）。
    注意：onnxruntime 与 onnxruntime-directml 冲突，需先卸载普通版。
    后台执行，进度与日志记录到任务列表。
    """
    jid = _new_job("安装 onnxruntime-directml")

    def worker() -> None:
        env = dict(os.environ)
        proxies = build_proxies(proxy_cfg)
        if proxies:
            env["HTTP_PROXY"] = proxies.get("http", "")
            env["HTTPS_PROXY"] = proxies.get("https", "")
        cmds = [
            # 1) 卸载普通版 onnxruntime（若存在）
            [sys.executable, "-m", "pip", "uninstall", "-y", "onnxruntime"],
            # 2) 安装 DirectML 版
            [sys.executable, "-m", "pip", "install", "--upgrade", "onnxruntime-directml"],
        ]
        lines: list[str] = []
        for i, cmd in enumerate(cmds, 1):
            _update_job(jid, message=f"步骤 {i}/{len(cmds)}：{'卸载普通版' if 'uninstall' in cmd else '安装 DirectML 版'}……")
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    lines.append(line)
                    if len(lines) > 300:
                        lines.pop(0)
                    _update_job(jid, lines=list(lines))
                proc.wait()
                if proc.returncode != 0:
                    _update_job(jid, status="failed",
                                message=f"步骤 {i} 失败（pip 退出码 {proc.returncode}）")
                    return
            except Exception as exc:  # noqa: BLE001
                _update_job(jid, status="failed", message=f"步骤 {i} 异常：{exc}")
                return
        _update_job(jid, status="done", message="onnxruntime-directml 安装完成，请重启程序")
        # 顺带验证
        try:
            import onnxruntime as ort
            _update_job(jid, message="安装完成，可用 provider：" + str(ort.get_available_providers()))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=worker, daemon=True, name="install-directml").start()
    return jid


def update_deps(packages: str, proxy_cfg: dict | None) -> str:
    """用 pip 安装/更新依赖（应用代理），后台执行并记录输出。"""
    jid = _new_job("更新依赖")
    pkg_list = [p for p in packages.replace(",", " ").split() if p]
    if not pkg_list:
        _update_job(jid, status="failed", message="未指定要更新的包")
        return jid

    def worker() -> None:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + pkg_list
        env = dict(os.environ)
        proxies = build_proxies(proxy_cfg)
        if proxies:
            env["HTTP_PROXY"] = proxies.get("http", "")
            env["HTTPS_PROXY"] = proxies.get("https", "")
        _update_job(jid, message="运行 pip ……")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            lines: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                lines.append(line)
                if len(lines) > 200:   # 只保留最近 200 行
                    lines.pop(0)
                _update_job(jid, lines=list(lines))
            proc.wait()
            if proc.returncode == 0:
                _update_job(jid, status="done", message="依赖更新完成")
            else:
                _update_job(jid, status="failed", message=f"pip 退出码 {proc.returncode}")
        except Exception as exc:  # noqa: BLE001
            _update_job(jid, status="failed", message=f"更新失败：{exc}")

    threading.Thread(target=worker, daemon=True, name="pip-update").start()
    return jid
