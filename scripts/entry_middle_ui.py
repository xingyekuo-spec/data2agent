"""PyInstaller / 双击入口 → d2a-middle-ui.exe(中间机管理界面)。"""
from __future__ import annotations

import sys

from launch_admin_ui import main

if __name__ == "__main__":
    raise SystemExit(main(["--role", "middle", *sys.argv[1:]]))
