#!/usr/bin/env python3
"""隔离模式启动器：python3 -I run_pnl.py <子命令> ...

`python3 -I`（等价 -E -s -P）不会把脚本目录加入 sys.path，
因此这里先把本文件所在目录显式插入，再调用包内 CLI。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from channel_pnl.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
