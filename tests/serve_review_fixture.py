"""Isolated browser QA server; never opens the production database.
Run from workspace: python -B tests/serve_review_fixture.py --port 8896
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import database as db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8896)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="imagedb-browser-review-") as tmp:
        base = Path(tmp)
        db.DATA_DIR = str(base / "data")
        db.DB_PATH = str(base / "data" / "fixture.sqlite")
        db.THUMBS_DIR = str(base / "data" / "thumbs")
        db.FRAMES_DIR = str(base / "data" / "frames")
        db.RECYCLE_DIR = str(base / "data" / "recycle")
        assert Path(db.DB_PATH).is_relative_to(base)
        db.init_schema()
        # Import dependent modules only after replacing every bound data path.
        from app.config import AppConfig
        from app.server import create_app
        from PIL import Image
        import uvicorn
        images = base / "images"
        images.mkdir()
        root_id = db.execute("INSERT INTO folders(name,path,is_root) VALUES (?,?,1)", ("Review fixture", str(images)))
        for group, count in (("alpha", 130), ("beta", 5)):
            folder = images / group
            folder.mkdir()
            fid = db.execute("INSERT INTO folders(name,path,parent_id) VALUES (?,?,?)", (group, str(folder), root_id))
            for i in range(count):
                name = f"{group}-{i:03}.jpg"
                path = folder / name
                # 每张都造出可区分的图案：纯色图的感知哈希几乎一样，测不出搜索效果
                im = Image.new("RGB", (640, 400), (20 + i % 200, 60 + (i * 7) % 180, 120))
                px = im.load()
                for y in range(0, 400, 4):
                    for x in range(0, 640, 4):
                        px[x, y] = ((x + i * 13) % 256, (y * 2 + i) % 256, (x * y + i) % 256)
                im.save(path)
                db.execute("INSERT INTO media_items(folder_id,path,filename,type,ext,size,mtime) VALUES (?,?,?,'image','jpg',?,?)",
                           (fid, str(path), name, path.stat().st_size, path.stat().st_mtime))
                if group == "alpha" and i == 0:
                    query = Path(__file__).resolve().parents[1] / "_isotest" / "browser_query.jpg"
                    query.parent.mkdir(parents=True, exist_ok=True)
                    query.write_bytes(path.read_bytes())
        from app import similar
        stats = similar.build_index()
        print(f"FIXTURE_INDEX added={stats['added']} failed={stats['failed']}", flush=True)
        print(f"ISOLATED_FIXTURE root={base} port={args.port}", flush=True)
        uvicorn.run(create_app(AppConfig()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
