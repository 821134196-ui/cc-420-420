#!/usr/bin/env python3
"""单条命令本地启动：初始化 SQLite、预置演示数据（发卡/挂失/回收）、启动 Web 服务。

用法：
    python run.py                # 默认 data/accessctl.db，端口 8088
    python run.py --reset        # 删除旧库后重新预置
    python run.py --port 9000
    python run.py --no-seed      # 仅初始化空库
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accessctl.db import Database  # noqa: E402
from accessctl.seed import seed  # noqa: E402
from accessctl.server import build_server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="楼宇门禁卡发放与离场回收系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--db", default=None, help="SQLite 路径，默认 data/accessctl.db")
    parser.add_argument("--reset", action="store_true", help="删除旧库重新预置")
    parser.add_argument("--no-seed", action="store_true", help="不预置演示数据")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else (
        Path(__file__).resolve().parent / "data" / "accessctl.db")
    if args.reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        print(f"已重置数据库：{db_path}")

    db = Database(db_path)
    if not args.no_seed:
        seed(db)

    server, scheduler = build_server(str(db_path), args.host, args.port,
                                     run_scheduler=True)
    # build_server 会再次打开库；种子数据已在同一文件，WAL 下对新连接立即可见
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print("  楼宇门禁卡发放与离场回收系统已启动")
    print(f"  访问地址：{url}")
    print(f"  数据库  ：{db_path}")
    print("  演示账号：1 林岚(管理员) / 2 王晨(柜台) / 4 陈磊(门岗) / 5 周敏(审计)")
    print("  停止服务：Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        if scheduler:
            scheduler.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
