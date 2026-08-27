# -*- coding: utf-8 -*-
"""
WD14 打标插件
=============
WD14 tagger 系列模型（SmilingWolf 的 wd-v1-4-vit-tagger / wd-swinv2-tagger-v3 等）。

模型准备：
    1. 下载模型（设置页 → 模型下载，仓库如 SmilingWolf/wd-swinv2-tagger-v3），
       或手动放置 model.onnx 与 selected_tags.csv 到同一目录；
    2. 设置页把 wd14 的 model_dir 指向该目录。

说明：
    - 输入尺寸固定 448x448，预处理采用中心裁剪 + CLIP 归一化；
    - 默认过滤画质分级标签（category 9），可在设置中开启 include_rating。
"""
from __future__ import annotations

from ..base import OnnxTaggerPlugin


class WD14Plugin(OnnxTaggerPlugin):
    """WD14 tagger 打标器。"""

    name = "wd14"
    display_name = "WD14 Tagger"
    description = "WD14 系列打标模型（如 wd-swinv2-tagger-v3），输入 448x448，DirectML 加速。"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        # WD14 默认参数
        self.config.setdefault("input_size", 448)
        self.config.setdefault("threshold", 0.35)
        self.config.setdefault("use_directml", True)
        self.config.setdefault("include_rating", False)

    def get_config(self) -> dict:
        cfg = super().get_config()
        cfg["model_dir_hint"] = "包含 model.onnx 与 selected_tags.csv 的目录（HuggingFace 仓库如 SmilingWolf/wd-swinv2-tagger-v3）"
        return cfg


# 供插件管理器发现
PLUGIN_CLASS = WD14Plugin
