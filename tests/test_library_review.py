"""Scanner review regressions: unittest + private package, temporary DB/files only."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


class LibraryReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imagedb-library-review-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "pictures"
        self.root.mkdir()
        # Do not import app.library/media before redirecting every database path.
        # A private package also avoids leaking patched module globals to other tests.
        self.pkg = "_library_review_app"
        package = types.ModuleType(self.pkg)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "app")]
        sys.modules[self.pkg] = package
        self.addCleanup(self.unload_package)
        self.db = importlib.import_module(self.pkg + ".database")
        for name, value in {
            "BASE_DIR": self.base, "DATA_DIR": self.base / "data",
            "THUMBS_DIR": self.base / "data/thumbs", "FRAMES_DIR": self.base / "data/frames",
            "RECYCLE_DIR": self.base / "data/recycle", "DB_PATH": self.base / "data/test.sqlite",
        }.items():
            setattr(self.db, name, str(value))
        self.db.init_schema()
        # No optional imaging dependencies and no real thumbnail cache access.
        self.media = types.ModuleType(self.pkg + ".media")
        self.media.delete_thumbnails = mock.Mock()
        sys.modules[self.pkg + ".media"] = self.media
        self.lib = importlib.import_module(self.pkg + ".library")
        self.addCleanup(self.lib._probe_pool.shutdown, wait=True)

    def unload_package(self):
        for name in list(sys.modules):
            if name == self.pkg or name.startswith(self.pkg + "."):
                sys.modules.pop(name, None)

    def seed(self, name="Photo.JPG", status="ok"):
        path = self.root / name
        path.write_bytes(b"temporary fixture, not decoded")
        fid = self.lib.import_folder(str(self.root))["folder_id"]
        row = self.db.query_one("SELECT * FROM media_items WHERE filename = ?", (name,))
        self.db.execute("INSERT OR IGNORE INTO tags(name) VALUES ('keep-me')")
        tid = self.db.query_one("SELECT id FROM tags WHERE name = 'keep-me'")["id"]
        self.db.execute("INSERT INTO media_tags(media_id,tag_id,source) VALUES (?,?,'manual')",
                        (row["id"], tid))
        self.db.execute("UPDATE media_items SET status = ? WHERE id = ?", (status, row["id"]))
        return fid, row["id"], path

    def item(self, mid):
        return self.db.query_one("SELECT * FROM media_items WHERE id = ?", (mid,))

    def assert_tags(self, mid):
        self.assertEqual(self.db.query_one("SELECT source FROM media_tags WHERE media_id = ?",
                                           (mid,))["source"], "manual")

    def insensitive_rename(self, old):
        new = old.with_name(old.name.lower())
        if not new.exists() or not os.path.samefile(old, new):
            self.skipTest("temporary directory is genuinely case-sensitive")
        old.rename(new)
        self.assertIn(new.name, os.listdir(self.root))
        self.assertNotIn(old.name, os.listdir(self.root))
        return new

    def test_case_rename_rescan_preserves_id_and_tags(self):
        fid, mid, old = self.seed()
        new = self.insensitive_rename(old)
        result = self.lib.rescan_folder(fid)
        self.assertEqual((result["added"], result["missing"]), (0, 0))
        self.assertEqual(self.item(mid)["path"], str(new))
        self.assertEqual(self.item(mid)["filename"], new.name)
        self.assertEqual(self.item(mid)["status"], "ok")
        self.assertEqual(len(self.db.query_all("SELECT id FROM media_items")), 1)
        self.assert_tags(mid)

    def test_case_rename_recovery_preserves_identity(self):
        fid, mid, old = self.seed(status="missing")
        new = self.insensitive_rename(old)
        result = self.lib.recheck_missing(fid)
        self.assertEqual(result, {"checked": 1, "recovered": 1, "still_missing": 0, "unknown": 0})
        self.assertEqual(self.item(mid)["path"], str(new))
        self.assert_tags(mid)

    def test_case_rename_rescan_without_global_preload(self):
        fid, mid, old = self.seed()
        new = self.insensitive_rename(old)
        with mock.patch.object(self.lib, "_KNOWN_FILES_LIMIT", 0):
            result = self.lib.rescan_folder(fid)
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.item(mid)["path"], str(new))

    def test_case_rename_repeat_imports_preserve_id(self):
        _, mid, old = self.seed()
        new = self.insensitive_rename(old)
        for fn in (self.lib.import_folder, self.lib.import_folder_progress):
            result = fn(str(self.root))
            self.assertEqual(result["media_added"], 0)
            self.assertEqual(self.item(mid)["path"], str(new))
        self.assert_tags(mid)

    def test_actual_case_sensitive_directory_keeps_distinct_files(self):
        upper = self.root / "Case.JPG"
        lower = self.root / "case.jpg"
        upper.write_bytes(b"one")
        try:
            with lower.open("xb") as stream:
                stream.write(b"two")
        except FileExistsError:
            self.skipTest("temporary filesystem is case-insensitive; modeled case test also runs")
        fid = self.lib.import_folder(str(self.root))["folder_id"]
        self.assertEqual(self.lib.rescan_folder(fid)["missing"], 0)
        self.assertEqual(len(self.db.query_all("SELECT id FROM media_items")), 2)

    def test_sensitive_directory_mismatch_does_not_merge(self):
        fid, mid, old = self.seed()
        old.unlink()
        new = self.root / "photo.jpg"
        new.write_bytes(b"a distinct file on modeled case-sensitive directory")
        real_stat = os.stat
        def sensitive_stat(path, *args, **kwargs):
            if os.fspath(path) == str(old):
                raise FileNotFoundError(str(old))
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(self.lib.os, "stat", side_effect=sensitive_stat):
            result = self.lib.rescan_folder(fid)
        self.assertEqual((result["added"], result["missing"]), (1, 1))
        self.assertEqual(self.item(mid)["path"], str(old))
        self.assert_tags(mid)

    def test_recovery_obeys_sensitive_directory_semantics(self):
        fid, mid, old = self.seed(status="missing")
        old.unlink()
        (self.root / "photo.jpg").write_bytes(b"different")
        real_stat = os.stat
        def sensitive_stat(path, *args, **kwargs):
            if os.fspath(path) == str(old):
                raise FileNotFoundError(str(old))
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(self.lib.os, "stat", side_effect=sensitive_stat):
            result = self.lib.recheck_missing(fid)
        self.assertEqual((result["recovered"], result["still_missing"]), (0, 1))
        self.assertEqual(self.item(mid)["status"], "missing")

    def test_unknown_alias_identity_neither_marks_nor_duplicates(self):
        fid, mid, old = self.seed()
        new = self.insensitive_rename(old)
        real_stat = os.stat
        def unknown_stat(path, *args, **kwargs):
            if os.fspath(path) == str(old):
                raise PermissionError("identity denied")
            return real_stat(path, *args, **kwargs)
        with mock.patch.object(self.lib.os, "stat", side_effect=unknown_stat):
            result = self.lib.rescan_folder(fid)
        self.assertEqual((result["added"], result["missing"]), (0, 0))
        self.assertEqual(self.item(mid)["filename"], old.name)
        self.assertEqual(len(self.db.query_all("SELECT id FROM media_items")), 1)
        self.assertTrue(new.exists())

    def test_unknown_alias_recovery_is_reported(self):
        fid, mid, old = self.seed(status="missing")
        self.insensitive_rename(old)
        with mock.patch.object(self.lib.os, "stat", side_effect=PermissionError("denied")):
            result = self.lib.recheck_missing(fid)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(self.item(mid)["status"], "missing")

    def test_unusual_filesystem_alias_is_not_assumed_absent(self):
        # Casefold is only candidate discovery, not the filesystem's collation.
        with mock.patch.object(self.lib, "probe_file", return_value="present"):
            result = self.lib._disk_name_match(str(self.root / "odd.JPG"), "odd.JPG",
                                               {"unrecognized.jpg"}, {"unrecognized.jpg": ["unrecognized.jpg"]})
        self.assertEqual(result, ("unknown", None))

    def test_disappearing_candidate_cannot_mark_live_old_path_absent(self):
        old = str(self.root / "Photo.JPG")
        value = types.SimpleNamespace(st_ino=123, st_dev=1)
        with mock.patch.object(self.lib.os, "stat", side_effect=[value, FileNotFoundError(), value]):
            result = self.lib._disk_name_match(old, "Photo.JPG", {"photo.jpg"},
                                               {"photo.jpg": ["photo.jpg"]})
        self.assertEqual(result, ("unknown", None))

    def test_distinct_file_identities_are_not_aliases(self):
        stats = [types.SimpleNamespace(st_ino=n, st_dev=1) for n in (123, 456)]
        with mock.patch.object(self.lib.os, "stat", side_effect=stats):
            result = self.lib._disk_name_match(str(self.root / "Photo.JPG"), "Photo.JPG",
                                               {"photo.jpg"}, {"photo.jpg": ["photo.jpg"]})
        self.assertEqual(result, ("unknown", None))

    def test_zero_file_identity_is_not_proof(self):
        fake = types.SimpleNamespace(st_ino=0, st_dev=1)
        with mock.patch.object(self.lib.os, "stat", return_value=fake):
            state, name = self.lib._disk_name_match(str(self.root / "Photo.JPG"), "Photo.JPG",
                                                     {"photo.jpg"}, {"photo.jpg": ["photo.jpg"]})
        self.assertEqual((state, name), ("unknown", None))

    def test_ambiguous_case_candidates_are_unknown(self):
        with mock.patch.object(self.lib.os, "stat", side_effect=AssertionError("no probe")):
            result = self.lib._disk_name_match(str(self.root / "PHOTO.jpg"), "PHOTO.jpg",
                {"Photo.jpg", "photo.JPG"}, {"photo.jpg": ["Photo.jpg", "photo.JPG"]})
        self.assertEqual(result, ("unknown", None))

    def test_existing_duplicate_identity_is_not_destructively_merged(self):
        fid, mid, old = self.seed(status="missing")
        new = self.insensitive_rename(old)
        self.db.execute("INSERT INTO media_items(folder_id,path,filename,type) VALUES (?,?,?,'image')",
                        (fid, str(new), new.name))
        result = self.lib.recheck_missing(fid)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(len(self.db.query_all("SELECT id FROM media_items")), 2)
        self.assert_tags(mid)

    def test_unchanged_fast_path_has_no_per_file_stat(self):
        fid, mid, path = self.seed()
        real_scandir, real_stat = os.scandir, os.stat
        class Entry:
            def __init__(self, entry):
                self.entry = entry
            def __getattr__(self, name):
                return getattr(self.entry, name)
            def stat(self, *args, **kwargs):
                raise AssertionError("unchanged media DirEntry.stat")
        def entries(directory):
            with real_scandir(directory) as iterator:
                return [Entry(e) for e in iterator]
        def stat(target, *args, **kwargs):
            if os.fspath(target) == str(path):
                raise AssertionError("unchanged media os.stat")
            return real_stat(target, *args, **kwargs)
        with mock.patch.object(self.lib.os, "scandir", side_effect=entries),              mock.patch.object(self.lib.os, "stat", side_effect=stat):
            result = self.lib.rescan_folder(fid)
        self.assertEqual((result["added"], result["missing"]), (0, 0))
        self.assert_tags(mid)

    def test_child_before_parent_import_connects_existing_subtree(self):
        child = self.root / "nested" / "child"
        child.mkdir(parents=True)
        (child / "image.jpg").write_bytes(b"fixture")
        child_id = self.lib.import_folder(str(child))["folder_id"]
        mid = self.db.query_one("SELECT id FROM media_items")["id"]
        parent = self.lib.import_folder(str(self.root))
        row = self.db.query_one("SELECT parent_id FROM folders WHERE id = ?", (child_id,))
        self.assertIsNotNone(row["parent_id"])
        self.assertIn(child_id, self.lib._subtree_folder_ids(parent["folder_id"]))
        self.assertEqual(parent["media_total"], 1)
        self.assertEqual(self.item(mid)["folder_id"], child_id)
        self.assertEqual(self.lib.rescan_folder(parent["folder_id"])["added"], 0)

    def test_parent_attachment_rejects_cycle_and_unrelated_parent(self):
        child_path = str(self.root / "child")
        cid = self.lib._insert_folder("child", child_path, None, 1)
        pid = self.lib._insert_folder("pictures", str(self.root), cid)
        self.lib._insert_folder("child", child_path, pid)
        self.assertIsNone(self.db.query_one("SELECT parent_id FROM folders WHERE id = ?", (cid,))["parent_id"])
        other = self.lib._insert_folder("other", str(self.base / "other"), None, 1)
        self.lib._insert_folder("child", child_path, other)
        self.assertIsNone(self.db.query_one("SELECT parent_id FROM folders WHERE id = ?", (cid,))["parent_id"])

    def test_existing_parent_edge_is_not_reparented(self):
        pid = self.lib._insert_folder("pictures", str(self.root), None, 1)
        other = self.lib._insert_folder("other", str(self.base / "other"), None, 1)
        cid = self.lib._insert_folder("child", str(self.root / "child"), other)
        self.lib._insert_folder("child", str(self.root / "child"), pid)
        self.assertEqual(self.db.query_one("SELECT parent_id FROM folders WHERE id = ?", (cid,))["parent_id"], other)

    def test_purge_retains_live_missing_record_and_tags(self):
        fid, mid, _ = self.seed(status="missing")
        self.assertEqual(self.lib.purge_missing(fid)["removed"], 0)
        self.assertIsNotNone(self.item(mid))
        self.assert_tags(mid)
        self.media.delete_thumbnails.assert_not_called()

    def test_purge_retains_unknown_file(self):
        fid, mid, path = self.seed(status="missing")
        path.unlink()
        with mock.patch.object(self.lib, "probe_file", return_value="unknown"):
            self.assertEqual(self.lib.purge_missing(fid)["removed"], 0)
        self.assert_tags(mid)

    def test_purge_retains_offline_and_unreadable_directory(self):
        fid, mid, path = self.seed(status="missing")
        path.unlink()
        with mock.patch.object(self.lib, "_isdir_fast", return_value=False) as reachable:
            self.assertEqual(self.lib.purge_missing(fid)["removed"], 0)
            reachable.assert_called_once_with(str(self.root), ttl=0)
        with mock.patch.object(self.lib, "_isdir_fast", return_value=True),              mock.patch.object(self.lib.os, "scandir", side_effect=PermissionError("denied")):
            self.assertEqual(self.lib.purge_missing(fid)["removed"], 0)
        self.assert_tags(mid)

    def test_purge_confirmed_absent_only_and_preserves_explicit_delete_semantics(self):
        fid, mid, path = self.seed(status="missing")
        path.unlink()
        self.assertEqual(self.lib.purge_missing(fid), {"removed": 1, "retained": 0, "live": 0, "unknown": 0})
        self.assertIsNone(self.item(mid))
        self.media.delete_thumbnails.assert_called_once_with([mid])
        _, live_id, live_path = self.seed("live.jpg")
        self.assertTrue(self.lib.remove_media_item(live_id))
        self.assertIsNone(self.item(live_id))
        self.assertTrue(live_path.exists())

    def test_purge_removed_child_directory_under_reachable_root(self):
        child = self.root / "child"
        child.mkdir()
        image = child / "gone.jpg"
        image.write_bytes(b"temporary fixture")
        fid = self.lib.import_folder(str(self.root))["folder_id"]
        mid = self.db.query_one("SELECT id FROM media_items")["id"]
        self.db.execute("UPDATE media_items SET status = 'missing' WHERE id = ?", (mid,))
        image.unlink()
        child.rmdir()
        self.assertEqual(self.lib.purge_missing(fid),
                         {"removed": 1, "retained": 0, "live": 0, "unknown": 0})
        self.assertIsNone(self.item(mid))

    def test_purge_caches_directory_and_root_checks_and_reports_retained(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.root / name).write_bytes(b"fixture")
        fid = self.lib.import_folder(str(self.root))["folder_id"]
        self.db.execute("UPDATE media_items SET status = 'missing'")
        (self.root / "b.jpg").unlink()
        (self.root / "c.jpg").unlink()
        real_probe = self.lib.probe_file
        def probe(path):
            return "unknown" if path.endswith("c.jpg") else real_probe(path)
        with mock.patch.object(self.lib.os, "scandir", wraps=os.scandir) as scan,              mock.patch.object(self.lib, "_root_of", wraps=self.lib._root_of) as root,              mock.patch.object(self.lib, "probe_file", side_effect=probe):
            result = self.lib.purge_missing(fid)
        self.assertEqual(result, {"removed": 1, "retained": 2, "live": 1, "unknown": 1})
        scan.assert_called_once_with(str(self.root))
        root.assert_called_once_with(fid)

    def test_import_progress_counts_known_files(self):
        self.seed()
        progress = []
        result = self.lib.import_folder_progress(str(self.root), progress.append)
        self.assertEqual(result["media_added"], 0)
        self.assertEqual(progress, [1])

    def test_purge_does_not_delete_concurrently_recovered_status(self):
        fid, mid, path = self.seed(status="missing")
        path.unlink()
        def recover(_):
            self.db.execute("UPDATE media_items SET status = 'ok' WHERE id = ?", (mid,))
            return "absent"
        with mock.patch.object(self.lib, "probe_file", side_effect=recover):
            self.assertEqual(self.lib.purge_missing(fid)["removed"], 0)
        self.assertEqual(self.item(mid)["status"], "ok")
        self.assert_tags(mid)

    def test_offline_rescan_and_readonly_check_never_mark(self):
        fid, mid, path = self.seed()
        path.unlink()
        with mock.patch.object(self.lib, "root_offline", return_value=True):
            self.assertTrue(self.lib.rescan_folder(fid)["root_offline"])
            self.assertTrue(self.lib.check_folder(fid)["root_offline"])
        self.assertEqual(self.item(mid)["status"], "ok")
        self.assertEqual(self.lib.check_folder(fid)["missing"], 0)
        self.assertEqual(self.item(mid)["status"], "ok")

    def test_unreadable_scan_does_not_mark(self):
        fid, mid, _ = self.seed()
        with mock.patch.object(self.lib, "probe_dir", return_value="unknown"):
            result = self.lib.rescan_folder(fid)
        self.assertEqual(result["dirs_unknown"], 1)
        self.assertEqual(self.item(mid)["status"], "ok")

    def test_start_rescan_atomically_requests_exclusive_scan(self):
        fid, _, _ = self.seed()
        jobs = self.lib.jobs
        with mock.patch.object(jobs, "run_async", side_effect=lambda jid, fn, name: jid):
            jid = self.lib.start_rescan(fid)
            self.assertEqual(jobs.get_job(jid)["status"], "running")
            with self.assertRaises(ValueError):
                self.lib.start_rescan(fid)
        self.assertEqual(len(jobs.JOBS), 1)


if __name__ == "__main__":
    unittest.main()
