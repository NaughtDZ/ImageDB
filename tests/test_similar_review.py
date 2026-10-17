"""以图找图（A 完全相同 + B 感知近似 + 标签相似）隔离回归测试。

所有数据库与图片都在临时目录，绝不接触正式库。
"""
import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app import database as db

# 先替换数据路径，再导入依赖它的模块
_TMP = tempfile.TemporaryDirectory(prefix="imagedb-similar-")
_ROOT = Path(_TMP.name)
for _mod_name, _attr, _sub in (
        ("app.database", "DATA_DIR", "data"),
        ("app.database", "DB_PATH", "data/test.sqlite"),
        ("app.database", "THUMBS_DIR", "data/thumbs"),
        ("app.database", "FRAMES_DIR", "data/frames"),
        ("app.database", "RECYCLE_DIR", "data/recycle")):
    pass
db.DATA_DIR = str(_ROOT / "data")
db.DB_PATH = str(_ROOT / "data" / "test.sqlite")
db.THUMBS_DIR = str(_ROOT / "data" / "thumbs")
db.FRAMES_DIR = str(_ROOT / "data" / "frames")
db.RECYCLE_DIR = str(_ROOT / "data" / "recycle")
db.init_schema()

from app import similar, server  # noqa: E402


def _atmp():
    return tempfile.TemporaryDirectory(prefix="imagedb-similar-case-")


