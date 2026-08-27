# -*- coding: utf-8 -*-
"""
cl-tagger 打标插件
==================
适配 cella110n 的 CL Tagger 系列模型：
    https://huggingface.co/cella110n/cl_tagger      （v1，公开可下载）
    https://huggingface.co/cella110n/cl_tagger_v2   （v2，需同意共享联系信息后下载）

模型规格（参考 ComfyUI-Mira 的 Tagger.py 社区标准实现）：
    - 输入：448x448，预处理 = pad 正方形(白色) → resize → RGB→BGR
      → 归一化 (x/255 - 0.5)/0.5（注意：不是 CLIP 的 mean/std！）
    - 模型文件：cl_tagger.onnx（或 model.onnx）
    - 标签映射：cl_tagger_tag_mapping.json，格式：
        {"idx_to_tag": {"0": "tag1", ...}, "tag_to_category": {"tag1": "General", ...}}
      类别：Rating / General / Artist / Character / Copyright / Meta / Quality / Model
    - 输出：General / Character 类标签按阈值过滤，Rating 只取最高分一项

模型准备（设置页 → 模型下载，或手动放置）：
    1. 在 HuggingFace 页面下载 cl_tagger.onnx 与 cl_tagger_tag_mapping.json；
    2. 放入同一个目录（如 data/models/cl_tagger/）；
    3. 设置页把 cl_tagger 的 model_dir 指向该目录。

运行库：
    pip install onnxruntime-directml   （Windows 显卡，DirectML 加速，推荐）
    pip install onnxruntime            （无显卡时 CPU 回退）
"""
from __future__ import annotations

from ..base import OnnxTaggerPlugin


class CLTaggerPlugin(OnnxTaggerPlugin):
    """cl-tagger：适配 cella110n/cl_tagger 系列模型的 ONNX 打标器。"""

    name = "cl_tagger"
    display_name = "cl-tagger（cella110n CLIP 打标）"
    description = ("适配 cella110n/cl_tagger 与 cl_tagger_v2 模型"
                   "（448x448，pad 正方形 + BGR 预处理，JSON 标签映射，支持 DirectML）。")

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        # cl-tagger 是固定规格的模型：强制使用正确参数，避免用户配置里的错误值
        # （此前曾发生 input_size 被误设为 224 导致推理失败，这里直接覆盖）
        self.config["preprocess_mode"] = "cl_tagger"   # 强制 cl_tagger 预处理
        self.config["input_size"] = 448                # 强制 448x448 输入
        self.config.setdefault("threshold", 0.35)      # General/Character 阈值
        self.config["use_directml"] = True
        self.config.setdefault("include_rating", False)  # 是否输出 Rating 标签

    def get_config(self) -> dict:
        cfg = super().get_config()
        cfg["model_dir_hint"] = ("包含 cl_tagger.onnx 与 cl_tagger_tag_mapping.json 的目录"
                                 "（HuggingFace：cella110n/cl_tagger 或 cella110n/cl_tagger_v2，"
                                 "v2 需先同意共享联系信息）")
        return cfg


# 供插件管理器发现：指定本模块对外暴露的插件类
PLUGIN_CLASS = CLTaggerPlugin
