"""重新扫描的进度反馈回归测试（隔离临时库）。

背景：用户手动删掉目录后点「重新扫描」，准备阶段与阶段 2 都没有进度上报，
界面停在 0% 像卡死。这里锁住三件事：进度要早、要连续、要单调。
"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import database as db

_TMP = tempfile.TemporaryDirectory(prefix="imagedb-rescan-progress-")
_ROOT = Path(_TMP.name)
db.DATA_DIR = str(_ROOT / "data")
db.DB_PATH = str(_ROOT / "data" / "test.sqlite")
db.THUMBS_DIR = str(_ROOT / "data" / "thumbs")
db.FRAMES_DIR = str(_ROOT / "data" / "frames")
db.RECYCLE_DIR = str(_ROOT / "data" / "recycle")
db.init_schema()

from app import library  # noqa: E402


class RescanProgressTests(unittest.TestCase):
    def setUp(self):
        for table in ("media_hashes", "media_tags", "tags", "media_items", "folders"):
            db.execute("DELETE FROM " + table)
        self.tmp = tempfile.TemporaryDirectory(prefix="imagedb-rescan-case-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lib"
        self.root.mkdir()
        self.root_id = db.execute(
            "INSERT INTO folders(name,path,is_root) VALUES ('lib',?,1)", (str(self.root),))
        self.calls = []

    def add_subdirs(self, n):
        for i in range(n):
            sub = self.root / f"d{i:03d}"
            sub.mkdir()
            img = sub / "a.jpg"
            Image.new("RGB", (8, 8)).save(img)
            fid = db.execute("INSERT INTO folders(name,path,parent_id) VALUES (?,?,?)",
                             (sub.name, str(sub), self.root_id))
            db.execute(
                "INSERT INTO media_items(folder_id,path,filename,type,ext,size,mtime,status)"
                " VALUES (?,?,?,'image','jpg',?,?,'ok')",
                (fid, str(img), "a.jpg", img.stat().st_size, img.stat().st_mtime))

    def run_rescan(self):
        self.calls = []
        res = library.rescan_folder(
            self.root_id, progress_cb=lambda pct, msg: self.calls.append((pct, msg)))
        return res

    def test_progress_reported_during_prepare_and_phase2(self):
        self.add_subdirs(30)
        for i in range(30):                      # 模拟用户把整棵子目录删掉
            (self.root / f"d{i:03d}" / "a.jpg").unlink()
            (self.root / f"d{i:03d}").rmdir()
        res = self.run_rescan()

        self.assertEqual(res["missing"], 30)
        self.assertEqual(res["dirs_unknown"], 0)
        self.assertTrue(self.calls, "必须有进度回调")
        self.assertLessEqual(self.calls[0][0], 2, "进度必须一开始就动，不能停在 0")
        messages = [m for _, m in self.calls]
        self.assertTrue(any("预载" in m or "目录列表" in m for m in messages),
                        "准备阶段也要有进度：" + repr(messages[:4]))
        self.assertTrue(any("核对未扫到的目录" in m for m in messages),
                        "阶段 2 也要有进度：" + repr(messages[-3:]))
        pcts = [p for p, _ in self.calls]
        self.assertEqual(pcts, sorted(pcts), "进度不能倒退")
        self.assertEqual(pcts[-1], 99)

    def test_deleted_dirs_marked_missing_but_kept(self):
        self.add_subdirs(5)
        for i in range(5):
            (self.root / f"d{i:03d}" / "a.jpg").unlink()
            (self.root / f"d{i:03d}").rmdir()
        self.run_rescan()
        self.assertEqual(db.query_one(
            "SELECT COUNT(*) n FROM media_items WHERE status='missing'")["n"], 5)
        # 只标记、绝不删记录
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM media_items")["n"], 5)

    def test_healthy_tree_reports_scan_progress_and_marks_nothing(self):
        self.add_subdirs(25)
        res = self.run_rescan()
        self.assertEqual(res["missing"], 0)
        self.assertEqual(res["recovered"], 0)
        self.assertTrue(any("扫描磁盘" in m for _, m in self.calls))
        self.assertEqual(db.query_one(
            "SELECT COUNT(*) n FROM media_items WHERE status='missing'")["n"], 0)

    def test_unknown_parent_is_not_marked(self):
        """父目录读不到时必须计 unknown、不标记（三态语义不能被优化掉）。"""
        self.add_subdirs(4)
        real_scandir = library.os.scandir

        def fake_scandir(path, *a, **k):
            raise PermissionError(13, "denied")

        library.os.scandir = fake_scandir
        try:
            res = self.run_rescan()
        finally:
            library.os.scandir = real_scandir
        self.assertEqual(db.query_one(
            "SELECT COUNT(*) n FROM media_items WHERE status='missing'")["n"], 0)


if __name__ == "__main__":
    unittest.main()
