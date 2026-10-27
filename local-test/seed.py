#!/usr/bin/env python3
"""重建 local-test 本地端到端测试环境。

数据文件(local-test/data/)不入库,clone 或清理后在本仓库根目录执行一次:

    python local-test/seed.py

生成内容:
  data/source.sqlite   E10-like 参考源库(测试 fixture,与自动测试同源)
  data/landing.sqlite  本地落地库(已跑一轮 sync,sink=local 仅供开发)

webhook 推送联调见 local-test/README.md。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "local-test" / "data"
CONFIG = "local-test/config/connect.yaml"


def run(args: list[str]) -> None:
    print(f"$ {' '.join(args)}", flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    # 清掉旧库与锁,保证可重复执行
    for pattern in ("source.sqlite*", "landing.sqlite*", "locks/*.lock"):
        for path in DATA.glob(pattern):
            path.unlink()
            print(f"removed {path.relative_to(ROOT)}")

    run([sys.executable, "-m", "tests.fixtures.e10.seed", "--db", str(DATA / "source.sqlite")])
    run([sys.executable, "-m", "data2agent.middle.extract", "sync", "--config", CONFIG])

    print("\n完成。查看水位与运行:")
    print("  python -m data2agent.middle.extract status "
          "--landing local-test/data/landing.sqlite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
