"""PyInstaller / 双击入口 → d2a-platform-ui.exe(平台机管理界面)。"""
from __future__ import annotations

import sys

from launch_admin_ui import main

if __name__ == "__main__":
    raise SystemExit(main(["--role", "platform", *sys.argv[1:]]))
