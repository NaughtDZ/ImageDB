# -*- coding: utf-8 -*-
"""
打标子系统
==========
插件化设计：所有打标工具都实现 TaggerPlugin 基类，
程序本体只依赖基类接口，与具体工具解耦。

新增打标工具的方式：
    1. 在 app/tagging/plugins/ 下新建 .py 文件；
    2. 继承 TaggerPlugin（或 OnnxTaggerPlugin）并实现 tag_image()；
    3. 在模块底部导出 PLUGIN_CLASS = YourPluginClass；
    4. 重启程序即可在打标对话框与设置页中看到新工具。

移除打标工具的方式：
    删除插件文件，或在设置页中把模型目录置空即可。
"""
from .base import TaggerPlugin, TagResult, OnnxTaggerPlugin, build_proxies, parse_tags_text
from .manager import PluginManager, init_manager, get_manager

__all__ = [
    "TaggerPlugin", "TagResult", "OnnxTaggerPlugin",
    "build_proxies", "parse_tags_text",
    "PluginManager", "init_manager", "get_manager",
]
