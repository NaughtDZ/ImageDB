# -*- coding: utf-8 -*-
"""
打标插件基类
============
所有打标工具（cl-tagger / wd14 / llm / 未来新增工具）都必须继承 TaggerPlugin
并实现 tag_image()。

设计要点：
    - 插件与程序本体完全解耦：程序只调用基类定义的方法；
    - 视频打标通过“抽帧 + 聚合”在基类中提供默认实现，插件无需关心；
    - OnnxTaggerPlugin 提供通用 ONNX 模型推理（WD14 / CLIP tagger 共用），
      优先使用 DirectML（onnxruntime-directml），自动回退 CPU。
"""
from __future__ import annotations

import csv
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("imagedb.tagging.base")


@dataclass
class TagResult:
    """一次打标返回的一个标签。"""
    tag: str                  # 标签名
    confidence: float = 1.0   # 置信度 0~1


# ---- 代理工具函数 ----
def build_proxies(proxy_cfg: dict | None) -> dict:
    """根据代理配置构造 requests 代理字典（空配置返回空字典）。"""
    if not proxy_cfg or not proxy_cfg.get("enabled"):
        return {}
    host = (proxy_cfg.get("host") or "").strip()
    port = str(proxy_cfg.get("port") or "").strip()
    if not host or not port:
        return {}
    scheme = proxy_cfg.get("type") or "http"
    auth = ""
    if proxy_cfg.get("username"):
        auth = f"{proxy_cfg['username']}:{proxy_cfg.get('password', '')}@"
    url = f"{scheme}://{auth}{host}:{port}"
    return {"http": url, "https": url}


def parse_tags_text(text: str) -> list[TagResult]:
    """
    解析 LLM 返回的标签文本：支持 JSON 数组、逗号/换行/顿号分隔等格式。
    返回去重后的标签列表。
    """
    text = (text or "").strip()
    if not text:
        return []
    tags: list[str] = []
    # 尝试 JSON 数组
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            tags = [str(t).strip() for t in arr if str(t).strip()]
    except (ValueError, TypeError):
        pass
    if not tags:
        # 按分隔符拆分：换行、中文逗号、顿号、英文逗号
        for part in text.replace("\n", ",").replace("\r", ",") \
                         .replace("，", ",").replace("、", ",").replace(";", ",") \
                         .split(","):
            t = part.strip().strip("[]\"' ").strip()
            # 去掉可能的前缀序号（如 "1. xxx" / "- xxx"）
            import re
            t = re.sub(r"^[\d\-\*\s]+\.?\s*", "", t)
            if t:
                tags.append(t)
    seen: set[str] = set()
    result: list[TagResult] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            result.append(TagResult(tag=t, confidence=1.0))
    return result


def _center_crop_resize(im, size: int):
    """先按短边等比缩放，再中心裁剪到 size×size（与 wd14 预处理一致）。"""
    w, h = im.size
    scale = size / min(w, h)
    nw, nh = max(size, int(round(w * scale))), max(size, int(round(h * scale)))
    im = im.resize((nw, nh), 2)  # 2 = Image.BILINEAR，避免依赖 PIL 枚举
    left = (nw - size) // 2
    top = (nh - size) // 2
    return im.crop((left, top, left + size, top + size))


