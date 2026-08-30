# -*- coding: utf-8 -*-
"""
媒体附加数据提取模块（EXIF / IPTC / XMP）
========================================
按需从媒体文件读取附加元数据，供「侧边栏附加数据」展示与「元数据/正则打标」使用。

设计原则：
    - 只读取，绝不修改文件；
    - 纯标准库 + Pillow，零新增依赖；
    - 提取失败返回空段，绝不因单个文件异常抛错（该模块与解耦，不牵扯数据库）；
    - 仅在需要时调用（查看图片时），不写入数据库、不做持久缓存（按需提取方案）。
"""
from __future__ import annotations

import logging
import os
import struct
from xml.etree import ElementTree

logger = logging.getLogger("imagedb.metadata")

# ---- EXIF 标签名映射（Pillow，可选依赖） ----
try:
    from PIL import Image, ExifTags
    HAS_PIL = True
    EXIF_TAGS = dict(ExifTags.TAGS)
except Exception:  # noqa: BLE001 - 未安装 Pillow 则跳过 EXIF
    HAS_PIL = False
    EXIF_TAGS = {}


def _dec(value):
    """bytes -> 可读字符串；失败则保留原始值（不抛错）。"""
    if isinstance(value, bytes):
        for enc in ("utf-8", "gbk", "shift_jis", "latin-1"):
            try:
                return value.decode(enc)
            except Exception:
                continue
        return value.hex()
    return value


# ---------------- EXIF ----------------
def _extract_exif(path):
    """用 Pillow 读取 EXIF（含 0th / Exif IFD / GPS IFD）。"""
    if not HAS_PIL:
        return {}
    result: dict = {}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return result

            def add(ifd, prefix=""):
                for tag_id, val in ifd.items():
                    if isinstance(val, dict):   # 嵌套 IFD，跳过
                        continue
                    name = EXIF_TAGS.get(tag_id, f"0x{tag_id:X}")
                    if prefix:
                        name = prefix + "/" + name
                    if isinstance(val, bytes):
                        if len(val) > 512:      # 跳过二进制大数据（如 MakerNote）
                            continue
                        val = _dec(val)
                    result[name] = str(val)

            add(exif)
            try:
                add(exif.get_ifd(0x8769), "Exif")   # Exif IFD
            except Exception:
                pass
            try:
                add(exif.get_ifd(0x8825), "GPS")    # GPS IFD
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("EXIF 提取失败 %s：%s", path, exc)
    return result


