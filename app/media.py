# -*- coding: utf-8 -*-
"""
媒体处理模块
============
职责：
    1. 生成图片/视频缩略图（存 data/thumbs/，路径写入数据库）；
    2. 探测视频时长/尺寸；
    3. 视频抽帧（供打标使用），输出到 data/frames/。

依赖容错：
    - Pillow 用于图片处理（核心依赖）；
    - OpenCV 为可选依赖（视频缩略图与抽帧需要），未安装时自动降级，
      视频功能（抽帧打标、视频缩略图）不可用，其余功能不受影响。
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid

from .database import FRAMES_DIR, THUMBS_DIR, execute, query_one

logger = logging.getLogger("imagedb.media")

# OpenCV 可选导入
try:
    import cv2  # type: ignore
    HAS_CV2 = True
except Exception:  # noqa: BLE001 - 导入失败视为未安装
    HAS_CV2 = False

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except Exception:  # noqa: BLE001
    HAS_PIL = False


def _thumb_dir() -> str:
    """缩略图目录（不存在则创建）。"""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    return THUMBS_DIR


def make_thumbnail(media_id: int, path: str, mtype: str,
                   thumb_sec: float = 1.0, thumb_size: int = 320) -> str | None:
    """
    生成缩略图并写入数据库 thumbnail 字段。
    返回缩略图相对路径（如 thumbs/123.jpg），失败返回 None。
    """
    if not HAS_PIL:
        logger.warning("Pillow 未安装，无法生成缩略图")
        return None

    out_rel = f"thumbs/{media_id}.jpg"   # 相对 data/ 目录的正斜杠路径
    out_abs = os.path.join(_thumb_dir(), f"{media_id}.jpg")

    try:
        if mtype == "image":
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)  # 遵循 EXIF 方向
                im = im.convert("RGB")
                im.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
                im.save(out_abs, "JPEG", quality=85)
                # 顺带记录图片尺寸
                execute("UPDATE media_items SET width = ?, height = ? WHERE id = ?",
                        (im.size[0], im.size[1], media_id))

        elif mtype == "video":
            if not HAS_CV2:
                return None
            cap = cv2.VideoCapture(path)  # type: ignore
            if not cap.isOpened():
                cap.release()
                return None
            # 定位到指定秒数的帧作为封面
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, thumb_sec) * 1000.0)
            ok, frame = cap.read()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            duration = (frame_count / fps) if fps > 0 and frame_count > 0 else None
            cap.release()
            if not ok or frame is None:
                return None
            # 记录视频尺寸与时长
            execute("UPDATE media_items SET width = ?, height = ?, duration = ? WHERE id = ?",
                    (w, h, duration, media_id))
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))  # type: ignore
            im.thumbnail((thumb_size, thumb_size), Image.LANCZOS)
            im.save(out_abs, "JPEG", quality=85)

        else:
            return None

        execute("UPDATE media_items SET thumbnail = ? WHERE id = ?", (out_rel, media_id))
        return out_rel
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成缩略图失败 %s：%s", path, exc)
        return None


def probe_video(path: str) -> tuple[int | None, int | None, float | None]:
    """探测视频尺寸与时长，返回 (宽, 高, 时长秒)；失败返回 (None, None, None)。"""
    if not HAS_CV2:
        return None, None, None
    try:
        cap = cv2.VideoCapture(path)  # type: ignore
        if not cap.isOpened():
            cap.release()
            return None, None, None
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        duration = (frame_count / fps) if fps > 0 and frame_count > 0 else None
        cap.release()
        return w, h, duration
    except Exception:  # noqa: BLE001
        return None, None, None


def extract_frames(video_path: str, interval_sec: float, max_frames: int = 0) -> list[str]:
    """
    按间隔抽帧，返回临时图片文件绝对路径列表。
    - interval_sec <= 0 时按“每 1 帧”抽取；
    - max_frames > 0 时最多抽 max_frames 张。
    通过 seek 定位帧位置，避免逐帧读取大视频。
    """
    if not HAS_CV2:
        raise RuntimeError("未安装 OpenCV（opencv-python-headless），无法抽帧")

    out_dir = os.path.join(FRAMES_DIR, uuid.uuid4().hex)
    os.makedirs(out_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)  # type: ignore
    if not cap.isOpened():
        cap.release()
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError(f"无法打开视频：{video_path}")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, int(round(interval_sec * fps))) if interval_sec > 0 else 1
        paths: list[str] = []
        frame_pos = 0
        while True:
            if total and frame_pos >= total:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            out = os.path.join(out_dir, f"frame_{len(paths):04d}.jpg")
            cv2.imwrite(out, frame)  # type: ignore
            paths.append(out)
            if max_frames > 0 and len(paths) >= max_frames:
                break
            frame_pos += step
        if not paths:
            raise RuntimeError(f"视频没有抽到任何帧：{video_path}")
        return paths
    finally:
        cap.release()


def cleanup_frames(paths: list[str]) -> None:
    """清理抽帧产生的临时目录。"""
    for p in paths:
        try:
            d = os.path.dirname(p)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def get_thumbnail_path(media_id: int) -> str | None:
    """读取数据库中记录的缩略图相对路径。"""
    row = query_one("SELECT thumbnail FROM media_items WHERE id = ?", (media_id,))
    if not row or not row["thumbnail"]:
        return None
    return row["thumbnail"]