class HashTests(unittest.TestCase):
    def setUp(self):
        # 每个用例独立的库内容（临时目录共享，但表必须清空）
        for table in ("media_hashes", "media_tags", "tags", "media_items", "folders"):
            db.execute("DELETE FROM " + table)
        similar.invalidate()
        self.tmp = _atmp()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.media = self.dir / "media"
        self.media.mkdir()
        self.fid = db.execute(
            "INSERT INTO folders(name,path,is_root) VALUES (?,?,1)",
            ("f" + self.dir.name, str(self.media)))
        self._n = 0

    def add_image(self, name="a.jpg", size=(640, 480), seed=1, smooth=True):
        """造一张"像照片"的图：平滑渐变 + 几个色块。

        刻意不用高频条纹——那种图在缩放后哈希必然大变，
        不能代表真实照片，会把测试结论带偏。
        """
        import random
        rnd = random.Random(seed)
        path = self.media / name
        w, h = size
        base = (rnd.randrange(40, 200), rnd.randrange(40, 200), rnd.randrange(40, 200))
        im = Image.new("RGB", size, base)
        px = im.load()
        for y in range(h):
            for x in range(0, w, 2):
                t = (x / max(1, w - 1)) * 0.6 + (y / max(1, h - 1)) * 0.4
                px[x, y] = (int(base[0] * (1 - t) + 40 * t),
                            int(base[1] * (1 - t) + 220 * t),
                            int(base[2] * (1 - t) + 90 * t))
                if x + 1 < w:
                    px[x + 1, y] = px[x, y]
        for _ in range(4):
            x0, y0 = rnd.randrange(0, w - 80), rnd.randrange(0, h - 80)
            color = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            im.paste(color, (x0, y0, x0 + rnd.randrange(40, 120), y0 + rnd.randrange(40, 120)))
        im.save(path, quality=92)
        mid = db.execute(
            "INSERT INTO media_items(folder_id,path,filename,type,ext,size,mtime,status)"
            " VALUES (?,?,?,'image','jpg',?,?,'ok')",
            (self.fid, str(path), name, path.stat().st_size, path.stat().st_mtime))
        self._n += 1
        return mid, path

    def test_identical_file_is_exact(self):
        mid, path = self.add_image()
        db.execute("DELETE FROM media_hashes")   # 只保留这一张的索引
        self.assertEqual(similar.build_index()["added"], 1)
        info = similar.hash_bytes(path.read_bytes())
        hits = similar.find_exact(info)
        self.assertEqual([h["media_id"] for h in hits], [mid])
        self.assertEqual(hits[0]["kind"], "exact")
        self.assertEqual(hits[0]["score"], 1.0)

    def test_recompressed_and_resized_is_near_duplicate(self):
        mid, path = self.add_image("orig.jpg", size=(640, 480))
        similar.build_index()
        with Image.open(path) as im:
            small = im.resize((320, 240), Image.LANCZOS)
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=60)
        info = similar.hash_bytes(buf.getvalue())
        hits = similar.search_hashes(info, max_distance=6, top=10)
        self.assertIn(mid, [h["media_id"] for h in hits])
        self.assertLessEqual(next(h["distance"] for h in hits if h["media_id"] == mid), 6)

    def test_different_image_not_matched_at_strict_threshold(self):
        noise_a = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
        noise_b = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
        buf_a, buf_b = io.BytesIO(), io.BytesIO()
        noise_a.save(buf_a, "PNG")
        noise_b.save(buf_b, "PNG")
        ha = similar.hash_bytes(buf_a.getvalue())
        hb = similar.hash_bytes(buf_b.getvalue())
        d = similar._hamming(similar.np.array([ha["phash"]], dtype=similar.np.uint64),
                             hb["phash"])[0]
        self.assertGreater(int(d), 6)

    def test_threshold_controls_result_count(self):
        mid, path = self.add_image("base.jpg")
        similar.build_index()
        with Image.open(path) as im:
            variant = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
            buf = io.BytesIO()
            variant.save(buf, "JPEG", quality=55)     # 常见的"转发图"：缩一半 + 重压
        info = similar.hash_bytes(buf.getvalue())
        strict = similar.search_hashes(info, max_distance=0, top=10)
        loose = similar.search_hashes(info, max_distance=6, top=10)
        self.assertGreaterEqual(len(loose), len(strict))
        self.assertIn(mid, [h["media_id"] for h in loose])
        best = next(h for h in loose if h["media_id"] == mid)
        self.assertLessEqual(best["distance"], 6)
        print("  [阈值测试] 缩小一半+重压 的距离 =", best["distance"])

    def test_index_is_incremental_and_detects_changes(self):
        mid, path = self.add_image()
        first = similar.build_index()
        self.assertEqual((first["added"], first["skipped"]), (1, 0))
        second = similar.build_index()
        self.assertEqual((second["added"], second["skipped"]), (0, 1))
        # 文件变了（大小/时间都变）→ 必须重算
        with Image.open(path) as im:
            im.resize((200, 150)).save(path, quality=80)
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        db.execute("UPDATE media_items SET size=?, mtime=? WHERE id=?",
                   (path.stat().st_size, path.stat().st_mtime, mid))
        third = similar.build_index()
        self.assertEqual(third["added"], 1)

    def test_unreadable_file_only_counts_as_failure(self):
        mid, path = self.add_image()
        path.unlink()                     # 文件消失，但记录仍在
        res = similar.build_index()
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["added"], 0)
        row = db.query_one("SELECT status FROM media_items WHERE id=?", (mid,))
        self.assertEqual(row["status"], "ok")   # 绝不因为建索引改动状态

    def test_missing_media_excluded_from_index(self):
        mid, _ = self.add_image()
        db.execute("UPDATE media_items SET status='missing' WHERE id=?", (mid,))
        self.assertEqual(similar.build_index()["total"], 0)

    def test_tag_similarity_is_jaccard_and_excludes_self(self):
        a, _ = self.add_image("a.jpg")
        b, _ = self.add_image("b.jpg", seed=2)
        c, _ = self.add_image("c.jpg", seed=3)
        def tag(mid, names, source="manual"):
            ids = []
            for n in names:
                row = db.query_one("SELECT id FROM tags WHERE name=?", (n,))
                tid = row["id"] if row else db.execute(
                    "INSERT INTO tags(name,source) VALUES (?,?)", (n, source))
                db.execute("INSERT OR IGNORE INTO media_tags(media_id,tag_id,source)"
                           " VALUES (?,?,?)", (mid, tid, source))
                ids.append(tid)
            return ids
        tag(a, ["cat", "indoors", "blue"])
        tag(b, ["cat", "indoors", "green", "grass"])
        tag(c, ["dog", "outdoors"])
        q = similar.tag_ids_for_media(a)
        hits = {h["media_id"]: h for h in
                similar.search_by_tags(q, min_score=0.1, top=10, exclude=a)}
        self.assertNotIn(a, hits)                 # 不把自己算进来
        self.assertIn(b, hits)
        self.assertNotIn(c, hits)
        # Jaccard = 2 / (3 + 4 - 2) = 0.4
        self.assertAlmostEqual(hits[b]["score"], 0.4, places=4)
        self.assertEqual(hits[b]["shared_tags"], 2)


    def test_huge_image_is_skipped_not_fatal(self):
        """超过上限的巨图只计数跳过，绝不能拖垮整批。"""
        self.add_image("ok1.jpg", seed=11)
        self.add_image("ok2.jpg", seed=12)
        with patch.object(similar, "HUGE_PIXELS", 1000):
            res = similar.build_index()
        self.assertEqual(res["skipped_large"], 2)
        self.assertEqual(res["added"], 0)
        self.assertEqual(res["failed"], 0)          # 不是失败，是明确跳过

    def test_large_image_uses_serialized_decode(self):
        self.add_image("big.jpg", size=(1200, 900), seed=13)
        with patch.object(similar, "LARGE_PIXELS", 1000), \
             patch.object(similar, "HUGE_PIXELS", 10 ** 12):
            res = similar.build_index()
        self.assertEqual(res["added"], 1)
        row = db.query_one("SELECT phash FROM media_hashes")
        self.assertIsNotNone(row["phash"])

    def test_progress_reported_for_every_image(self):
        """进度必须逐张回报：整页一次性回报会让界面看起来卡死。"""
        for i in range(5):
            self.add_image(f"p{i}.jpg", seed=20 + i)
        calls = []
        similar.build_index(progress_cb=lambda d, t, m: calls.append((d, t, m)))
        self.assertGreaterEqual(len(calls), 5)
        self.assertEqual(calls[-1][1], 5)
        self.assertTrue(any("p0.jpg" in m for _, _, m in calls))   # 带文件名，能看出在动

    def test_cancel_keeps_partial_progress(self):
        for i in range(6):
            self.add_image(f"c{i}.jpg", seed=30 + i)
        state = {"n": 0}

        def cb(done, total, msg):
            state["n"] += 1
            if state["n"] >= 2:
                similar.cancel_index()

        res = similar.build_index(progress_cb=cb)
        self.assertTrue(res["cancelled"])
        self.assertLess(res["done"], 6)
        stored = db.query_one("SELECT COUNT(*) n FROM media_hashes")["n"]
        self.assertGreater(stored, 0)               # 已完成的必须落盘，不能白干
        self.assertLess(stored, 6)

    def test_single_unreadable_file_does_not_abort_batch(self):
        mid, path = self.add_image("good.jpg", seed=40)
        bad_mid, bad_path = self.add_image("bad.jpg", seed=41)
        bad_path.write_bytes(b"not an image at all")
        db.execute("UPDATE media_items SET size=?, mtime=? WHERE id=?",
                   (bad_path.stat().st_size, bad_path.stat().st_mtime, bad_mid))
        res = similar.build_index()
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["added"], 1)           # 好的那张照常入库


    def test_failures_are_classified_logged_and_written(self):
        """失败必须有原因、有日志、有清单 —— 不能静默。"""
        mid1, p1 = self.add_image("bad.jpg", seed=50)
        p1.write_bytes(b"definitely not an image")
        db.execute("UPDATE media_items SET size=?, mtime=? WHERE id=?",
                   (p1.stat().st_size, p1.stat().st_mtime, mid1))
        mid2, p2 = self.add_image("gone.jpg", seed=51)
        p2.unlink()
        with self.assertLogs("imagedb.similar", level="WARNING") as logs:
            res = similar.build_index()
        self.assertEqual(res["failed"], 2)
        self.assertEqual(res["failed_by_reason"].get("decode"), 1)
        self.assertEqual(res["failed_by_reason"].get("unreadable"), 1)
        self.assertTrue(res["failure_file"], "必须落盘失败清单")
        text = Path(res["failure_file"]).read_text(encoding="utf-8")
        self.assertIn("bad.jpg", text)
        self.assertIn("gone.jpg", text)
        self.assertTrue(any("哈希失败" in line for line in logs.output))

    def test_svg_is_reported_as_unsupported(self):
        mid, _ = self.add_image("a.jpg", seed=52)
        svg = self.media / "logo.svg"
        svg.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>",
                       encoding="utf-8")
        db.execute("INSERT INTO media_items(folder_id,path,filename,type,ext,size,mtime,status)"
                   " VALUES (?,?,?,'image','svg',?,?,'ok')",
                   (self.fid, str(svg), "logo.svg", svg.stat().st_size, svg.stat().st_mtime))
        res = similar.build_index()
        self.assertEqual(res["failed_by_reason"].get("unsupported"), 1)

    def test_search_endpoint_accepts_raw_body(self):
        mid, path = self.add_image()
        similar.build_index()
        app = server.create_app(SimpleNamespace())
        endpoint = next(r.endpoint for r in app.routes
                        if getattr(r, "path", None) == "/api/similar/search")
        body = path.read_bytes()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/api/similar/search",
                 "query_string": b"mode=exact&top=5",
                 "headers": [(b"content-length", str(len(body)).encode())],
                 "client": ("test", 1), "server": ("test", 80), "scheme": "http"}
        from starlette.requests import Request
        payload = asyncio.run(endpoint(Request(scope, receive)))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["id"], mid)
        self.assertEqual(payload["items"][0]["kind"], "exact")

    def test_search_rejects_non_image(self):
        app = server.create_app(SimpleNamespace())
        endpoint = next(r.endpoint for r in app.routes
                        if getattr(r, "path", None) == "/api/similar/search")
        body = b"this is not an image"

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/api/similar/search",
                 "query_string": b"", "headers": [], "client": ("t", 1),
                 "server": ("t", 80), "scheme": "http"}
        from starlette.requests import Request
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(endpoint(Request(scope, receive)))
        self.assertEqual(ctx.exception.status_code, 400)

    def tearDown(self):
        similar.invalidate()
        db.execute("DELETE FROM media_hashes")


if __name__ == "__main__":
    unittest.main()
