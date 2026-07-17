"""PyInstaller 入口 → 便携包 data2agent.exe(中间机)。"""
from __future__ import annotations

import sys

from launch_admin_ui import main

if __name__ == "__main__":
    raise SystemExit(main(["--role", "middle", *sys.argv[1:]]))
