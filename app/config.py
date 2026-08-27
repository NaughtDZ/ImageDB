# -*- coding: utf-8 -*-
"""
配置管理模块
============
所有应用设置（代理、打标参数、视频参数、校验间隔等）统一存放在
SQLite 数据库的 settings 表中（键值对），避免散落配置文件。

设计说明：
    - 每个配置项都有默认值，首次启动时自动写入数据库；
    - GUI 设置页修改后通过 PUT /api/settings 写回数据库；
    - 打标工具的配置以 JSON 字符串存于 tool_<工具名> 键中。
"""
from __future__ import annotations

import json
import logging

from .database import execute, query_all, query_one

logger = logging.getLogger("imagedb.config")

# 布尔字符串解析辅助
_TRUE_VALUES = {"1", "true", "yes", "on"}

# 配置默认值（键 → 默认值字符串）
DEFAULTS: dict[str, str] = {
    "port": "8000",                     # HTTP 服务端口
    "verify_interval_sec": "60",        # 后台自动校验（清理缺失记录）间隔，单位秒；0 表示关闭
    "proxy_enabled": "false",           # 是否启用代理
    "proxy_type": "http",               # 代理类型：http / socks5（socks 需要额外依赖）
    "proxy_host": "",                   # 代理主机
    "proxy_port": "",                   # 代理端口
    "proxy_username": "",               # 代理用户名（可选）
    "proxy_password": "",               # 代理密码（可选）
    "hf_token": "",                     # HuggingFace 访问令牌（下载受限/gated 模型时需要）
    "video_frame_interval_sec": "5.0",  # 视频打标抽帧间隔（秒）
    "video_max_frames": "20",           # 视频打标最多抽帧数（0 = 不限制）
    "video_thumb_frame_sec": "1.0",     # 视频缩略图取帧位置（秒）
    "thumb_size": "320",                # 缩略图最长边像素
    "thumb_cache_limit_mb": "200",      # 缩略图磁盘缓存上限（MB），超限自动清理最旧的（LRU）
    "tagging_parallel": "4",            # 并行打标量：每次批量推理的图片数（GPU 并行度）
    # ---- 打标工具参数（JSON 字符串，每个工具一个配置块）----
    "tool_cl_tagger": json.dumps({
        "model_dir": "",        # 模型目录（包含 model.onnx 与 tags.txt / selected_tags.csv）
        "input_size": 224,      # 模型输入尺寸
        "threshold": 0.5,       # 置信度阈值
        "use_directml": True,   # 优先使用 DirectML 加速
    }, ensure_ascii=False),
    "tool_wd14": json.dumps({
        "model_dir": "",        # 模型目录（包含 model.onnx 与 selected_tags.csv）
        "input_size": 448,      # WD14 输入尺寸 448x448
        "threshold": 0.35,      # 置信度阈值
        "use_directml": True,
        "include_rating": False,  # 是否包含画质分级标签（wd14 的 category 9）
    }, ensure_ascii=False),
    "tool_llm": json.dumps({
        "base_url": "",         # OpenAI 兼容接口地址，如 https://api.openai.com/v1
        "api_key": "",          # API 密钥
        "model": "",            # 模型名，如 gpt-4o-mini
        "prompt": "请用英文为这张图片生成 5-15 个标签，用逗号分隔，只输出标签列表，不要其他文字。",
        "timeout": 60,          # 请求超时（秒）
    }, ensure_ascii=False),
}


class AppConfig:
    """应用配置：读写 settings 表，未设置时回退默认值。"""

    def __init__(self) -> None:
        # 首次运行时把默认值全部写入数据库（幂等）
        for key, value in DEFAULTS.items():
            row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
            if row is None:
                execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self._cache: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """从数据库重新加载全部设置到内存缓存。"""
        self._cache = {}
        for row in query_all("SELECT key, value FROM settings"):
            self._cache[row["key"]] = row["value"]

    # ---- 基础读取 ----
    def get(self, key: str, default: str | None = None) -> str | None:
        """读取字符串配置项。"""
        return self._cache.get(key, default)

    def get_int(self, key: str, default: int = 0) -> int:
        """读取整数配置项。"""
        try:
            return int(float(self._cache.get(key, default)))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """读取浮点配置项。"""
        try:
            return float(self._cache.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """读取布尔配置项。"""
        val = self._cache.get(key)
        if val is None:
            return default
        return str(val).strip().lower() in _TRUE_VALUES

    # ---- 写入 ----
    def set(self, key: str, value: object) -> None:
        """写入单个配置项并刷新缓存。"""
        execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))
        self.reload()

    # ---- 便捷方法 ----
    def to_dict(self) -> dict[str, str]:
        """返回全部配置的浅拷贝（字符串值）。"""
        return dict(self._cache)

    def proxy_dict(self) -> dict:
        """构造代理配置字典（供 requests 等使用）。"""
        return {
            "enabled": self.get_bool("proxy_enabled", False),
            "type": self.get("proxy_type", "http"),
            "host": self.get("proxy_host", ""),
            "port": self.get("proxy_port", ""),
            "username": self.get("proxy_username", ""),
            "password": self.get("proxy_password", ""),
        }

    def tool_config(self, tool_name: str) -> dict:
        """读取某个打标工具的配置（JSON 解析为字典）。"""
        raw = self.get(f"tool_{tool_name}", "{}") or "{}"
        try:
            cfg = json.loads(raw)
            return cfg if isinstance(cfg, dict) else {}
        except (ValueError, TypeError):
            logger.warning("工具 %s 的配置不是合法 JSON：%s", tool_name, raw)
            return {}