class TaggerPlugin(ABC):
    """打标插件抽象基类。"""

    # ---- 插件元信息（子类覆盖）----
    name: str = "base"                # 唯一标识（存储于数据库 tag_jobs.tool）
    display_name: str = "基础打标器"   # 界面显示名
    description: str = ""             # 说明
    supports_video: bool = True       # 是否支持视频（通过抽帧间接支持）

    def __init__(self, config: dict | None = None):
        # config 来自设置表 tool_<name> 字段解析出的 dict
        self.config: dict = config or {}
        self._loaded = False
        self._error: str | None = None
        self._proxy: dict = {}
        self._providers: list[str] = []   # 实际使用的推理 provider（load 后填充）

    # ---- 生命周期 ----
    def load(self) -> bool:
        """加载模型/初始化资源。成功返回 True。子类可覆盖。"""
        self._loaded = True
        return True

    def unload(self) -> None:
        """释放资源。子类可覆盖。"""
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def error(self) -> str | None:
        return self._error

    def set_error(self, msg: str) -> None:
        self._error = msg
        logger.error("[%s] %s", self.name, msg)

    def set_proxy(self, proxy_cfg: dict) -> None:
        """由管理器在任务开始前注入代理配置。"""
        self._proxy = proxy_cfg or {}

    # ---- 核心打标接口 ----
    @abstractmethod
    def tag_image(self, image_path: str) -> list[TagResult]:
        """对单张图片打标，返回标签列表（按置信度降序）。"""
        raise NotImplementedError

    def tag_video_frames(self, frame_paths: list[str]) -> list[TagResult]:
        """
        对视频抽出的多帧图片打标并聚合（默认实现：逐帧打标 + 投票聚合）。
        子类可覆盖实现更精细的策略。
        """
        votes: dict[str, list[float]] = {}
        # 批量推理所有帧（一次 session.run 处理多帧，速度更快）
        if hasattr(self, "tag_images"):
            results_list = self.tag_images(frame_paths)
            for fp, frame_results in zip(frame_paths, results_list):
                for r in frame_results:
                    votes.setdefault(r.tag, []).append(r.confidence)
        else:
            for fp in frame_paths:
                try:
                    for r in self.tag_image(fp):
                        votes.setdefault(r.tag, []).append(r.confidence)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("帧打标失败 %s：%s", fp, exc)
        if not votes:
            return []
        # 聚合：按出现次数降序、平均置信度降序
        items = [
            (tag, sum(confs) / len(confs), len(confs))
            for tag, confs in votes.items()
        ]
        items.sort(key=lambda x: (-x[2], -x[1]))
        return [TagResult(tag=t, confidence=round(min(1.0, c), 4)) for t, c, _ in items]

    def to_dict(self) -> dict:
        """序列化为前端可用的信息。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "supports_video": self.supports_video,
            "loaded": self.is_loaded,
            "error": self.error,
            "config": self.get_config(),
        }

    def get_config(self) -> dict:
        """返回给前端展示/编辑的配置（含默认值提示）。"""
        return dict(self.config)


class OnnxTaggerPlugin(TaggerPlugin):
    """
    通用 ONNX 打标器（cl-tagger / wd14 共用）。
    模型目录结构要求：
        model.onnx            （onnx 模型文件）
        tags.txt              （每行一个标签名）或 selected_tags.csv（wd14 格式）
    """

    # CLIP 系列模型的归一化参数（wd14 与 cl-tagger 通常相同）
    CLIP_MEAN = [0.48145466, 0.4578275, 0.40821073]
    CLIP_STD = [0.26862954, 0.26130258, 0.27577711]

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.session = None
        self.tag_names: list[str] = []
        self.tag_categories: list[int] = []   # wd14 的 category 列（用于过滤分级标签）

    # ---- 模型文件查找 ----
    def find_model_files(self) -> tuple[str, str] | None:
        """
        在 model_dir 中查找 (onnx 文件, 标签文件)。
        支持两种模型目录布局：
          A) cl_tagger 风格：cl_tagger.onnx + cl_tagger_tag_mapping.json（同名前缀）
          B) 通用风格：model.onnx + tags.txt / selected_tags.csv（wd14 风格）
        """
        model_dir = (self.config.get("model_dir") or "").strip()
        if not model_dir or not os.path.isdir(model_dir):
            self.set_error("未配置模型目录（model_dir），请在设置页下载或指定模型")
            return None

        # A) cl_tagger 风格：任意 .onnx + 同名前缀的 *_tag_mapping.json
        try:
            onnx_names = [f for f in os.listdir(model_dir) if f.lower().endswith(".onnx")]
        except OSError:
            onnx_names = []
        for of in sorted(onnx_names):
            stem = os.path.splitext(of)[0]
            for cand in (f"{stem}_tag_mapping.json", f"{stem}.json",
                         "tag_mapping.json", "tags.json"):
                p = os.path.join(model_dir, cand)
                if os.path.isfile(p):
                    return os.path.join(model_dir, of), p
            # 兼容 onnx/ 子目录布局
            for sub in ("onnx", ""):
                p = os.path.join(model_dir, sub, of)
                if os.path.isfile(p):
                    for cand in (f"{stem}_tag_mapping.json", f"{stem}.json"):
                        tp = os.path.join(model_dir, sub, cand)
                        if os.path.isfile(tp):
                            return p, tp

        # B) 通用风格：model.onnx / wd14.onnx
        onnx_path = None
        for cand in ("model.onnx", "model_fp16.onnx", "wd14.onnx"):
            p = os.path.join(model_dir, cand)
            if os.path.isfile(p):
                onnx_path = p
                break
        if onnx_path is None:
            for cand in ("model.onnx", "model_fp16.onnx"):
                p = os.path.join(model_dir, "onnx", cand)
                if os.path.isfile(p):
                    onnx_path = p
                    break
        if onnx_path is None:
            self.set_error(f"模型目录中未找到 model.onnx（或 cl_tagger.onnx）：{model_dir}")
            return None
        # 标签文件（优先 JSON 映射，其次 txt / csv）
        tags_path = None
        for cand in ("tag_mapping.json", "tags.json", "model_vocabulary.json", "tags.txt", "selected_tags.csv"):
            p = os.path.join(model_dir, cand)
            if os.path.isfile(p):
                tags_path = p
                break
        if tags_path is None:
            self.set_error(f"模型目录中未找到标签文件（tag_mapping.json / tags.txt / selected_tags.csv）：{model_dir}")
            return None
        return onnx_path, tags_path

    # ---- 加载 ----
    def load(self) -> bool:
        try:
            import numpy as np  # noqa: F401 - 检查依赖
            import onnxruntime as ort
        except ImportError as exc:
            self.set_error(
                f"缺少运行库：{exc}。请执行 pip install onnxruntime-directml（Windows 显卡，DirectML 加速）"
                f"或 pip install onnxruntime（CPU）"
            )
            return False

        found = self.find_model_files()
        if not found:
            return False
        onnx_path, tags_path = found

        # 读取标签文件（JSON 映射 / 词汇表 / CSV / 纯文本）
        if "model_vocabulary" in tags_path:
            # v2 模型：用 model_vocabulary.json 加载标签
            if not self._load_vocabulary_json(tags_path):
                return False
        elif tags_path.endswith(".json"):
            # v1 / cl_tagger：JSON 映射（idx_to_tag + tag_to_category）
            if not self._load_tag_mapping_json(tags_path):
                return False
        elif tags_path.endswith(".csv"):
            self._load_tags_csv(tags_path)
        else:
            try:
                with open(tags_path, "r", encoding="utf-8") as f:
                    self.tag_names = [line.strip() for line in f if line.strip()]
            except OSError as exc:
                self.set_error(f"读取标签文件失败：{exc}")
                return False
        if not self.tag_names:
            self.set_error("标签文件为空")
            return False

        # 构建 providers：优先 DirectML，回退 CPU
        providers: list[str] = []
        if self.config.get("use_directml", True):
            try:
                if "DmlExecutionProvider" in ort.get_available_providers():
                    providers.append("DmlExecutionProvider")
                    logger.info("[%s] 使用 DirectML 加速", self.name)
            except Exception:  # noqa: BLE001
                pass
        providers.append("CPUExecutionProvider")
        try:
            self.session = ort.InferenceSession(onnx_path, providers=providers)
        except Exception as exc:  # noqa: BLE001
            self.set_error(f"加载 ONNX 模型失败：{exc}")
            return False
        # 输入尺寸自适应：读模型声明的真实 shape，覆盖配置的 input_size
        # （cl_tagger v2 等模型输入尺寸可能与 v1 不同，如 384/448，必须按模型实际尺寸预处理）
        try:
            inp_shape = self.session.get_inputs()[0].shape
            if (len(inp_shape) == 4 and isinstance(inp_shape[2], int)
                    and isinstance(inp_shape[3], int) and inp_shape[2] == inp_shape[3]):
                self.config["input_size"] = inp_shape[2]
                logger.info("[%s] 模型输入尺寸自适应: %s（覆盖为 %d）",
                            self.name, inp_shape, inp_shape[2])
            else:
                logger.info("[%s] 模型输入声明: %s（非静态方形，跳过自适应）",
                            self.name, inp_shape)
        except Exception:  # noqa: BLE001
            pass
        self._loaded = True
        self._error = None
        # 记录实际使用的 provider（供管理器决定并行策略）
        try:
            self._providers = list(self.session.get_providers())
        except Exception:  # noqa: BLE001
            self._providers = []
        logger.info("[%s] 模型加载完成：%s，providers=%s", self.name, onnx_path, self._providers)
        return True

    def unload(self) -> None:
        self.session = None
        super().unload()

    def _load_vocabulary_json(self, path: str) -> bool:
        """解析 v2 的 model_vocabulary.json（标签词汇表）。
        可能是 {"0": "tag", ...}、{"idx_to_tag": {...}} 或 {"tags": [...]} 等格式。
        """
        import json
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            self.set_error(f"读取 v2 词汇表失败：{exc}")
            return False
        if isinstance(data, dict):
            if "idx_to_tag" in data:
                data = data["idx_to_tag"]
            elif "tags" in data and isinstance(data["tags"], list):
                data = {str(i): t for i, t in enumerate(data["tags"])}
        elif isinstance(data, list):
            data = {str(i): t for i, t in enumerate(data)}
        else:
            self.set_error("v2 词汇表格式不支持")
            return False
        self.tag_names = [str(v) for v in data.values()] if isinstance(data, dict) else []
        # v2 词汇表通常不含分类信息；设为空列表而非全空字符串，
        # 避免触发 has_string_cats=True 走分类过滤而把所有标签丢弃。
        self.tag_categories = []
        if not self.tag_names:
            self.set_error("v2 词汇表为空")
            return False
        return True

    def _load_tag_mapping_json(self, path: str) -> bool:
        """
        解析 cl_tagger 风格的标签映射 JSON。
        支持两种格式：
          A) {"idx_to_tag": {"0": "tag1", ...}, "tag_to_category": {"tag1": "General", ...}}
          B) {"0": {"tag": "tag1", "category": "General"}, ...}
        类别：Rating / General / Artist / Character / Copyright / Meta / Quality / Model
        """
        import json
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            self.set_error(f"解析标签映射 JSON 失败：{exc}")
            return False

        idx_to_tag: dict[int, str] = {}
        tag_to_category: dict[str, str] = {}
        if isinstance(data, dict) and "idx_to_tag" in data:
            # 格式 A
            for k, v in data["idx_to_tag"].items():
                try:
                    idx_to_tag[int(k)] = str(v)
                except (ValueError, TypeError):
                    continue
            tag_to_category = {str(k): str(v) for k, v in (data.get("tag_to_category") or {}).items()}
        elif isinstance(data, dict):
            # 格式 B：dict-of-dicts
            for k, v in data.items():
                try:
                    idx = int(k)
                except (ValueError, TypeError):
                    continue
                if isinstance(v, dict):
                    tag = str(v.get("tag", ""))
                    if tag:
                        idx_to_tag[idx] = tag
                        if "category" in v:
                            tag_to_category[tag] = str(v["category"])
                else:
                    idx_to_tag[idx] = str(v)
        else:
            self.set_error("标签映射 JSON 格式不支持（应为 idx_to_tag 字典或 dict-of-dicts）")
            return False

        if not idx_to_tag:
            self.set_error("标签映射 JSON 中没有有效条目")
            return False

        # 按索引填充标签名与分类（输出向量长度 = 最大索引 + 1）
        max_idx = max(idx_to_tag.keys())
        self.tag_names = [""] * (max_idx + 1)
        self.tag_categories = [""] * (max_idx + 1)
        for idx, tag in idx_to_tag.items():
            self.tag_names[idx] = tag
            self.tag_categories[idx] = tag_to_category.get(tag, "Unknown")
        return True

    def _load_tags_csv(self, path: str) -> None:
        """解析 wd14 的 selected_tags.csv（列：name, category, count）。"""
        self.tag_names, self.tag_categories = [], []
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                # 识别列位置（兼容不同格式的表头）
                name_idx = header.index("name") if header and "name" in header else 1
                cat_idx = header.index("category") if header and "category" in header else None
                for row in reader:
                    if len(row) <= name_idx:
                        continue
                    self.tag_names.append(row[name_idx])
                    cat = -1
                    if cat_idx is not None and len(row) > cat_idx and row[cat_idx].strip().isdigit():
                        cat = int(row[cat_idx].strip())
                    self.tag_categories.append(cat)
        except (OSError, ValueError) as exc:
            logger.warning("解析标签 CSV 失败 %s：%s", path, exc)
            self.tag_names, self.tag_categories = [], []

    # ---- 预处理 ----
    def _preprocess(self, image_path: str):
        """
        读取图片并做与模型匹配的预处理，返回 NCHW float32 数组。
        两种预处理模式（config.preprocess_mode）：
          - "cl_tagger"（cella110n 的 cl_tagger 系列）：
              pad 成正方形（白色 255 填充）→ resize 到 input_size → RGB→BGR
              → 归一化 (x/255 - 0.5)/0.5
          - 其他（默认，wd14 / CLIP tagger）：
              中心裁剪等比缩放 → CLIP mean/std 归一化
        """
        import numpy as np
        from PIL import Image
        size = int(self.config.get("input_size", 224))
        mode = self.config.get("preprocess_mode", "wd14")

        if mode == "cl_tagger":
            # ---- cl_tagger 预处理（参考 ComfyUI-Mira 的 Tagger.py）----
            with Image.open(image_path) as im:
                arr = np.asarray(im.convert("RGB"), dtype=np.float32)
            h, w, _ = arr.shape
            if h != w:
                # 居中 pad 成正方形，填充白色（255）
                new_size = max(h, w)
                pad_top = (new_size - h) // 2
                pad_bottom = new_size - h - pad_top
                pad_left = (new_size - w) // 2
                pad_right = new_size - w - pad_left
                arr = np.pad(arr, ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)),
                             mode="constant", constant_values=255)
            # resize 到目标尺寸（双三次插值，与参考实现一致）
            im = Image.fromarray(arr.astype(np.uint8))
            im = im.resize((size, size), Image.BICUBIC)
            arr = np.asarray(im, dtype=np.float32)
            arr = arr[:, :, ::-1]            # RGB -> BGR
            arr = (arr / 255.0 - 0.5) / 0.5  # 归一化到 [-1, 1]
            return arr.transpose(2, 0, 1)[None, ...]  # HWC -> NCHW

        # ---- 默认：wd14 / CLIP 风格预处理 ----
        mean = np.array(self.CLIP_MEAN, dtype=np.float32)
        std = np.array(self.CLIP_STD, dtype=np.float32)
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            im = _center_crop_resize(im, size)
            arr = np.asarray(im, dtype=np.float32) / 255.0
        arr = (arr - mean) / std
        arr = arr.transpose(2, 0, 1)[None, ...]  # HWC -> NCHW
        return arr

    # ---- 打标 ----
    def tag_image(self, image_path: str) -> list[TagResult]:
        """对单张图片打标（内部走批量推理）。"""
        results = self.tag_images([image_path])
        return results[0] if results else []

    def tag_images(self, image_paths: list[str]) -> list[list[TagResult]]:
        """
        批量打标：多张图片拼成一个 batch 一次推理，显著提升 GPU 利用率。
        返回与输入等长的标签列表（每项为按置信度降序的 TagResult 列表）。
        """
        if not image_paths:
            return []
        if self.session is None:
            if not self.load():
                raise RuntimeError(self.error or "模型未加载")
        import numpy as np
        # 预处理：批量拼接成 [N, 3, H, W]
        xs = []
        valid_idx = []
        for i, p in enumerate(image_paths):
            try:
                xs.append(self._preprocess(p))
                valid_idx.append(i)
            except Exception as exc:  # noqa: BLE001
                logger.warning("预处理失败 %s：%s", p, exc)
        if not xs:
            return [[] for _ in image_paths]
        x = np.concatenate(xs, axis=0)  # [N, 3, H, W]
        input_name = self.session.get_inputs()[0].name
        # 一次推理整个 batch
        logits_batch = self.session.run(None, {input_name: x})[0]
        threshold = float(self.config.get("threshold", 0.5))
        include_rating = bool(self.config.get("include_rating", False))
        mode = self.config.get("preprocess_mode", "wd14")
        has_string_cats = any(isinstance(c, str) for c in self.tag_categories)

        # 逐张处理 batch 输出
        per_image: dict[int, list[TagResult]] = {}
        for k, probs in enumerate(logits_batch):
            results: list[TagResult] = []
            if has_string_cats:
                # ---- cl_tagger 风格：按分类过滤 ----
                rating_candidates: list[tuple[float, str]] = []
                for i, p in enumerate(probs):
                    if i >= len(self.tag_names) or not self.tag_names[i]:
                        continue
                    cat = self.tag_categories[i] if i < len(self.tag_categories) else "Unknown"
                    tag = self.tag_names[i]
                    if "_" in tag and not self.config.get("keep_underscore", False):
                        tag = tag.replace("_", " ")
                    if cat == "Rating":
                        rating_candidates.append((float(p), tag))
                    elif cat in ("General", "Character"):
                        if p >= threshold:
                            results.append(TagResult(tag=tag, confidence=float(p)))
                    elif include_rating:
                        if p >= threshold:
                            results.append(TagResult(tag=tag, confidence=float(p)))
                if include_rating and rating_candidates:
                    best = max(rating_candidates, key=lambda x: x[0])
                    results.append(TagResult(tag=best[1], confidence=best[0]))
            else:
                # ---- wd14 / 通用风格 ----
                for i, p in enumerate(probs):
                    if i >= len(self.tag_names):
                        break
                    if p < threshold:
                        continue
                    if self.tag_categories and self.tag_categories[i] == 9 and not include_rating:
                        continue
                    tag = self.tag_names[i]
                    if "_" in tag and not self.config.get("keep_underscore", False):
                        tag = tag.replace("_", " ")
                    results.append(TagResult(tag=tag, confidence=float(p)))
            # 去重并排序
            seen: set[str] = set()
            dedup: list[TagResult] = []
            for r in results:
                if r.tag and r.tag not in seen:
                    seen.add(r.tag)
                    dedup.append(r)
            dedup.sort(key=lambda r: -r.confidence)
            per_image[valid_idx[k]] = dedup

        # 按原顺序返回（缺失的返回空列表）
        return [per_image.get(i, []) for i in range(len(image_paths))]

    def get_config(self) -> dict:
        cfg = dict(self.config)
        cfg.setdefault("input_size", 224)
        cfg.setdefault("threshold", 0.5)
        cfg.setdefault("use_directml", True)
        return cfg
