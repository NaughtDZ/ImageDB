# -*- coding: utf-8 -*-
"""
内置打标插件
============
本目录下的每个 .py 文件都是一个独立的打标工具插件：

    cl_tagger.py   基于 CLIP 的通用 ONNX 打标器（DirectML 加速）
    wd14.py        WD14 tagger 系列模型（如 wd-swinv2-tagger-v3）
    llm.py         自定义 LLM（OpenAI 兼容视觉接口）

新增插件：复制任意插件文件改造成新工具，或参考 base.py 的文档。
程序启动时自动扫描本目录，无需修改其他代码。
"""
