"""Regression tests: every database/file write uses a TemporaryDirectory."""
import errno
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db, media, server, tagstats
from PIL import Image


class FileOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imagedb-files-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        dirs = dict(DATA_DIR=str(self.root / "data"), DB_PATH=str(self.root / "data" / "test.sqlite"),
                    THUMBS_DIR=str(self.root / "data" / "thumbs"), FRAMES_DIR=str(self.root / "data" / "frames"),
                    RECYCLE_DIR=str(self.root / "data" / "recycle"))
        for mod in (db, media, server):
            changes = {k: v for k, v in dirs.items() if hasattr(mod, k)}
            p = patch.multiple(mod, **changes)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(tagstats, "mark_dirty")
        self.dirty = p.start()
        self.addCleanup(p.stop)
        db.init_schema()
        self.src = self.root / "src"
        self.dst = self.root / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        self.fid = db.execute("INSERT INTO folders(name,path,is_root) VALUES (?,?,1)", ("src", str(self.src)))
        self.did = db.execute("INSERT INTO folders(name,path,is_root) VALUES (?,?,1)", ("dst", str(self.dst)))
        self.app = server.create_app(SimpleNamespace())  # deliberately no lifespan/real config

    def call(self, path, request):
        return next(r.endpoint for r in self.app.routes if getattr(r, "path", None) == path)(request)

    def add(self, name="a.jpg", exists=True):
        path = self.src / name
        if exists:
            Image.new("RGB", (1200, 800)).save(path)
        mid = db.execute("INSERT INTO media_items(folder_id,path,filename,type,ext) VALUES (?,?,?,'image','jpg')",
                         (self.fid, str(path), name))
        tid = db.execute("INSERT INTO tags(name,source) VALUES (?,'manual')", ("tag-" + str(mid),))
        db.execute("INSERT INTO media_tags(media_id,tag_id,source) VALUES (?,?,'manual')", (mid, tid))
        return mid, path

    def move(self, mid):
        return self.call("/api/media/move", server.MediaMoveRequest(media_ids=[mid], dest_dir=str(self.dst)))

    def recycled(self, payload=True, tags=None):
        stored = Path(db.RECYCLE_DIR) / "payload.jpg"
        if payload:
            Image.new("RGB", (30, 20)).save(stored)
        original = self.src / "restore.jpg"
        rid = db.execute("INSERT INTO recycle_bin(media_id,folder_id,path,filename,type,ext,stored_path,tags_json) VALUES (?,?,?,?,?,?,?,?)",
                         (100, self.fid, str(original), original.name, "image", "jpg", str(stored),
                          json.dumps(tags if tags is not None else [{"name": "restored-tag", "source": "manual", "confidence": 1.0}])))
        return rid, stored, original

    def restore(self, rid):
        return self.call("/api/recycle/restore", server.RecycleIdsRequest(ids=[rid]))

    def test_move_missing_keeps_record_and_tags(self):
        mid, _ = self.add(exists=False)
        result = self.move(mid)
        self.assertEqual(result["moved"], 0)
        self.assertTrue(result["errors"])
        self.assertIsNotNone(db.query_one("SELECT id FROM media_items WHERE id=?", (mid,)))
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM media_tags WHERE media_id=?", (mid,))["n"], 1)

    def test_move_unknown_does_not_touch_existing_file(self):
        mid, path = self.add()
        with patch.object(server.library, "probe_file", return_value="unknown"):
            result = self.move(mid)
        self.assertTrue(result["errors"])
        self.assertTrue(path.exists())
        self.assertEqual(db.query_one("SELECT path FROM media_items WHERE id=?", (mid,))["path"], str(path))

    def test_move_collision_updates_filename(self):
        mid, src = self.add()
        (self.dst / "a.jpg").write_bytes(b"do-not-overwrite")
        result = self.move(mid)
        self.assertEqual(result["moved"], 1)
        row = db.query_one("SELECT path,filename FROM media_items WHERE id=?", (mid,))
        self.assertEqual(row["filename"], "a_1.jpg")
        self.assertEqual(Path(row["path"]).name, row["filename"])
        self.assertFalse(src.exists())
        self.assertEqual((self.dst / "a.jpg").read_bytes(), b"do-not-overwrite")

    def test_move_avoids_missing_database_destination(self):
        mid, _ = self.add()
        db.execute("INSERT INTO media_items(folder_id,path,filename,type,status) VALUES (?,?,?,'image','missing')",
                   (self.did, str(self.dst / "a.jpg"), "a.jpg"))
        self.assertEqual(self.move(mid)["moved"], 1)
        self.assertEqual(db.query_one("SELECT filename FROM media_items WHERE id=?", (mid,))["filename"], "a_1.jpg")

    def test_move_database_failure_rolls_file_back(self):
        mid, src = self.add()
        with patch.object(server, "transaction", side_effect=RuntimeError("injected database failure")):
            result = self.move(mid)
        self.assertTrue(result["errors"])
        self.assertTrue(src.exists())
        self.assertFalse((self.dst / src.name).exists())
        self.assertEqual(db.query_one("SELECT path FROM media_items WHERE id=?", (mid,))["path"], str(src))

    def test_move_rollback_never_overwrites_recreated_source(self):
        mid, src = self.add()
        def fail_commit(fn):
            src.write_bytes(b"new external file")
            raise RuntimeError("injected database failure")
        with patch.object(server, "transaction", side_effect=fail_commit):
            result = self.move(mid)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(src.read_bytes(), b"new external file")
        self.assertTrue((self.dst / src.name).exists())
        self.assertIn(str(self.dst / src.name), result["errors"][0])
        self.assertIsNotNone(db.query_one("SELECT id FROM media_items WHERE id=?", (mid,)))

    def test_move_same_directory_skips(self):
        mid, src = self.add()
        result = self.call("/api/media/move", server.MediaMoveRequest(media_ids=[mid], dest_dir=str(self.src)))
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(result["errors"])
        self.assertTrue(src.exists())

    def test_move_helper_never_overwrites(self):
        src, dest = self.src / "source.bin", self.dst / "dest.bin"
        src.write_bytes(b"source")
        dest.write_bytes(b"destination")
        with self.assertRaises(OSError):
            server._move_file_no_replace(str(src), str(dest))
        self.assertEqual(src.read_bytes(), b"source")
        self.assertEqual(dest.read_bytes(), b"destination")

    def test_cross_device_copy_moves_only_after_success(self):
        src, dest = self.src / "source.bin", self.dst / "dest.bin"
        src.write_bytes(b"source")
        with patch.object(server.os, "rename", side_effect=OSError(errno.EXDEV, "cross device")):
            server._move_file_no_replace(str(src), str(dest))
        self.assertFalse(src.exists())
        self.assertEqual(dest.read_bytes(), b"source")

    def test_restore_missing_payload_keeps_recovery_record(self):
        rid, _, original = self.recycled(payload=False)
        result = self.restore(rid)
        self.assertEqual(result["restored"], 0)
        self.assertTrue(result["errors"])
        self.assertIsNotNone(db.query_one("SELECT id FROM recycle_bin WHERE id=?", (rid,)))
        self.assertIsNone(db.query_one("SELECT id FROM media_items WHERE id=100"))
        self.assertFalse(original.exists())

    def test_restore_success_recovers_labels_and_file(self):
        rid, stored, original = self.recycled()
        result = self.restore(rid)
        self.assertEqual(result["restored"], 1)
        self.assertTrue(original.is_file())
        self.assertFalse(stored.exists())
        self.assertIsNone(db.query_one("SELECT id FROM recycle_bin WHERE id=?", (rid,)))
        self.assertEqual(db.query_one("SELECT source FROM media_tags WHERE media_id=100")["source"], "manual")
        self.dirty.assert_called_once()

    def test_restore_bad_snapshot_does_not_move_file(self):
        rid, stored, original = self.recycled(tags=[{"name": "bad", "confidence": "nan"}])
        self.assertTrue(self.restore(rid)["errors"])
        self.assertTrue(stored.exists())
        self.assertFalse(original.exists())

    def test_restore_sql_failure_rolls_back_everything(self):
        rid, stored, original = self.recycled()
        db.execute("CREATE TRIGGER reject_restore BEFORE INSERT ON media_tags BEGIN SELECT RAISE(ABORT,'injected tag failure'); END")
        self.assertTrue(self.restore(rid)["errors"])
        self.assertTrue(stored.exists())
        self.assertFalse(original.exists())
        self.assertIsNone(db.query_one("SELECT id FROM media_items WHERE id=100"))
        self.assertIsNone(db.query_one("SELECT id FROM tags WHERE name='restored-tag'"))
        self.assertIsNotNone(db.query_one("SELECT id FROM recycle_bin WHERE id=?", (rid,)))

    def test_restore_existing_destination_not_overwritten(self):
        rid, stored, original = self.recycled()
        original.write_bytes(b"existing")
        self.assertTrue(self.restore(rid)["errors"])
        self.assertEqual(original.read_bytes(), b"existing")
        self.assertTrue(stored.exists())

    def test_trash_unknown_preserves_record(self):
        mid, path = self.add()
        with patch.object(server.library, "probe_file", return_value="unknown"):
            result = self.call("/api/media/trash", server.MediaTrashRequest(media_ids=[mid]))
        self.assertEqual(result["removed"], 0)
        self.assertTrue(result["errors"])
        self.assertTrue(path.exists())
        self.assertIsNotNone(db.query_one("SELECT id FROM media_items WHERE id=?", (mid,)))

    def test_explicit_trash_absent_file_counts_record_only(self):
        mid, _ = self.add(exists=False)
        result = self.call("/api/media/trash", server.MediaTrashRequest(media_ids=[mid]))
        self.assertEqual(result["records_only"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(result["errors"])
        self.assertIsNone(db.query_one("SELECT id FROM media_items WHERE id=?", (mid,)))

    def test_trash_offline_ignores_cached_reachability(self):
        mid, _ = self.add(exists=False)
        with patch.object(server.library, "_isdir_fast", return_value=False) as probe:
            result = self.call("/api/media/trash", server.MediaTrashRequest(media_ids=[mid]))
        probe.assert_called_once_with(str(self.src), ttl=0)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(result["errors"])
        self.assertIsNotNone(db.query_one("SELECT id FROM media_items WHERE id=?", (mid,)))

    def test_app_trash_is_recoverable(self):
        mid, path = self.add()
        with patch.object(server, "_os_recycle_supported", return_value=False):
            result = self.call("/api/media/trash", server.MediaTrashRequest(media_ids=[mid, mid]))
        self.assertEqual(result["app_trash"], 1)
        self.assertFalse(result["errors"])
        self.assertFalse(path.exists())
        row = db.query_one("SELECT * FROM recycle_bin WHERE media_id=?", (mid,))
        self.assertTrue(Path(row["stored_path"]).exists())
        self.assertEqual(self.restore(row["id"])["restored"], 1)
        self.assertTrue(path.exists())

    def test_thumbnail_preserves_original_dimensions(self):
        mid, path = self.add()
        self.assertTrue(media.make_thumbnail(mid, str(path), "image", thumb_size=320))
        row = db.query_one("SELECT width,height FROM media_items WHERE id=?", (mid,))
        self.assertEqual((row["width"], row["height"]), (1200,800))
        with Image.open(Path(db.THUMBS_DIR) / (str(mid)+".jpg")) as thumb:
            self.assertEqual(thumb.size, (320,213))


if __name__ == "__main__":
    unittest.main()
