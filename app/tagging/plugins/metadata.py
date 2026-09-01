# -*- coding: utf-8 -*-
r"""
元数据 / 正则打标插件（实验性）
=============================
从文件名或附加元数据（EXIF/IPTC/XMP）中用「正则规则」提取标签。

规则配置（settings 表的 tool_metadata 键，JSON）：
    { "rules": [ { enabled, name, source, pattern, flags, tag, normalize } ] }
      - enabled   : 是否启用该规则
      - name      : 规则名（可用于 {name} 占位符）
      - source    : filename | metadata | all
      - pattern   : 正则表达式
      - flags     : 可选 i / m / s（忽略大小写 / 多行 / 点匹配换行）
      - tag       : 标签模板，支持 {match}（整体匹配）、$1..$9（捕获组）、{name}
      - normalize : 可选 lower / upper
      - split     : 可选分隔符（正则）。命中后把渲染出的标签按它拆成多个，用于「文件名里 _ 分隔的多个 tag」

用途示例：
    - 从文件名提取：      pattern="^(.*?)[_-](\d+)$",  tag="$1"
    - 从元数据关键词打标： source="metadata", pattern="(vtuber)", tag="$1"

说明：实验版主要针对图片；视频抽帧不做本规则（返回空，不额外消耗）。
"""
from __future__ import annotations

import logging
import os
import re

from ..base import TaggerPlugin, TagResult

logger = logging.getLogger("imagedb.tagging.metadata")


def _flags_from(flags: str) -> int:
    f = 0
    for ch in (flags or ""):
        if ch == "i":
            f |= re.I
        elif ch == "m":
            f |= re.M
        elif ch == "s":
            f |= re.S
    return f


def _render(template: str, m, rule_name: str) -> str:
    """替换标签模板：{match} / {name} / $0..$9。"""
    s = template or "{match}"
    s = s.replace("{match}", m.group(0) or "")
    s = s.replace("{name}", rule_name or "")
    for i in range(9, -1, -1):
        try:
            g = m.group(i) or ""
        except IndexError:
            g = ""
        s = s.replace("$%d" % i, g)
    return s


class MetadataTaggerPlugin(TaggerPlugin):
    """从文件名或元数据(EXIF/IPTC/XMP)按正则规则提取标签。"""

    name = "metadata"
    display_name = "元数据/正则打标（实验）"
    description = "从文件名或附加元数据(EXIF/IPTC/XMP)按正则规则提取标签（实验性）。"

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.config.setdefault("rules", [])

    def load(self) -> bool:
        # 无需加载模型
        self._loaded = True
        return True

    def get_config(self) -> dict:
        cfg = dict(self.config)
        cfg.setdefault("rules", [])
        cfg["hint"] = ("规则存于本工具配置的 rules 里：source=filename/metadata/all，"
                       "pattern=正则，tag 模板可用 {match}、$1、{name}；split=可选分隔符，把结果拆成多个标签。")
        return cfg

    # ---- 核心打标 ----
    def tag_image(self, image_path: str) -> list[TagResult]:
        compiled = []
        for r in (self.config.get("rules") or []):
            pattern = (r.get("pattern") or "").strip()
            if not pattern:
                continue
            if "enabled" in r and not r["enabled"]:
                continue
            try:
                compiled.append({
                    "name": r.get("name", ""),
                    "source": (r.get("source") or "filename").lower(),
                    "re": re.compile(pattern, _flags_from(r.get("flags", ""))),
                    "tag": r.get("tag", "{match}"),
                    "normalize": (r.get("normalize") or "").lower(),
                    "split": (r.get("split") or ""),   # 可选：命中后把标签按该分隔符拆成多个
                })
            except re.error as exc:
                logger.warning("规则「%s」正则错误：%s", r.get("name", ""), exc)

        if not compiled:
            return []

        filename = os.path.basename(image_path)
        stem = os.path.splitext(filename)[0]
        file_cands = [filename, stem]
        # 仅当存在 metadata/all 规则时才读取附加元数据（文件名规则无需打开图片）
        need_meta = any(c["source"] in ("metadata", "all") for c in compiled)
        meta_cands = self._meta_values(image_path) if need_meta else []

        found: dict[str, float] = {}
        for c in compiled:
            cands = []
            if c["source"] in ("filename", "all"):
                cands.extend(file_cands)
            if c["source"] in ("metadata", "all"):
                cands.extend(meta_cands)
            for text in cands:
                if not text:
                    continue
                m = c["re"].search(text)
                if not m:
                    continue
                tag = _render(c["tag"], m, c["name"])
                if c["normalize"] == "lower":
                    tag = tag.lower()
                elif c["normalize"] == "upper":
                    tag = tag.upper()
                tag = (tag or "").strip()
                if tag:
                    sep = c.get("split") or ""
                    if sep:
                        # split 为分隔符（正则）：把渲染结果拆成多个标签，如 "[tag] 后 _ 分隔的多个 tag"
                        for sub in re.split(sep, tag):
                            sub = (sub or "").strip()
                            if sub:
                                found.setdefault(sub, 1.0)
                    else:
                        found.setdefault(tag, 1.0)   # 规则标签置信度 1.0
                break   # 每条规则对一个候选命中取一条即可
        return [TagResult(tag=t, confidence=c) for t, c in found.items()]

    # ---- 元数据候选值 ----
    def _meta_values(self, path: str) -> list[str]:
        try:
            from ...metadata import extract_metadata
            mm = extract_metadata(path, "image")
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取元数据失败 %s：%s", path, exc)
            return []
        out: list[str] = []
        for group in ("basic", "exif", "iptc", "xmp"):
            for v in (mm.get(group) or {}).values():
                if isinstance(v, (list, tuple)):
                    out.extend(str(x) for x in v if str(x).strip())
                elif isinstance(v, dict):
                    continue
                elif str(v).strip():
                    out.append(str(v))
        return out

    def tag_video_frames(self, frame_paths: list[str]):
        """实验版：视频抽帧不做元数据/文件名规则（返回空，不额外消耗）。"""
        logger.debug("[metadata] 视频打标暂不适用（实验版仅支持图片），跳过 %d 帧", len(frame_paths))
        return []


# 供插件管理器发现
PLUGIN_CLASS = MetadataTaggerPlugin
