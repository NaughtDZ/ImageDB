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
                Image.new("RGB", (640, 400), (30 + i % 160, 80, 130)).save(path)
                db.execute("INSERT INTO media_items(folder_id,path,filename,type,ext,size) VALUES (?,?,?,'image','jpg',?)",
                           (fid, str(path), name, path.stat().st_size))
        print(f"ISOLATED_FIXTURE root={base} port={args.port}", flush=True)
        uvicorn.run(create_app(AppConfig()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
