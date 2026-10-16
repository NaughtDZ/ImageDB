"""Safety regressions. Only temporary SQLite databases; never import server/settings.

Run: .venv/Scripts/python.exe -m unittest discover -s tests -p test_tags_jobs_review.py -v
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app import database, imagetag, jobs, tagstats
from app.tagging.base import TagResult
from app.tagging.manager import PluginManager


class TemporaryTagDatabase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="imagedb-tags-review-"))
        self.root = Path(self.tmp)
        self.db_path = str(self.root / "test.sqlite")
        self.stack.enter_context(patch.object(database, "DB_PATH", self.db_path))
        # Do not call init_schema/ensure_dirs: no production folders or settings.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(database.SCHEMA)
        self.stack.enter_context(patch.object(tagstats, "mark_dirty"))
        self.fid = database.execute("INSERT INTO folders(name,path) VALUES (?,?)", ("test", self.tmp))
        self.mid = self.add_media("image.jpg")
        self.manager = object.__new__(PluginManager)  # No discovery/config/model initialization.
        self.manager._plugins = {}
        self.manager._config_provider = lambda: Mock(get_int=Mock(return_value=1))

    def add_media(self, filename):
        return database.execute(
            "INSERT INTO media_items(folder_id,path,filename,type) VALUES (?,?,?,'image')",
            (self.fid, str(self.root / filename), filename),
        )

    def tags(self, mid=None):
        return database.query_all(
            "SELECT t.name, mt.confidence, mt.source FROM media_tags mt "
            "JOIN tags t ON t.id=mt.tag_id WHERE media_id=? ORDER BY t.name", (mid or self.mid,),
        )

    def seed(self, name, source, confidence=1.0):
        imagetag._write_tags(self.mid, [{"name": name, "confidence": confidence}], source)

    def write_sidecar(self, entries):
        with closing(sqlite3.connect(imagetag.sidecar_path(self.tmp))) as conn, conn:
            conn.execute("CREATE TABLE tags(filename TEXT PRIMARY KEY, tags_json TEXT NOT NULL)")
            conn.executemany("INSERT INTO tags VALUES (?,?)", entries.items())

    def new_tag_job(self, status="pending"):
        return database.execute("INSERT INTO tag_jobs(status) VALUES (?)", (status,))

    def test_readonly_uri_encodes_unc_and_drive_paths(self):
        with patch.object(imagetag.os.path, "abspath", return_value="//server/share/has #?%/.imgtag"):
            self.assertEqual(imagetag._readonly_sidecar_uri("ignored"),
                             "file:////server/share/has%20%23%3F%25/.imgtag?mode=ro")
        with patch.object(imagetag.os.path, "abspath", return_value="C:/has #?%/.imgtag"):
            self.assertEqual(imagetag._readonly_sidecar_uri("ignored"),
                             "file:///C:/has%20%23%3F%25/.imgtag?mode=ro")

    def test_missing_sidecar_is_not_created(self):
        self.assertEqual(imagetag.read_tags(self.tmp), {})
        self.assertFalse(Path(imagetag.sidecar_path(self.tmp)).exists())

    def test_model_overwrite_never_takes_manual_or_other_source(self):
        self.seed("manual", "manual", 0.8)
        self.seed("other", "other-model", 0.7)
        self.seed("old", "wd14")
        self.manager._write_tags(self.mid, [TagResult("manual", 0.1), TagResult("other", 0.2),
                                           TagResult("new", 0.0)], "wd14", True)
        self.assertEqual(self.tags(), [
            {"name": "manual", "confidence": 0.8, "source": "manual"},
            {"name": "new", "confidence": 0.0, "source": "wd14"},
            {"name": "other", "confidence": 0.7, "source": "other-model"},
        ])
        self.manager._write_tags(self.mid, [], "wd14", True)
        self.assertEqual([r["source"] for r in self.tags()], ["manual", "other-model"])
        # Even accidental use of the reserved manual source cannot delete manual links.
        self.manager._write_tags(self.mid, [], "manual", True)
        self.assertEqual(len(self.tags()), 2)

    def test_model_validation_precedes_delete(self):
        self.seed("old", "wd14")
        before = self.tags()
        for invalid in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(confidence=invalid), self.assertRaises(ValueError):
                self.manager._write_tags(self.mid, [TagResult("good"), TagResult("bad", invalid)], "wd14", True)
            self.assertEqual(self.tags(), before)
        with self.assertRaises(ValueError):
            self.manager._write_tags(self.mid, [TagResult(" ")], "wd14", True)
        with self.assertRaises(ValueError):
            self.manager._write_tags(self.mid, None, "wd14", True)
        self.assertEqual(self.tags(), before)

    def test_write_failure_rolls_back_deletion_and_partial_tag_creation(self):
        self.seed("old", "import")
        database.execute("CREATE TRIGGER reject_broken BEFORE INSERT ON media_tags "
                         "WHEN (SELECT name FROM tags WHERE id=NEW.tag_id)='broken' "
                         "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            imagetag._write_tags(self.mid, ["good", "broken"], "import", True)
        self.assertEqual([r["name"] for r in self.tags()], ["old"])
        self.assertIsNone(database.query_one("SELECT id FROM tags WHERE name='good'"))
        self.assertIsNone(database.query_one("SELECT id FROM tags WHERE name='broken'"))

    def test_bad_sidecar_rows_report_errors_without_clearing(self):
        self.seed("old", "import")
        malformed = ["not-json", "{}", "null", '[{"name":"ok"},7]',
                     '[{"name":"bad","confidence":NaN}]', '[{"name":"bad","confidence":"oops"}]',
                     '[{"name":""}]']
        for payload in malformed:
            with self.subTest(payload=payload):
                sidecar = Path(imagetag.sidecar_path(self.tmp))
                if sidecar.exists():
                    with closing(sqlite3.connect(sidecar)) as conn, conn:
                        conn.execute("UPDATE tags SET tags_json=?", (payload,))
                else:
                    self.write_sidecar({"image.jpg": payload})
                errors = []
                self.assertEqual(imagetag.read_tags(self.tmp, errors=errors), {})
                self.assertEqual(len(errors), 1)
                result = imagetag.import_folder(self.fid, overwrite=True)
                self.assertEqual((result["media"], result["tags"], result["files"]), (0, 0, 1))
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["errors"][0]["filename"], "image.jpg")
                self.assertEqual([r["name"] for r in self.tags()], ["old"])

    def test_valid_empty_list_clears_only_import_and_zero_is_preserved(self):
        self.seed("manual", "manual")
        self.seed("other", "wd14")
        self.seed("old", "import")
        empty_id = self.add_media("empty.jpg")
        imagetag._write_tags(empty_id, ["remove"], "import")
        self.write_sidecar({"empty.jpg": "[]", "image.jpg": json.dumps([
            {"name": "zero", "confidence": 0}, {"name": "default", "confidence": None},
            {"name": "manual", "confidence": 0.2}, {"name": "other", "confidence": 0.1},
        ])})
        self.assertEqual(imagetag.read_tags(self.tmp)["empty.jpg"], [])
        result = imagetag.import_folder(self.fid, overwrite=True)
        self.assertEqual(result, {"media": 2, "tags": 2, "files": 1, "errors": []})
        self.assertEqual(self.tags(empty_id), [])
        current = {r["name"]: r for r in self.tags()}
        self.assertEqual(current["zero"]["confidence"], 0.0)
        self.assertEqual(current["default"]["confidence"], 1.0)
        self.assertEqual(current["manual"]["source"], "manual")
        self.assertEqual(current["other"]["source"], "wd14")
        self.assertNotIn("old", current)
        tagstats.mark_dirty.assert_called()

    def test_invalid_row_does_not_block_other_images(self):
        self.seed("old", "import")
        good = self.add_media("good.jpg")
        self.write_sidecar({"image.jpg": "{}", "good.jpg": '["valid"]'})
        result = imagetag.import_folder(self.fid, overwrite=True)
        self.assertEqual((result["media"], result["tags"], len(result["errors"])), (1, 1, 1))
        self.assertEqual([r["name"] for r in self.tags()], ["old"])
        self.assertEqual([r["name"] for r in self.tags(good)], ["valid"])

    def test_failed_image_reports_error_and_continues(self):
        self.seed("old", "import")
        good = self.add_media("good.jpg")
        database.execute("CREATE TRIGGER reject_broken BEFORE INSERT ON media_tags "
                         "WHEN (SELECT name FROM tags WHERE id=NEW.tag_id)='broken' "
                         "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        self.write_sidecar({"image.jpg": '["new","broken"]', "good.jpg": '["valid"]'})
        result = imagetag.import_folder(self.fid, overwrite=True)
        self.assertEqual((result["media"], result["tags"], len(result["errors"])), (1, 1, 1))
        self.assertEqual([r["name"] for r in self.tags()], ["old"])
        self.assertEqual([r["name"] for r in self.tags(good)], ["valid"])

    def test_conflicting_tag_creation_and_duplicate_counts(self):
        ids = [self.mid] + [self.add_media(f"image-{i}.jpg") for i in range(7)]
        barrier = threading.Barrier(len(ids))
        def write(mid):
            barrier.wait(timeout=5)
            return imagetag._write_tags(mid, ["shared", "shared"], "import")
        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            self.assertEqual(list(pool.map(write, ids)), [1] * len(ids))
        self.assertEqual(database.query_one("SELECT COUNT(*) c FROM tags")["c"], 1)
        self.assertEqual(imagetag._write_tags(self.mid, ["shared"], "other"), 0)

    def test_model_load_error_finalizes_job(self):
        jid = self.new_tag_job()
        plugin = Mock(is_loaded=False)
        plugin.load.side_effect = RuntimeError("load exploded")
        self.manager._plugins["wd14"] = plugin
        self.manager._run_job(jid, "wd14", "media", [self.mid], True)
        job = self.manager.get_job(jid)
        self.assertEqual(job["status"], "failed")
        self.assertIn("load exploded", job["message"])
        self.assertIsNotNone(job["finished_at"])

    def test_outer_setup_error_finalizes_job(self):
        jid = self.new_tag_job()
        self.manager._plugins["wd14"] = Mock()
        with patch.object(self.manager, "_media_in_folders", side_effect=RuntimeError("lookup failed")):
            self.manager._run_job(jid, "wd14", "folder", [self.fid], True)
        self.assertEqual(self.manager.get_job(jid)["status"], "failed")

    def test_cancel_during_inference_prevents_writes_and_done_override(self):
        self.seed("old", "wd14")
        jid = self.new_tag_job()
        self.manager._plugins["wd14"] = Mock(is_loaded=True)
        def inference(*args):
            database.execute("UPDATE tag_jobs SET status='cancelled' WHERE id=?", (jid,))
            return [[TagResult("new")]]
        with patch.object(self.manager, "_tag_batch", side_effect=inference):
            self.manager._run_job(jid, "wd14", "media", [self.mid], True)
        self.assertEqual(self.manager.get_job(jid)["status"], "cancelled")
        self.assertIsNotNone(self.manager.get_job(jid)["finished_at"])
        self.assertEqual([r["name"] for r in self.tags()], ["old"])

    def test_thread_start_error_finalizes_tag_job(self):
        self.manager._plugins["wd14"] = Mock()
        with patch("app.tagging.manager.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                self.manager.start_job("wd14", "media", [self.mid], True)
        self.assertEqual(database.query_one("SELECT status FROM tag_jobs")["status"], "failed")


class JobRegistryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(jobs, "JOBS", {}))
        self.stack.enter_context(patch.object(jobs, "MAX_JOBS", 5))

    def test_active_jobs_never_evicted_even_over_limit(self):
        ids = [jobs.new_job("scan", "active") for _ in range(12)]
        self.assertEqual(len(jobs.JOBS), 12)
        for _ in range(20):
            jid = jobs.new_job("other", "finished")
            jobs.finish(jid, "done")
        self.assertTrue(all(jobs.get_job(jid)["status"] == "running" for jid in ids))
        self.assertIsNotNone(jobs.busy("scan"))
        for jid in ids:
            jobs.finish(jid, "done")
        self.assertLessEqual(len(jobs.JOBS), jobs.MAX_JOBS)

    def test_exclusive_claim_is_atomic(self):
        barrier = threading.Barrier(12)
        def claim(_):
            barrier.wait(timeout=5)
            try:
                return jobs.new_job("scan", "scan", exclusive=True)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = [jid for jid in pool.map(claim, range(12)) if jid]
        self.assertEqual(len(ids), 1)
        self.assertIsNotNone(jobs.new_job("export", "independent", exclusive=True))
        jobs.finish(ids[0], "failed", error="failed")
        self.assertIsNotNone(jobs.new_job("scan", "retry", exclusive=True))

    def test_async_thread_start_failure_releases_exclusive_job(self):
        jid = jobs.new_job("scan", "scan", exclusive=True)
        with patch("app.jobs.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                jobs.run_async(jid, lambda: None)
        self.assertEqual(jobs.get_job(jid)["status"], "failed")
        self.assertIsNone(jobs.busy("scan"))


class CacheGenerationTests(unittest.TestCase):
    """No database connection: controlled transaction scheduling probes races."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("_generation", 0), ("_built_generation", -1),
                            ("_initialized", False), ("_rebuilding", False),
                            ("_last_rebuild", 0.0), ("COALESCE_SEC", 0.0)):
            self.stack.enter_context(patch.object(tagstats, name, value))
        self.stack.enter_context(patch.object(tagstats, "_idle", threading.Event()))
        tagstats._idle.set()
        self.stack.enter_context(patch.object(tagstats, "query_one", return_value={"c": 0}))
        # All count SQL is replaced: no production path can be opened by these tests.
        self.transaction = self.stack.enter_context(patch.object(tagstats, "transaction", return_value=0))

    def test_mark_during_rebuild_is_not_acknowledged(self):
        def building(_):
            tagstats.mark_dirty()
            return 0
        self.transaction.side_effect = building
        with patch.object(tagstats, "_kick"):
            tagstats.rebuild_now()
        self.assertEqual(tagstats._built_generation, 0)
        self.assertEqual(tagstats._generation, 1)
        with patch.object(tagstats, "_kick") as kick:
            tagstats.ensure_fresh()
            kick.assert_called_once()

    def test_worker_rebuilds_again_for_concurrent_generation(self):
        def building(_):
            if self.transaction.call_count == 1:
                tagstats.mark_dirty()  # A commit notification arrives during the build.
            return 0
        self.transaction.side_effect = building
        tagstats._rebuilding = True
        tagstats._idle.clear()
        tagstats._worker()
        self.assertEqual(self.transaction.call_count, 2)
        self.assertEqual(tagstats._built_generation, tagstats._generation)
        self.assertFalse(tagstats._rebuilding)
        self.assertTrue(tagstats._idle.is_set())

    def test_burst_marks_start_one_worker(self):
        with patch.object(tagstats.threading, "Thread") as thread:
            for _ in range(100):
                tagstats.mark_dirty()
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(tagstats._generation, 100)
            self.assertTrue(tagstats._rebuilding)
        tagstats._worker()
        self.assertEqual(self.transaction.call_count, 1)
        self.assertEqual(tagstats._built_generation, 100)

    def test_initialized_empty_cache_does_not_rebuild_endlessly(self):
        tagstats.rebuild_now()
        with patch.object(tagstats, "_kick") as kick:
            for _ in range(20):
                tagstats.ensure_fresh()
            kick.assert_not_called()
        self.assertEqual(self.transaction.call_count, 1)
        tagstats.query_one.assert_not_called()

    def test_build_failure_keeps_generation_pending(self):
        self.transaction.side_effect = RuntimeError("transaction failed")
        tagstats._rebuilding = True
        tagstats._worker()
        self.assertFalse(tagstats._initialized)
        self.assertEqual(tagstats._built_generation, -1)
        self.assertFalse(tagstats._rebuilding)
        self.assertTrue(tagstats._idle.is_set())


if __name__ == "__main__":
    unittest.main()