# ---------------- JPEG 段扫描（供 IPTC / XMP 使用） ----------------
def _jpeg_app_segments(path):
    """返回 {app_id: [payload, ...]}，仅 JPEG；失败返回 {}。"""
    out: dict = {}
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":   # 只读 SOI 2 字节，不能吞掉下一个标记的首字节
                return out
            while True:
                b0 = f.read(1)
                if not b0:
                    break
                if b0 != b"\xff":
                    continue
                m = f.read(1)
                if not m:
                    break
                b = m[0]
                if b in (0xd8, 0xd9) or 0xd0 <= b <= 0xd7 or b == 0x01:
                    continue
                if b == 0xda:            # SOS：进入压缩数据，之后无更多 APP 段
                    break
                ln = struct.unpack(">H", f.read(2))[0]
                payload = f.read(ln - 2)
                if 0xe0 <= b <= 0xef:    # APPn 段
                    out.setdefault(b - 0xe0, []).append(payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("JPEG 段解析失败 %s：%s", path, exc)
    return out


# ---------------- IPTC（IIM） ----------------
def _parse_iim(data):
    """解析 IPTC IIM 数据集，返回 {标签名: 值}（Keywords 为列表）。"""
    result: dict = {}
    names = {
        (2, 0): "RecordVersion", (2, 5): "ObjectName", (2, 7): "EditStatus",
        (2, 10): "Urgency", (2, 15): "Category", (2, 20): "SupplementalCategory",
        (2, 25): "Keywords", (2, 40): "SpecialInstructions", (2, 45): "Reference",
        (2, 55): "CreatedDate", (2, 60): "CreatedTime", (2, 62): "DigitizedDate",
        (2, 63): "DigitizedTime", (2, 65): "Program", (2, 70): "ProgramVersion",
        (2, 80): "Byline", (2, 85): "BylineTitle", (2, 90): "City",
        (2, 92): "SubLocation", (2, 95): "State", (2, 100): "CountryCode",
        (2, 101): "Country", (2, 103): "TransmissionReference", (2, 105): "Headline",
        (2, 110): "Credit", (2, 115): "Source", (2, 116): "Copyright",
        (2, 118): "Contact", (2, 120): "Caption", (2, 122): "CaptionWriter",
        (2, 123): "Instructions",
    }
    n = len(data)
    i = 0
    while i < n:
        if data[i] != 0x1c:
            i += 1
            continue
        if i + 5 > n:
            break
        record, ds = data[i + 1], data[i + 2]
        ln = struct.unpack(">H", data[i + 3:i + 5])[0]
        if ln & 0x8000:                 # 扩展长度：高位置位，随后 4 字节为真实长度
            if i + 9 > n:
                break
            ln = struct.unpack(">I", data[i + 5:i + 9])[0]
            val_start = i + 9
        else:
            val_start = i + 5
        if val_start + ln > n:
            break
        val = data[val_start:val_start + ln]
        name = names.get((record, ds))
        if name:
            s = _dec(val).strip()
            if name == "Keywords":
                result.setdefault("Keywords", []).append(s)
            elif name not in result:
                result[name] = s
        i = val_start + ln
    return result


def _extract_iptc(path):
    """解析 JPEG APP13「Photoshop 3.0」中的 IPTC(0x0404) 资源。"""
    for payload in _jpeg_app_segments(path).get(13, []):
        if not payload.startswith(b"Photoshop 3.0\x00"):
            continue
        body = payload[len(b"Photoshop 3.0\x00"):]
        i = 0
        n = len(body)
        while i + 12 <= n:
            if body[i:i + 4] != b"8BIM":
                break
            rid = struct.unpack(">H", body[i + 4:i + 6])[0]
            name_len = body[i + 6]
            pos = i + 7 + name_len
            if (name_len + 1) % 2:       # Pascal 名字补偶
                pos += 1
            if pos + 4 > n:
                break
            size = struct.unpack(">I", body[pos:pos + 4])[0]
            data_start = pos + 4
            if data_start + size > n:
                break
            if rid == 0x0404:
                return _parse_iim(body[data_start:data_start + size])
            i = data_start + size
            if size % 2:                 # 数据补偶
                i += 1
    return {}


# ---------------- XMP ----------------
_XMP_LABELS = {
    "subject": "Keywords", "title": "Title", "description": "Description",
    "creator": "Creator", "rights": "Rights", "keywords": "Keywords",
    "label": "Label", "rating": "Rating", "headline": "Headline",
    "source": "Source", "city": "City", "country": "Country",
    "createdate": "CreateDate", "creatortool": "CreatorTool",
    "modifydate": "ModifyDate", "credit": "Credit", "caption": "Caption",
    "writer": "Writer", "copyright": "Copyright", "format": "Format",
}


def _collect_texts(elem):
    """递归收集 XMP 属性值（rdf:li 或直接文本）。"""
    texts = []
    for child in elem.iter():
        if child.tag.endswith("}li") or child.tag == "li":
            if child.text and child.text.strip():
                texts.append(child.text.strip())
    if not texts and elem.text and elem.text.strip():
        texts.append(elem.text.strip())
    return texts


_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def _parse_xmp_xml(data):
    """解析 XMP 的 RDF/XML，返回简化键值字典。"""
    result: dict = {}
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("XMP XML 解析失败：%s", exc)
        return result
    for elem in root.iter():
        tag = elem.tag
        if not isinstance(tag, str) or "}" not in tag:
            continue
        # 跳过 RDF 结构元素（rdf:RDF / rdf:Description / rdf:Bag / rdf:li 等），
        # 只取真正的元数据属性元素（dc:* / xmp:* / photoshop:* 等）。
        uri, _, local = tag[1:].partition("}")
        if uri == _RDF_NS:
            continue
        local = local.lower()
        label = _XMP_LABELS.get(local)
        if not label:
            continue
        vals = _collect_texts(elem)
        if label == "Keywords":
            for v in vals:
                result.setdefault("Keywords", [])
                if v not in result["Keywords"]:
                    result["Keywords"].append(v)
        elif label not in result and vals:
            result[label] = vals[0]
    return result


def _extract_xmp(path):
    """解析 JPEG APP1 的 XMP 包（XML）。"""
    header = b"http://ns.adobe.com/xap/1.0/\x00"
    for payload in _jpeg_app_segments(path).get(1, []):
        if payload.startswith(header):
            return _parse_xmp_xml(payload[len(header):])
    return {}



def _probe_basic(path: str, mtype: str) -> dict:
    """只读探测文件基本信息（不写库）。

    可能包含：filename / path / type / size / width / height /
              format(图片) / codec(视频) / duration(视频秒) / bitrate(视频平均码率 bps) /
              created(创建时间 unix秒) / modified(修改时间 unix秒)。
    任一字段失败都会优雅降级，绝不抛错。
    """
    info: dict = {"path": path, "filename": os.path.basename(path), "type": mtype}
    try:
        st = os.stat(path)
        info["size"] = st.st_size
        info["created"] = int(st.st_ctime)      # Windows 上 st_ctime = 文件创建时间
        info["modified"] = int(st.st_mtime)     # 文件修改时间
    except OSError:
        pass
    try:
        if mtype == "image":
            from PIL import Image
            with Image.open(path) as im:
                info["format"] = im.format or os.path.splitext(path)[1].lstrip(".").upper()
                info["width"], info["height"] = im.size
        elif mtype == "video":
            try:
                import cv2
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                    fc = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
                    info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                    info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
                    if fps > 0 and fc > 0:
                        info["duration"] = round(fc / fps, 3)
                    try:
                        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                        # OpenCV 的 fourcc 首个字符在低字节（CV_FOURCC 宏），用位移解码
                        info["codec"] = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ").upper()
                    except Exception:
                        pass
                    cap.release()
            except Exception:
                pass
    except Exception:
        pass
    # 视频平均码率 = 字节数 * 8 / 时长（秒）
    if info.get("size") and info.get("duration"):
        info["bitrate"] = int(info["size"] * 8 / info["duration"])
    return info


# ---------------- 对外入口 ----------------
def extract_metadata(path: str, mtype: str = "image") -> dict:
    """按需提取某媒体的基本信息与 EXIF/IPTC/XMP。

    返回 {"basic": {...}, "exif": {...}, "iptc": {...}, "xmp": {...}}。
    视频无附加数据（exif/iptc/xmp 为空段）；文件缺失或解析失败一律返回空段，绝不抛错。
    """
    meta = {"basic": {}, "exif": {}, "iptc": {}, "xmp": {}}
    try:
        if not os.path.isfile(path):
            return meta
        meta["basic"] = _probe_basic(path, mtype)
        if mtype == "image":
            meta["exif"] = _extract_exif(path)
            meta["iptc"] = _extract_iptc(path)
            meta["xmp"] = _extract_xmp(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("附加数据提取失败 %s：%s", path, exc)
    return meta

