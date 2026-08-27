# -*- coding: utf-8 -*-
"""
ImageDB 应用包
===============
包含以下模块（模块之间尽量解耦，便于独立维护与扩展）：
    config     配置管理（代理、打标参数、视频参数等，存于 SQLite settings 表）
    database   SQLite 数据库层（唯一直接访问数据库的地方）
    library    目录库管理（导入目录、扫描、缺失校验、目录树）
    media      媒体处理（缩略图、视频抽帧、时长探测）
    tagging    打标子系统（插件基类 + 插件管理器 + 内置插件）
    downloader 模型下载 / 代理测试 / 依赖更新
    server     FastAPI HTTP 服务层（REST API + 前端静态资源）
"""
__version__ = "1.0.0"
