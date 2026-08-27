# -*- coding: utf-8 -*-
"""
自定义 LLM 打标插件
===================
通过任意 OpenAI 兼容的视觉大模型接口打标（支持代理）。

配置项（设置页可编辑）：
    base_url  : 接口地址，如 https://api.openai.com/v1 或本地 vLLM 地址
    api_key   : 密钥
    model     : 模型名（需支持视觉输入），如 gpt-4o-mini
    prompt    : 提示词
    timeout   : 请求超时（秒）

依赖：requests（核心依赖已包含）。
"""
from __future__ import annotations

import base64
import logging
import os

import requests

from ..base import TaggerPlugin, TagResult, build_proxies, parse_tags_text

logger = logging.getLogger("imagedb.tagging.llm")

# 图片过大时压缩（LLM 接口对图片大小有限制），单位像素
_MAX_IMAGE_EDGE = 1536


class LLMTaggerPlugin(TaggerPlugin):
    """自定义 LLM 打标器（OpenAI 兼容视觉接口）。"""

    name = "llm"
    display_name = "自定义 LLM"
    description = "调用 OpenAI 兼容的视觉模型接口打标（支持代理）。"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.config.setdefault("base_url", "")
        self.config.setdefault("api_key", "")
        self.config.setdefault("model", "")
        self.config.setdefault(
            "prompt",
            "请用英文为这张图片生成 5-15 个标签，用逗号分隔，只输出标签列表，不要其他文字。",
        )
        self.config.setdefault("timeout", 60)

    def load(self) -> bool:
        # LLM 插件无需加载本地模型
        self._loaded = True
        return True

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        proxies = build_proxies(self._proxy)
        if proxies:
            s.proxies.update(proxies)
        s.headers.update({"User-Agent": "ImageDB/1.0"})
        return s

    def _encode_image(self, image_path: str) -> str:
        """读取图片并转 base64（必要时先压缩）。"""
        with open(image_path, "rb") as f:
            data = f.read()
        # 尝试用 Pillow 压缩超大图片
        try:
            from PIL import Image
            import io
            with Image.open(image_path) as im:
                if max(im.size) > _MAX_IMAGE_EDGE:
                    im.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=90)
                    data = buf.getvalue()
        except Exception:  # noqa: BLE001 - 压缩失败则用原图
            pass
        return base64.b64encode(data).decode("ascii")

    def tag_image(self, image_path: str) -> list[TagResult]:
        base_url = (self.config.get("base_url") or "").strip().rstrip("/")
        api_key = (self.config.get("api_key") or "").strip()
        model = (self.config.get("model") or "").strip()
        if not base_url or not model:
            raise RuntimeError("LLM 打标器未配置 base_url / model，请在设置页配置")
        if not os.path.isfile(image_path):
            raise RuntimeError(f"图片不存在：{image_path}")

        b64 = self._encode_image(image_path)
        # 由文件扩展名推断 MIME
        ext = os.path.splitext(image_path)[1].lower()
        mime = {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/jpeg")

        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.config.get("prompt", "")},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "max_tokens": 512,
            "temperature": 0.3,
        }
        try:
            resp = self._make_session().post(
                url, json=payload, headers=headers,
                timeout=float(self.config.get("timeout", 60)),
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"LLM 接口调用失败：{exc}") from exc

        results = parse_tags_text(text)
        logger.info("LLM 打标 %s：得到 %d 个标签", image_path, len(results))
        return results

    def get_config(self) -> dict:
        cfg = dict(self.config)
        cfg["base_url_hint"] = "OpenAI 兼容接口地址，如 https://api.openai.com/v1"
        return cfg


# 供插件管理器发现
PLUGIN_CLASS = LLMTaggerPlugin
