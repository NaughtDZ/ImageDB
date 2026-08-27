# -*- coding: utf-8 -*-
"""
ImageDB 素材管理器 —— 程序入口
================================
一个类似 Eagle 的本地素材（图片/视频）管理工具。

用法：
    python main.py                 # 启动，默认端口 8000，自动打开浏览器
    python main.py --port 9000     # 指定端口
    python main.py --no-browser    # 不自动打开浏览器
    python main.py --host 0.0.0.0  # 允许局域网访问

启动流程：
    1. 初始化 SQLite 数据库（data/imagedb.sqlite，程序根目录下）
    2. 加载配置（代理、打标工具、视频参数等）
    3. 启动后台校验线程（自动清理磁盘上已不存在的文件记录）
    4. 启动 HTTP 服务并自动打开浏览器
"""
import argparse
import logging
import os
import sys
import threading
import time
import webbrowser

# 确保脚本所在目录在模块搜索路径中（便于直接 python main.py 运行）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("imagedb.main")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="ImageDB 本地素材管理工具")
    parser.add_argument("--port", type=int, default=None, help="HTTP 服务端口（默认读取配置，缺省 8000）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args()


def open_browser(url: str) -> None:
    """等待服务就绪后自动打开浏览器。"""
    import socket
    try:
        port = int(url.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        port = 8000
    for _ in range(50):  # 最多等待 5 秒
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(url)


def find_free_port(host: str, start_port: int) -> int:
    """探测可用端口：若 start_port 被占用，依次尝试 start_port+1, +2, ...（最多 +50）。
    返回一个可绑定的端口。若全部被占用则返回原端口（交给 uvicorn 处理并报错）。"""
    import socket
    for offset in range(51):
        port = start_port + offset
        if port > 65535:
            break
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port   # 能绑定说明可用
            except OSError:
                continue      # 被占用，尝试下一个
    return start_port


def main() -> None:
    args = parse_args()

    # 1. 初始化数据库（建表，幂等）
    from app.database import init_schema
    init_schema()
    logger.info("数据库初始化完成：%s", os.path.join(BASE_DIR, "data", "imagedb.sqlite"))

    # 2. 加载配置
    from app.config import AppConfig
    config = AppConfig()
    port = args.port or config.get_int("port", 8000)

    # 3. 端口探测：若被占用（如上次残留进程），自动尝试下一个可用端口
    port = find_free_port(args.host, port)

    # 4. 构建 FastAPI 应用（启动时自动拉起后台校验线程）
    from app.server import create_app
    app = create_app(config)

    # 5. 启动服务器（放在主线程，浏览器用定时器延迟打开）
    import uvicorn
    url = f"http://{args.host}:{port}"
    if not args.no_browser:
        threading.Timer(0.8, open_browser, args=(url,)).start()
    logger.info("ImageDB 已启动：%s", url)
    logger.info("按 Ctrl+C 退出")
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
