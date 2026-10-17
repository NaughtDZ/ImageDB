# -*- coding: utf-8 -*-
"""以图找图：完全相同（内容哈希）、视觉近似（感知哈希）、标签相似。

设计边界（务必保留）：
    - 索引只是**可重建的缓存**：单独的 media_hashes 表，删除不影响媒体记录本身；
    - 建索引只**读**原文件，绝不修改、移动、删除任何媒体；
    - 读不到的文件只计入失败数，绝不因此标记缺片或改动状态；
    - 上传的查询图只在内存里解码，不落盘、不入库。
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np

from .database import (DATA_DIR, chunk_ids, execute, executemany, query_all, query_one)

logger = logging.getLogger("imagedb.similar")

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

HASH_BYTES = 8
DEFAULT_MAX_DISTANCE = 6      # 汉明距离默认上限（0=几乎像素级一致）
MAX_DISTANCE_LIMIT = 24
MAX_READ_BYTES = 32 * 1024 * 1024   # 单张图一次读入内存的上限（并发生效，别太大）
# 像素规模分级：
#   > LARGE_PIXELS 的图**串行解码**（一次只解一张），否则 4 个线程同时吃几百 MB，
#   机器会明显卡顿、看起来像"卡死"；
#   > HUGE_PIXELS 的直接跳过并计数（本地库里最大的图有 8 亿像素级，不值得为它停摆）。
LARGE_PIXELS = 40_000_000
HUGE_PIXELS = 400_000_000
_large_lock = threading.Lock()
BATCH_WRITE = 200
MAX_QUERY_TOP = 500
MAX_FAILURE_SAMPLE = 5000      # 失败清单最多记录这么多条（足够定位，也不会撑爆内存）
# PIL 不带这些格式的解码器，失败是"格式不支持"而不是"文件坏了"，分开报更好定位
UNSUPPORTED_EXTS = {".svg"}

# ---- 32x32 DCT 矩阵（只算一次）----
_DCT = np.empty((32, 32), dtype=np.float32)
for _u in range(32):
    for _x in range(32):
        _DCT[_u, _x] = np.cos((2 * _x + 1) * _u * np.pi / 64)

_POP = np.array([bin(_i).count("1") for _i in range(256)], dtype=np.uint8)
_HAS_BITCOUNT = hasattr(np, "bitwise_count")

if HAS_PIL:
    # 本地素材库里本来就有上亿像素的图，PIL 默认 8900 万像素的"炸弹"阈值
    # 会不停刷 DecompressionBombWarning。这里把上限抬到 HUGE_PIXELS，
    # 超过它的由下面的 _hashes_for 明确跳过并计数，而不是靠警告表达。
    Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, HUGE_PIXELS)

_mirror_lock = threading.Lock()
_mirror: dict | None = None
_cancel = threading.Event()


# ---------------------------------------------------------------- 哈希计算
def _pack_bits(bits: np.ndarray) -> int:
    """把 64 个布尔位打包成 uint64 整数。"""
    v = 0
    for b in bits.ravel():
        v = (v << 1) | int(bool(b))
    return v


def _to_gray(im, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(im.convert("L").resize(size, Image.LANCZOS), dtype=np.float32)


def perceptual_hashes(im) -> tuple[int, int, int]:
    """返回 (pHash, dHash, aHash)，各 64 位。

    三者的容错方向不同：pHash 对整体亮度/压缩稳，dHash 对水平结构敏感，
    aHash 最宽松。查询时三者同时落在阈值内才算命中，可显著压低误报。
    """
    im = ImageOps.exif_transpose(im)   # 统一方向，否则旋转后的同一张图哈希不同

    g32 = _to_gray(im, (32, 32))
    c = _DCT @ g32 @ _DCT.T
    low = c[:8, :8].ravel()
    ph = _pack_bits(low > np.median(low[1:]))

    g98 = _to_gray(im, (9, 8))
    dh = _pack_bits(g98[:, 1:] > g98[:, :-1])

    g8 = _to_gray(im, (8, 8))
    ah = _pack_bits(g8 > g8.mean())

    return ph, dh, ah


def content_hash(data: bytes) -> bytes:
    """文件内容指纹（16 字节 blake2b），用于「完全相同」。"""
    return hashlib.blake2b(data, digest_size=16).digest()


def _hashes_for(im) -> dict:
    """按像素规模决定解码方式，再算三种哈希。

    超大图串行解码（_large_lock），避免多个工作线程同时申请几百 MB；
    超过 HUGE_PIXELS 的返回 {"too_large": True}，由调用方计数跳过，绝不让它拖死整批。
    """
    px = int((im.width or 0) * (im.height or 0))
    if px > HUGE_PIXELS:
        return {"too_large": True, "pixels": px}
    if px > LARGE_PIXELS:
        with _large_lock:
            im.load()
            ph, dh, ah = perceptual_hashes(im)
    else:
        im.load()
        ph, dh, ah = perceptual_hashes(im)
    return {"phash": ph, "dhash": dh, "ahash": ah, "pixels": px}


def _hash_bytes_or_raise(data: bytes) -> dict:
    """解码 + 算哈希；解不开就抛异常，由上层决定记什么错误。"""
    with Image.open(io.BytesIO(data)) as im:
        # 不用 im.draft()：draft 的解码比例随原图尺寸变化，
        # 同一张图的不同分辨率副本会得到不同结果（哈希必须与分辨率无关）。
        info = _hashes_for(im)
    if info.get("too_large"):
        return info
    info.update(chash=content_hash(data), size=len(data))
    return info


def hash_bytes(data: bytes) -> dict | None:
    """对上传的查询图算哈希；无法解码返回 None（调用方回 400）。"""
    if not HAS_PIL or not data:
        return None
    try:
        return _hash_bytes_or_raise(data)
    except Exception as exc:  # noqa: BLE001  解不开的格式只跳过，不算错误
        logger.debug("无法解码查询图：%s", exc)
        return None


def hash_file(path: str, size: int | None = None) -> dict | None:
    """读取磁盘文件并算哈希。

    **失败必须可解释**：返回 {"error": 原因, "detail": 说明}，绝不静默返回 None
    （之前就是静默返回 None，导致失败了几百张却一条日志都没有）。
      unreadable  读不到（文件消失/权限/网络盘掉线）
      empty       0 字节
      decode      能读但解不开（格式不支持、文件损坏、截断）
    成功返回含 phash/dhash/ahash/chash 的字典。
    """
    try:
        if size is None:
            size = os.path.getsize(path)
        if size <= 0:
            return {"error": "empty", "detail": "文件大小为 0"}
        with open(path, "rb") as fh:
            data = fh.read(MAX_READ_BYTES)
            big = bool(fh.read(1))
    except OSError as exc:
        return {"error": "unreadable", "detail": str(exc)}
    if big:
        # 超大图：流式计算内容哈希，再单独解码一次（避免整文件驻留内存）
        try:
            h = hashlib.blake2b(digest_size=16)
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(4 << 20), b""):
                    h.update(block)
        except OSError as exc:
            return {"error": "unreadable", "detail": str(exc)}
        try:
            with Image.open(path) as im:
                info = _hashes_for(im)
        except Exception as exc:  # noqa: BLE001
            return {"error": "decode", "detail": f"{type(exc).__name__}: {exc}"}
        if info.get("too_large"):
            return info
        info.update(chash=h.digest(), size=size)
        return info
    try:
        return _hash_bytes_or_raise(data)
    except Exception as exc:  # noqa: BLE001
        return {"error": "decode", "detail": f"{type(exc).__name__}: {exc}"}


def _blob(value: int) -> bytes:
    return int(value).to_bytes(HASH_BYTES, "big", signed=False)


def _unblob(blob: bytes) -> int:
    return int.from_bytes(blob, "big", signed=False)


# ---------------------------------------------------------------- 索引维护
def _hashable_rows(folder_id: int | None, after_id: int, limit: int) -> list[dict]:
    sql = ("SELECT id, path, size, mtime FROM media_items"
           " WHERE type = 'image' AND id > ? AND COALESCE(status,'ok') != 'missing'")
    args: list = [after_id]
    if folder_id is not None:
        from . import library
        ids = library._subtree_folder_ids(folder_id)
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        sql += f" AND folder_id IN ({ph})"
        args.extend(ids)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    return query_all(sql, args)


def index_total(folder_id: int | None = None) -> int:
    sql = ("SELECT COUNT(*) n FROM media_items WHERE type = 'image'"
           " AND COALESCE(status,'ok') != 'missing'")
    args: list = []
    if folder_id is not None:
        from . import library
        ids = library._subtree_folder_ids(folder_id)
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        sql += f" AND folder_id IN ({ph})"
        args.extend(ids)
    return query_one(sql, args)["n"]


def build_index(folder_id: int | None = None, progress_cb=None,
                should_cancel=None, workers: int = 4) -> dict:
    """给媒体建/补哈希索引。只读原文件，不修改任何媒体记录。

    workers：并发读取线程数。网络盘上每张图有一半时间在等 I/O，
    实测单线程约 22 小时才能走完 45 万张，4 线程可压到几小时；
    写库仍然只由主线程做，避免并发写。
    """
    _cancel.clear()
    total = index_total(folder_id)
    done = fresh = added = failed = skipped_large = 0
    by_reason: dict[str, int] = {}
    failures: list[dict] = []
    pos = 0
    pending: list[tuple] = []
    workers = max(1, min(8, int(workers or 1)))
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    page = 128
    window = max(2, workers * 2)     # 同时在飞的图片数：小窗口才能随时停、随时报进度

    def flush() -> None:
        nonlocal pending
        if pending:
            executemany(
                "INSERT INTO media_hashes(media_id, phash, dhash, ahash, chash,"
                " src_size, src_mtime, updated_at)"
                " VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))"
                " ON CONFLICT(media_id) DO UPDATE SET"
                " phash=excluded.phash, dhash=excluded.dhash, ahash=excluded.ahash,"
                " chash=excluded.chash, src_size=excluded.src_size,"
                " src_mtime=excluded.src_mtime, updated_at=excluded.updated_at",
                pending)
            pending = []

    started = time.time()
    while True:
        if _cancel.is_set() or (should_cancel and should_cancel()):
            break
        rows = _hashable_rows(folder_id, pos, page)
        if not rows:
            break
        pos = rows[-1]["id"]
        known = {}
        for chunk in chunk_ids([r["id"] for r in rows]):
            ph = ",".join("?" * len(chunk))
            for r in query_all(
                    "SELECT media_id, src_size, src_mtime FROM media_hashes"
                    f" WHERE media_id IN ({ph})", chunk):
                known[r["media_id"]] = r
        def stale(row) -> bool:
            old = known.get(row["id"])
            return not (old and old["src_size"] == row["size"]
                        and abs((old["src_mtime"] or 0) - (row["mtime"] or 0)) < 1e-6)

        todo = [r for r in rows if stale(r)]
        done += len(rows) - len(todo)
        fresh += len(rows) - len(todo)

        def consume(row, info) -> None:
            """处理一张图的结果：写库 + 进度。**每张都回报**，界面才不会像卡死。"""
            nonlocal done, added, failed, skipped_large
            done += 1
            reason = detail = None
            if info is None:
                reason, detail = "unknown", "没有返回结果"
            elif info.get("too_large"):
                skipped_large += 1
            elif info.get("error"):
                reason = info["error"]
                detail = info.get("detail") or ""
            else:
                pending.append((row["id"], _blob(info["phash"]), _blob(info["dhash"]),
                                _blob(info["ahash"]), info["chash"],
                                row["size"], row["mtime"]))
                added += 1
                if len(pending) >= BATCH_WRITE:
                    flush()
            if reason:
                if (reason == "decode"
                        and os.path.splitext(row["path"])[1].lower() in UNSUPPORTED_EXTS):
                    reason = "unsupported"
                failed += 1
                by_reason[reason] = by_reason.get(reason, 0) + 1
                if len(failures) < MAX_FAILURE_SAMPLE:
                    failures.append({"path": row["path"], "reason": reason,
                                     "detail": detail[:200]})
                # 前 20 条打到日志（够定位问题），其余降为 DEBUG 免得刷屏——
                # 但**最后一定会汇总并落盘**，不再出现"失败了几百张却毫无输出"。
                log = logger.warning if failed <= 20 else logger.debug
                log("哈希失败[%s] %s：%s", reason, row["path"], detail)
            if not progress_cb:
                return
            elapsed = max(1e-6, time.time() - started)
            rate = done / elapsed
            eta = (total - done) / max(1e-6, rate)
            name = os.path.basename(row["path"])[:36]
            progress_cb(done, total,
                        f"{done}/{total} · 新增 {added} · 跳过 {fresh} · 失败 {failed}"
                        f" · {rate * 60:.0f} 张/分 · 剩 {int(eta // 60)} 分 · {name}")

        if _cancel.is_set():
            break
        if pool is not None and todo:
            # 滚动窗口：谁先算完谁先写库、先报进度。
            # 不用 pool.map —— 那是个"整页屏障"，一张超大图就能让 128 张的进度全停住。
            inflight: dict = {}
            it = iter(todo)

            def refill() -> None:
                while len(inflight) < window:
                    nxt = next(it, None)
                    if nxt is None:
                        return
                    inflight[pool.submit(hash_file, nxt["path"], nxt["size"])] = nxt

            refill()
            while inflight and not _cancel.is_set():
                finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in finished:
                    row = inflight.pop(fut)
                    try:
                        info = fut.result()
                    except Exception as exc:  # noqa: BLE001  单张失败不能拖垮整批
                        logger.debug("哈希失败 %s：%s", row["path"], exc)
                        info = None
                    consume(row, info)
                refill()
        else:
            for row in todo:
                if _cancel.is_set():
                    break
                consume(row, hash_file(row["path"], row["size"]))
    if pool is not None:
        pool.shutdown(wait=True)
    flush()
    elapsed = time.time() - started
    invalidate()

    failure_file = ""
    if failures:
        # 失败清单落盘，方便直接用记事本查是哪几张、为什么
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            failure_file = os.path.join(DATA_DIR, "similar_index_failures.txt")
            with open(failure_file, "w", encoding="utf-8") as fh:
                fh.write(f"# 以图搜图索引失败清单 {len(failures)} 条"
                         f"（{time.strftime('%Y-%m-%d %H:%M:%S')}）\n")
                fh.write("# 原因\t路径\t详情\n")
                for item in failures:
                    fh.write(f"{item['reason']}\t{item['path']}\t{item['detail']}\n")
        except OSError as exc:
            logger.warning("写失败清单失败：%s", exc)
            failure_file = ""

    if failed:
        logger.warning("哈希索引有 %d 张失败（%s），清单：%s",
                       failed, by_reason, failure_file or "未能落盘")
    logger.info("哈希索引完成：新增 %d，已有 %d，失败 %d，超大跳过 %d，用时 %.1fs",
                added, fresh, failed, skipped_large, elapsed)
    return {"total": total, "done": done, "added": added, "skipped": fresh,
            "failed": failed, "skipped_large": skipped_large,
            "failed_by_reason": by_reason, "failure_file": failure_file,
            "failure_sample": failures[:20],
            "cancelled": _cancel.is_set(), "seconds": round(elapsed, 1)}


def cancel_index() -> None:
    _cancel.set()


def index_stats() -> dict:
    row = query_one("SELECT COUNT(*) n FROM media_hashes")
    return {"indexed": row["n"] if row else 0, "total": index_total(),
            "running": _build_running(), "cancelled": _cancel.is_set()}


_running = threading.Event()


def _build_running() -> bool:
    return _running.is_set()


def mark_running(flag: bool) -> None:
    if flag:
        _running.set()
    else:
        _running.clear()


# ---------------------------------------------------------------- 内存镜像
def invalidate() -> None:
    global _mirror, _chash_map
    with _mirror_lock:
        _mirror = None
        _chash_map = None


_chash_map: dict | None = None


def _load_mirror() -> dict:
    # 只收三个哈希都齐全的行：写库中途被打断也不会让镜像构建崩掉
    rows = [r for r in query_all(
        "SELECT media_id, phash, dhash, ahash, chash FROM media_hashes")
        if r["phash"] and r["dhash"] and r["ahash"]]
    n = len(rows)
    ids = np.fromiter((r["media_id"] for r in rows), dtype=np.int64, count=n)
    chashes = [r["chash"] for r in rows]

    def column(key: str) -> np.ndarray:
        joined = b"".join(r[key] for r in rows)
        return np.frombuffer(joined, dtype=">u8").astype(np.uint64)

    return {"ids": ids, "phash": column("phash"), "dhash": column("dhash"),
            "ahash": column("ahash"), "chash": chashes, "n": n}


def _exact_map(mirror: dict) -> dict:
    """内容哈希 → 镜像下标。**懒构建**：45 万张的字典约 80 MB，
    只做「找近似」时没必要付这笔内存。"""
    global _chash_map
    if _chash_map is None:
        _chash_map = {c: i for i, c in enumerate(mirror["chash"]) if c}
    return _chash_map


def warm() -> None:
    """后台预热镜像，避免第一次搜索等太久。"""
    def run():
        try:
            get_mirror()
        except Exception as exc:  # noqa: BLE001
            logger.warning("哈希镜像预热失败：%s", exc)
    threading.Thread(target=run, daemon=True, name="similar-warm").start()


def get_mirror(force: bool = False) -> dict:
    global _mirror
    with _mirror_lock:
        if _mirror is None or force:
            _mirror = _load_mirror()
        return _mirror


def _hamming(arr: np.ndarray, value: int) -> np.ndarray:
    x = arr ^ np.uint64(value)
    if _HAS_BITCOUNT:
        return np.bitwise_count(x)
    bytes_view = np.ascontiguousarray(x).view(np.uint8).reshape(-1, HASH_BYTES)
    return _POP[bytes_view].sum(axis=1, dtype=np.uint16)


# ---------------------------------------------------------------- 搜索
def search_hashes(query: dict, max_distance: int = DEFAULT_MAX_DISTANCE,
                  top: int = 100) -> list[dict]:
    """按感知哈希 + 内容哈希找相同/近似图，返回按相似度降序的结果。"""
    mirror = get_mirror()
    if not mirror["n"]:
        return []
    max_distance = max(0, min(MAX_DISTANCE_LIMIT, int(max_distance)))
    dp = _hamming(mirror["phash"], query["phash"])
    da = _hamming(mirror["ahash"], query["ahash"])
    # 只用 pHash + aHash 的平均值作为匹配距离。
    # 实测（同一张图的各种副本，见 tests/test_similar_review.py）：
    #     缩小一半+重压  pH=0~2  aH=0~4   → 距离 0~3.5
    #     质量 40 重压   pH=0~2  aH=3~5   → 距离 1.5~3.5
    #     旋转 3°        pH=2~8  aH=16~22 → 距离 9~15（旋转本就难，属已知弱项）
    #     毫不相干的图   pH=34~44 aH=35~40 → 距离 32~42
    # dHash 实测对重采样过于敏感（同一张图缩小一半就能差 9~20 位），
    # 放进平均会把"同一张图"误判成不相似，因此只保存不进判定。
    dist = (dp.astype(np.float32) + da) / 2.0
    hit = np.flatnonzero(dist <= max_distance)
    if hit.size == 0:
        return []
    order = hit[np.argsort(dist[hit], kind="stable")][:top]
    exact_idx = _exact_map(mirror).get(query.get("chash"))
    out = []
    for idx in order:
        d = float(dist[idx])
        exact = int(idx) == exact_idx
        out.append({
            "media_id": int(mirror["ids"][idx]),
            "kind": "exact" if exact else "similar",
            "distance": round(d, 1),
            "score": 1.0 if exact else round(1.0 - d / 64.0, 4),
        })
    return out


def find_exact(query: dict, top: int = 100) -> list[dict]:
    mirror = get_mirror()
    idx = _exact_map(mirror).get(query.get("chash"))
    if idx is None:
        return []
    return [{"media_id": int(mirror["ids"][idx]), "kind": "exact",
             "distance": 0, "score": 1.0}]


def hash_for_media(media_id: int) -> dict | None:
    """取库内某张图的哈希；索引里没有就现算并存下（单图，代价可控）。"""
    row = query_one("SELECT media_id, phash, dhash, ahash, chash, src_size, src_mtime"
                    " FROM media_hashes WHERE media_id = ?", (media_id,))
    if row and row["phash"] and row["dhash"] and row["ahash"]:
        return {"phash": _unblob(row["phash"]), "dhash": _unblob(row["dhash"]),
                "ahash": _unblob(row["ahash"]), "chash": row["chash"]}
    media = query_one("SELECT path, size, mtime FROM media_items WHERE id = ?", (media_id,))
    if not media:
        return None
    info = hash_file(media["path"], media["size"])
    if not info:
        return None
    execute("INSERT INTO media_hashes(media_id, phash, dhash, ahash, chash, src_size,"
            " src_mtime, updated_at) VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))"
            " ON CONFLICT(media_id) DO UPDATE SET phash=excluded.phash, dhash=excluded.dhash,"
            " ahash=excluded.ahash, chash=excluded.chash, src_size=excluded.src_size,"
            " src_mtime=excluded.src_mtime, updated_at=excluded.updated_at",
            (media_id, _blob(info["phash"]), _blob(info["dhash"]), _blob(info["ahash"]),
             info["chash"], media["size"], media["mtime"]))
    invalidate()
    return info


def tag_ids_for_media(media_id: int) -> list[int]:
    return [r["tag_id"] for r in query_all(
        "SELECT tag_id FROM media_tags WHERE media_id = ?", (media_id,))]


def search_by_tags(tag_ids: list[int], min_score: float = 0.3,
                   top: int = 100, exclude: int | None = None) -> list[dict]:
    """按标签交集（Jaccard）找相似图。

    Jaccard = 交集 / 并集，取值 0~1，对「标签多寡不同」的图片更公平：
    一张打了 40 个标签的图和一张打了 5 个标签的图，光看交集数量会失真。
    """
    tag_ids = list(dict.fromkeys(int(t) for t in tag_ids))[:16]
    if not tag_ids:
        return []
    ph = ",".join("?" * len(tag_ids))
    rows = query_all(
        "SELECT media_id, COUNT(*) AS c FROM media_tags"
        f" WHERE tag_id IN ({ph}) GROUP BY media_id"
        " ORDER BY c DESC LIMIT 2000", tag_ids)
    if not rows:
        return []
    counts = {r["media_id"]: r["c"] for r in rows}
    sizes: dict[int, int] = {}
    for chunk in chunk_ids(list(counts)):
        ph2 = ",".join("?" * len(chunk))
        for r in query_all(
                "SELECT media_id, COUNT(*) AS n FROM media_tags"
                f" WHERE media_id IN ({ph2}) GROUP BY media_id", chunk):
            sizes[r["media_id"]] = r["n"]
    qn = len(tag_ids)
    out = []
    for mid, shared in counts.items():
        if exclude is not None and mid == exclude:
            continue
        union = qn + sizes.get(mid, shared) - shared
        score = shared / union if union > 0 else 0.0
        if score >= min_score:
            out.append({"media_id": mid, "kind": "tag", "distance": None,
                        "score": round(score, 4), "shared_tags": shared})
    out.sort(key=lambda x: (-x["score"], -x.get("shared_tags", 0), x["media_id"]))
    return out[:top]


def shared_tag_names(query_tags: list[int], media_id: int) -> list[str]:
    if not query_tags:
        return []
    ph = ",".join("?" * len(query_tags))
    rows = query_all(
        "SELECT t.name FROM media_tags mt JOIN tags t ON t.id = mt.tag_id"
        f" WHERE mt.media_id = ? AND mt.tag_id IN ({ph})", [media_id] + query_tags)
    return [r["name"] for r in rows]


def resolve_media(ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for chunk in chunk_ids(ids):
        ph = ",".join("?" * len(chunk))
        for r in query_all(
                "SELECT id, folder_id, path, filename, type, ext, size, mtime, width, height,"
                " duration, thumbnail, status FROM media_items"
                f" WHERE id IN ({ph})", chunk):
            out[r["id"]] = r
    return out
